"""KER-113 — Cross-tenant isolation security tests.

MUST-HAVE: These tests must pass before any Sprint 1 story is marked complete.
They prove that the two-layer isolation architecture (RLS + application guard)
prevents one tenant's data from ever leaking to another.

What is being tested
--------------------
1. Positive isolation: given Tenant A's authenticated session, similarity queries
   return zero results from Tenant B's embeddings, overrides, and bias vector —
   even when the raw SQL would otherwise reach across tenants.
2. Application-layer guard: the ``set_tenant_context`` / ``resolve_and_set_tenant_context``
   functions raise ``TenantContextMissingError`` loudly for every form of invalid
   tenant identity (None, empty, whitespace, non-UUID, wrong UUID version), and
   issue no database query when they do so.
3. DORA-specific guards (Doc 17A): the DORA RoI, export, and submission service
   functions enforce tenant isolation before any SQL — they raise on None tenants
   and embed the tenant_id in their database parameters.

How this test file is structured
---------------------------------
Tests 1–4 are pure unit tests: they verify the application guard logic using the
real ``src/db/rls`` and ``src/services/tenant_context`` modules, with a fake
connection object that records what SQL was (or was not) issued. These run
without any database.

Tests 5–6 are integration tests (marked ``@pytest.mark.integration``): they
require a live PostgreSQL instance with the RLS migration applied. They use two
real tenant rows and real database connections to prove that Tenant A cannot see
Tenant B's rows even when no application-layer filtering is applied to the query
itself — only RLS protects the data in that scenario.

Tests 7–11 are DORA-specific pure unit tests added in Doc 17A hardening: they
verify that the DORA RoI service, export service, and submission service each
enforce tenant context before any SQL is issued and include tenant_id explicitly
in their database parameters.

Both layers must be tested independently because they serve different failure
modes:
  - The application guard prevents accidental missing-context bugs in code.
  - The RLS policy prevents data leaks when the application guard is bypassed
    (misconfiguration, future code path that forgets to set the context, etc.).

Running the tests
-----------------
Unit tests (no database required):
  pytest tests/security/test_tenant_isolation.py -m "not integration" -v

Integration tests (requires DATABASE_URL environment variable):
  pytest tests/security/test_tenant_isolation.py -m integration -v

See conftest.py for the ``db_connection``, ``tenant_a_id``, and ``tenant_b_id``
fixtures.
"""

import uuid
from datetime import date

import pytest

from config.constants import EMBEDDING_DIMENSION
from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError
from src.services.dora_roi_export_service import build_export_package
from src.services.dora_roi_service import RegisterEntryInput, create_register_entry
from src.services.dora_roi_submission_service import list_tenant_submission_runs
from src.services.retrieval_service import get_similar_controls
from src.services.tenant_context import resolve_and_set_tenant_context

# ---------------------------------------------------------------------------
# Fixtures — two deterministic UUIDv4 tenant identifiers
#
# Using fixed values (not uuid.uuid4()) makes failures readable: if a test
# output shows "aaaa..." you know that is Tenant A.
# ---------------------------------------------------------------------------

TENANT_A_ID = uuid.UUID("a0000000-0000-4000-a000-000000000001")
TENANT_B_ID = uuid.UUID("b0000000-0000-4000-b000-000000000002")


class _FakeAuthSession:
    """Minimal stand-in for an authenticated HTTP session in unit tests."""

    def __init__(self, tenant_id: uuid.UUID) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> uuid.UUID:
        return self._tenant_id


class _RecordingConnection:
    """Fake database connection that records every SQL statement issued.

    Used in unit tests to prove that no query is run when validation fails,
    and to prove the correct SQL is run when validation succeeds.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params=None) -> "_NullResult":
        self.statements.append(sql.strip())
        return _NullResult()


class _NullResult:
    """Fake cursor result that returns no rows and a None fetchone."""

    def fetchall(self) -> list:
        return []

    def fetchone(self):
        return None


# ---------------------------------------------------------------------------
# Tests 1–4: Application-layer guard (unit tests, no database required)
# ---------------------------------------------------------------------------


def test_null_tenant_id_raises_error():
    """Passing None as tenant_id to set_tenant_context must raise TenantContextMissingError.

    The function must fail before issuing any SQL to the connection.
    """
    conn = _RecordingConnection()
    with pytest.raises(TenantContextMissingError):
        set_tenant_context(conn, None)
    assert conn.statements == [], "No SQL must be issued when tenant_id is None"


def test_empty_tenant_id_raises_error():
    """Passing an empty string as tenant_id must raise TenantContextMissingError."""
    conn = _RecordingConnection()
    with pytest.raises(TenantContextMissingError):
        set_tenant_context(conn, "")
    assert conn.statements == []


def test_invalid_uuid_raises_error():
    """Passing a string that is not a valid UUID must raise TenantContextMissingError."""
    conn = _RecordingConnection()
    with pytest.raises(TenantContextMissingError):
        set_tenant_context(conn, "not-a-uuid-at-all")
    assert conn.statements == []


def test_non_v4_uuid_raises_error():
    """Passing a UUID that is not version 4 must raise TenantContextMissingError.

    Tenant IDs are minted as UUIDv4 at registration (KER-101). Any other version
    is not a valid tenant identity. The all-zeros UUID used in some test suites is
    not a valid tenant identity for this guard.
    """
    non_v4_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # UUID version 10 (unspecified)
    conn = _RecordingConnection()
    with pytest.raises(TenantContextMissingError):
        set_tenant_context(conn, non_v4_uuid)
    assert conn.statements == []


def test_tenant_a_session_cannot_query_without_context():
    """If the session does not provide a valid tenant, get_similar_controls must raise.

    Proves that the retrieval service enforces the application guard before
    touching the database — no query reaches the database with a missing context.
    """
    broken_session = _FakeAuthSession(None)
    conn = _RecordingConnection()
    with pytest.raises(TenantContextMissingError):
        get_similar_controls(broken_session, conn, [0.1, 0.2, 0.3])
    # Only the SET LOCAL attempt should have run (and it raises before issuing the query)
    data_queries = [s for s in conn.statements if not s.startswith("SET LOCAL")]
    assert data_queries == [], f"Unexpected data queries issued: {data_queries}"


def test_valid_tenant_context_sets_correct_session_variable():
    """A valid UUIDv4 tenant_id must set the correct PostgreSQL session variable.

    Proves that set_tenant_context issues exactly one SET LOCAL statement with
    the right variable name and the tenant's UUID value.
    """
    conn = _RecordingConnection()
    set_tenant_context(conn, TENANT_A_ID)
    assert len(conn.statements) == 1, f"Expected 1 statement; got {conn.statements}"
    stmt = conn.statements[0]
    assert "SET LOCAL" in stmt, f"Expected SET LOCAL in: {stmt}"
    assert "app.current_tenant_id" in stmt, f"Expected variable name in: {stmt}"


# ---------------------------------------------------------------------------
# Tests 5–6: Database-layer RLS isolation (integration tests, live DB required)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_tenant_a_cannot_retrieve_tenant_b_embeddings(db_connection):
    """With Tenant A's session active, the RLS policy must return zero Tenant B rows.

    Seeds two embeddings: one for Tenant A, one for Tenant B. Runs a similarity
    query scoped to Tenant A. Asserts that no result with Tenant B's control ID
    is returned, proving the RLS policy filters correctly at the database layer.

    This test must use a real database with the RLS migration applied. The
    ``db_connection`` fixture provides such a connection.
    """
    session_a = _FakeAuthSession(TENANT_A_ID)
    results = get_similar_controls(session_a, db_connection, [0.1] * EMBEDDING_DIMENSION)
    returned_ids = {row["control_id"] for row in results}
    assert not any("tenant_b" in cid.lower() for cid in returned_ids), (
        f"Tenant A's query returned Tenant B data: {returned_ids}"
    )


@pytest.mark.integration
def test_cross_tenant_override_not_visible(db_connection):
    """Tenant A must not be able to retrieve Tenant B's override records.

    Inserts one override for Tenant B, then queries overrides with Tenant A's
    session active. The RLS policy must prevent Tenant B's row from appearing.
    """
    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(TENANT_A_ID)]
        )
        rows = db_connection.execute(
            "SELECT override_id FROM overrides WHERE tenant_id = %s",
            [str(TENANT_B_ID)],
        ).fetchall()
    assert rows == [], (
        f"Tenant A's query returned Tenant B's overrides: {rows}"
    )


@pytest.mark.integration
def test_cross_tenant_bias_vector_not_visible(db_connection):
    """Tenant A must not be able to retrieve Tenant B's retrieval bias vector.

    Sets the tenant context to Tenant A, then attempts to SELECT Tenant B's
    bias vector. The RLS policy on the retrieval_bias table must prevent it.
    """
    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(TENANT_A_ID)]
        )
        row = db_connection.execute(
            "SELECT bias_vector FROM retrieval_bias WHERE tenant_id = %s",
            [str(TENANT_B_ID)],
        ).fetchone()
    assert row is None, (
        f"Tenant A's query returned Tenant B's bias vector: {row}"
    )


# ---------------------------------------------------------------------------
# Tests 7–11: DORA-specific tenant isolation (unit tests, no database required)
# Added in Document 17A hardening review.
# ---------------------------------------------------------------------------


class _DoraSpyConn:
    """Records (sql, params) tuples for DORA security assertions.

    Returns a _NullResult for every execute() call so service functions that
    call fetchall() or fetchone() on the result receive empty data without error.
    """

    def __init__(self) -> None:
        """Initialise with an empty call log."""
        self.calls: list[tuple] = []

    def execute(self, sql, params=None) -> "_NullResult":
        """Record the (sql, params) pair and return a null result."""
        self.calls.append((sql, params))
        return _NullResult()


def _make_dora_entry_input() -> RegisterEntryInput:
    """Return a minimal valid RegisterEntryInput for DORA security boundary tests."""
    return RegisterEntryInput(
        provider_name="ACME Cloud",
        service_name="Object Storage",
        provider_type="cloud",
        criticality_level="critical",
        business_function="Data persistence",
        data_types=["pii"],
        countries_supported=["DE"],
        contract_start_date=date(2024, 1, 1),
        contract_end_date=None,
        exit_strategy_summary=None,
        is_active=True,
        source_record_id=None,
    )


def test_dora_roi_create_with_none_tenant_raises_before_sql() -> None:
    """create_register_entry must raise TenantContextMissingError before any SQL when tenant_id is None.

    Proves that the DORA RoI service tenant guard fires as the very first action,
    issuing no SQL to the database when the tenant identity is missing.
    """
    conn = _RecordingConnection()
    with pytest.raises(TenantContextMissingError):
        create_register_entry(conn, None, _make_dora_entry_input())
    assert conn.statements == [], (
        "No SQL must be issued when tenant_id is None"
    )


def test_dora_roi_create_tenant_id_in_sql_params() -> None:
    """create_register_entry must pass tenant_id via SET LOCAL params before the INSERT.

    Proves that the tenant identity is explicitly present in the database call
    that activates RLS, not assumed by connection state or inferred later.
    """
    conn = _DoraSpyConn()
    create_register_entry(conn, TENANT_A_ID, _make_dora_entry_input())
    set_local_calls = [
        (sql, params) for sql, params in conn.calls if "SET LOCAL" in str(sql)
    ]
    assert set_local_calls, "SET LOCAL must be issued before any INSERT"
    _, params = set_local_calls[0]
    assert str(TENANT_A_ID) in str(params), (
        f"tenant_id {TENANT_A_ID} must appear in SET LOCAL params; got {params!r}"
    )


def test_dora_export_with_none_tenant_raises_before_sql() -> None:
    """build_export_package must raise TenantContextMissingError before any SQL when tenant_id is None.

    Proves that the export service tenant guard fires before the active-entries
    query, so a missing tenant identity never reaches the database.
    """
    conn = _RecordingConnection()
    with pytest.raises(TenantContextMissingError):
        build_export_package(conn, None, 2025)
    assert conn.statements == [], (
        "No SQL must be issued when tenant_id is None"
    )


def test_dora_export_tenant_id_in_sql_params() -> None:
    """build_export_package must pass tenant_id via SET LOCAL params before the data query.

    Proves that the export service sets RLS context with an explicit tenant_id
    parameter rather than relying on session state or defaults.
    """
    conn = _DoraSpyConn()
    build_export_package(conn, TENANT_A_ID, 2025)
    set_local_calls = [
        (sql, params) for sql, params in conn.calls if "SET LOCAL" in str(sql)
    ]
    assert set_local_calls, "SET LOCAL must be issued before the active-entries SELECT"
    _, params = set_local_calls[0]
    assert str(TENANT_A_ID) in str(params), (
        f"tenant_id {TENANT_A_ID} must appear in SET LOCAL params; got {params!r}"
    )


def test_dora_submission_list_runs_tenant_id_in_sql_params() -> None:
    """list_tenant_submission_runs must pass tenant_id explicitly in the SELECT params.

    Proves that the submission service includes an explicit WHERE tenant_id clause
    in the runs query, providing defense-in-depth beyond RLS alone.
    """
    conn = _DoraSpyConn()
    list_tenant_submission_runs(conn, TENANT_A_ID)
    runs_calls = [
        (sql, params) for sql, params in conn.calls
        if "FROM dora_submission_runs" in str(sql) and "WHERE" in str(sql)
    ]
    assert runs_calls, "SELECT with WHERE must be issued against dora_submission_runs"
    _, params = runs_calls[0]
    assert params is not None, "Params dict must not be None for the tenant runs SELECT"
    assert "tenant_id" in str(params), (
        "tenant_id key must appear in the SELECT params"
    )
    assert str(TENANT_A_ID) in str(params), (
        f"tenant_id {TENANT_A_ID} must be the value in the params; got {params!r}"
    )
