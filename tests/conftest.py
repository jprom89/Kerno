"""Shared fixtures for Kerno's integration test suite.

Plain-English summary
---------------------
Integration tests require a live PostgreSQL database with all migrations applied.
This file provides three fixtures that every integration test file may use:

  ``db_connection``  — A live database connection with seed data for two test
                        tenants. Each test receives a fresh copy of the seed rows
                        and the rows are cleaned up after each test completes.
  ``tenant_a_id``    — The fixed UUIDv4 for Tenant A.
  ``tenant_b_id``    — The fixed UUIDv4 for Tenant B.

The ``_DbConnection`` wrapper bridges two parameter-style conventions that exist
in the codebase: ``%s`` positional (used in ``rls.py``) and ``:name`` named (used
in the service layer). Both styles reach psycopg2 correctly through the wrapper's
``execute()`` method.

How to run or test
------------------
Live-database tests run ONLY against the owner-approved disposable database
kerno_test (TEST-SAFETY-001, docs/test_database_runbook.md), configured with
KERNO_TEST_DATABASE_URL and KERNO_TEST_DATABASE_APPROVAL in the environment or
in the gitignored .env.test. DATABASE_URL and the ordinary .env are never test
authorisation:

    python -m pytest                            # unit tests; live tests skip, named
    python -m pytest --require-live-database    # live validation; fails if not configured

The first thing this file does, before any repository import, is establish
that boundary (tests/_database_safety.py). Every psycopg2.connect in the
process — fixtures, the tests' own sessions, SQLAlchemy engines, the app's
pool — is refused unless it is to the approved target while this process
provably holds the exclusive lock it takes before its first write. The lock
is re-proved for every new connection, before db_connection's own seeding
and cleanup, and once more at the end of the session.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid

import pytest

# ── Test-database boundary — must precede every repository import ──────────
from tests import _database_safety as test_database_safety

_TEST_DATABASE = test_database_safety.bootstrap_test_process()

from config.constants import EMBEDDING_DIMENSION  # noqa: E402 — after the boundary on purpose

try:
    import psycopg2  # noqa: E402
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

# Integration-marked tests that skipped in a --require-live-database run.
_LIVE_TEST_SKIPS: list[str] = []
_REQUIRE_LIVE = {"enabled": False}
# Set when the lock could not be re-proved at the end of the session.
_FINAL_EXCLUSIVITY_PROBLEMS: list[str] = []

# Fixed deterministic UUIDv4 identifiers for the two test tenants.
# Using constants (not uuid4()) makes test failure messages readable:
# "tenant_a..." clearly identifies the intended tenant.
TENANT_A_ID = uuid.UUID("a0000000-0000-4000-a000-000000000001")
TENANT_B_ID = uuid.UUID("b0000000-0000-4000-b000-000000000002")

_TENANT_A_EMBEDDING = [0.1] * EMBEDDING_DIMENSION
_TENANT_B_EMBEDDING = [0.9] * EMBEDDING_DIMENSION
_ZERO_VECTOR = [0.0] * EMBEDDING_DIMENSION

# Named-parameter pattern: matches :word_identifier in SQL.
# The negative lookbehind (?<!:) prevents matching the second colon in
# PostgreSQL type-cast syntax (e.g. '1970-01-01'::timestamptz would otherwise
# yield a spurious match on "timestamptz").
_NAMED_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_]\w*)")


def _format_vector(values: list[float]) -> str:
    """Return a pgvector-compatible string representation of a float list.

    pgvector accepts the format ``[v1,v2,...,vN]`` when cast to vector.
    """
    return "[" + ",".join(str(v) for v in values) + "]"


def _is_vector_value(value: object) -> bool:
    """Return True if ``value`` looks like an embedding vector.

    A vector is a large list of numeric values. The minimum length check
    avoids treating small positional lists (e.g. ``[tenant_id, limit]``) as
    vectors. The numeric check avoids treating string lists as vectors.
    """
    return (
        isinstance(value, list)
        and len(value) >= EMBEDDING_DIMENSION
        and all(isinstance(v, (int, float)) for v in value)
    )


def _convert_named_params(sql: str, params: dict) -> tuple[str, dict]:
    """Convert ``:name`` placeholders to ``%(name)s`` for psycopg2.

    Vector-valued parameters (large float lists) are additionally converted to
    pgvector string format and the placeholder is suffixed with ``::vector`` so
    PostgreSQL applies the correct type cast. Non-vector params are passed
    through unchanged.

    Returns the converted SQL string and the adapted params dict.
    """
    adapted: dict = {}

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        value = params.get(name)
        if _is_vector_value(value):
            adapted[name] = _format_vector(value)
            return f"%({name})s::vector"
        adapted[name] = value
        return f"%({name})s"

    converted_sql = _NAMED_PARAM_RE.sub(_replace, sql)
    return converted_sql, adapted


class _CursorResult:
    """Wraps a psycopg2 cursor to expose the fetchall/fetchone interface.

    The application services expect a result object with ``fetchall()`` and
    ``fetchone()`` methods. psycopg2 cursors satisfy this directly, but wrapping
    them here lets the wrapper's ``execute()`` return a uniform result type.
    """

    def __init__(self, cursor) -> None:
        """Store the psycopg2 cursor."""
        self._cursor = cursor

    def fetchall(self) -> list:
        """Return all remaining rows, or an empty list for non-SELECT statements."""
        try:
            return self._cursor.fetchall()
        except Exception:
            return []

    def fetchone(self):
        """Return the next row, or None for non-SELECT statements."""
        try:
            return self._cursor.fetchone()
        except Exception:
            return None


class _DbConnection:
    """psycopg2 connection wrapper that matches the application service interface.

    The application codebase uses two SQL parameter styles:
      - ``%s`` with a list (psycopg2 native, used in rls.py)
      - ``:name`` with a dict (SQLAlchemy-style, used in service files)

    This wrapper normalises both to psycopg2's ``%(name)s`` / positional ``%s``
    style so integration tests can exercise real service functions against a live
    database without modifying the service code.

    The ``transaction()`` context manager is required because PostgreSQL's
    ``SET LOCAL`` only scopes the session variable to the current transaction.
    """

    def __init__(self, raw_conn) -> None:
        """Store the underlying psycopg2 connection."""
        self._conn = raw_conn

    def execute(self, sql: str, params=None) -> _CursorResult:
        """Execute ``sql`` against the live database and return a result wrapper.

        Accepts either a list (positional ``%s`` style) or a dict (``:name``
        style) for ``params``. Dict params are converted to psycopg2 ``%(name)s``
        style, with automatic ``::vector`` casting for float-list values.
        """
        cursor = self._conn.cursor()
        if isinstance(params, dict):
            converted_sql, adapted_params = _convert_named_params(sql, params)
            cursor.execute(converted_sql, adapted_params)
        else:
            cursor.execute(sql, params)
        return _CursorResult(cursor)

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    def rollback(self) -> None:
        """Roll back the current transaction."""
        self._conn.rollback()

    @contextlib.contextmanager
    def transaction(self):
        """Open a transaction block, committing on clean exit and rolling back on error.

        PostgreSQL's ``SET LOCAL`` scopes a session variable to the current
        transaction only. Every integration test that calls ``SET LOCAL`` must
        do so inside a ``transaction()`` block so the scope is correctly bounded.
        """
        try:
            yield self
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise


@pytest.fixture(scope="session")
def tenant_a_id() -> uuid.UUID:
    """Return the fixed UUIDv4 for Tenant A used across all integration tests."""
    return TENANT_A_ID


@pytest.fixture(scope="session")
def tenant_b_id() -> uuid.UUID:
    """Return the fixed UUIDv4 for Tenant B used across all integration tests."""
    return TENANT_B_ID


@pytest.fixture
def db_connection(request) -> _DbConnection:
    """Yield a live connection to the approved test database with Tenant A and Tenant B rows seeded.

    Skips, naming the reason, when no test database is configured (fails
    instead under --require-live-database). Before the first write in the run
    it verifies the live target read-only and takes the exclusive lock; before
    every seeding and every cleanup it re-proves the lock is still held, and
    stops the whole run if it is not. Seeds both tenant rows, one embedding
    per tenant, one Tenant B override, and one Tenant B retrieval_bias row;
    deletes all seeded rows afterwards, in foreign-key-safe order.

    The seeded Tenant B data is intentionally detectable: the control_id contains
    the string "tenant_b" so assertions like ``assert "tenant_b" not in results``
    are non-vacuous — they prove RLS blocked a real row, not an empty table.
    """
    raw_conn = open_guarded_fixture_connection(request.config)
    raw_conn.autocommit = False
    conn = _DbConnection(raw_conn)

    require_exclusive_session()
    _teardown_seed_data(conn)
    conn.commit()
    _seed_integration_data(conn)
    conn.commit()

    yield conn

    conn.rollback()
    require_exclusive_session()
    _teardown_seed_data(conn)
    conn.commit()
    raw_conn.close()


def open_guarded_fixture_connection(config, state=None):
    """Return a raw connection to the approved test database, or skip/fail/stop — never fall back.

    No target configured: skip with the reason, or fail under
    --require-live-database. Target rejected or busy: stop the whole run
    before any write, because every later test would meet the same refusal.
    """
    state = state or _TEST_DATABASE
    if not _PSYCOPG2_AVAILABLE:
        _skip_or_fail(config, "psycopg2 is not installed")
    if state.target is None:
        _skip_or_fail(config, "no approved test database is configured (docs/test_database_runbook.md)")
    try:
        test_database_safety.ensure_exclusive_session("pytest", state=state)
    except test_database_safety.DatabaseTargetBusy as exc:
        pytest.exit(f"test database busy: {exc}", returncode=pytest.ExitCode.INTERRUPTED)
    except test_database_safety.DatabaseExclusivityLost as exc:
        pytest.exit(f"test database exclusivity lost: {exc}", returncode=pytest.ExitCode.INTERRUPTED)
    except test_database_safety.KernoTestDatabaseError as exc:
        pytest.exit(f"test database refused: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)
    return psycopg2.connect(state.target.url)


def require_exclusive_session(state=None) -> None:
    """Stop the whole run unless the exclusive lock is provably still held."""
    session = (state or _TEST_DATABASE).session
    try:
        if session is None:
            raise test_database_safety.DatabaseExclusivityLost("the exclusive session was never opened.")
        session.assert_held()
    except test_database_safety.DatabaseExclusivityLost as exc:
        pytest.exit(f"test database exclusivity lost: {exc}", returncode=pytest.ExitCode.INTERRUPTED)


def _skip_or_fail(config, reason: str) -> None:
    """Skip a live-database test with its reason, or fail it when live validation was required."""
    if config.getoption("require_live_database"):
        pytest.fail(f"live database required but unavailable: {reason}")
    pytest.skip(f"live database test skipped: {reason}")


# ---------------------------------------------------------------------------
# Test-database boundary hooks
# ---------------------------------------------------------------------------


def pytest_addoption(parser) -> None:
    """Register --require-live-database: the explicit request for live validation."""
    parser.addoption(
        "--require-live-database",
        action="store_true",
        default=False,
        help="fail, rather than skip, when the approved kerno_test database is not configured, "
        "and fail the run if any integration-marked test is skipped",
    )


def pytest_configure(config) -> None:
    """Fail the run on invalid test settings, missing required settings, or parallel workers."""
    _REQUIRE_LIVE["enabled"] = bool(config.getoption("require_live_database"))
    if _TEST_DATABASE.problem:
        raise pytest.UsageError(f"test database configuration refused: {_TEST_DATABASE.problem}")
    if _REQUIRE_LIVE["enabled"] and _TEST_DATABASE.target is None:
        raise pytest.UsageError(
            "--require-live-database was given but no approved test database is configured: set "
            f"{test_database_safety.TEST_DATABASE_URL_VARIABLE} and "
            f"{test_database_safety.TEST_DATABASE_APPROVAL_VARIABLE} (docs/test_database_runbook.md)."
        )
    if _TEST_DATABASE.target is not None and _parallel_workers_requested(config):
        raise pytest.UsageError(
            "parallel test workers are not supported against the test database: the fixture "
            "tenants are fixed, so concurrent workers would collide."
        )


def _parallel_workers_requested(config) -> bool:
    """True when pytest-xdist (or a worker environment) would run tests in parallel."""
    workers = getattr(config.option, "numprocesses", None)
    distribution = getattr(config.option, "dist", "no")
    return bool(os.environ.get("PYTEST_XDIST_WORKER") or workers or distribution not in (None, "no"))


def pytest_runtest_logreport(report) -> None:
    """Remember integration-marked tests that skipped, so a required live run cannot hide them."""
    if report.skipped and "integration" in report.keywords:
        _LIVE_TEST_SKIPS.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    """Re-prove the lock one last time, and fail a required live run in which an integration test skipped.

    Every fixture cleanup has run by now. If exclusivity cannot be proved,
    the last cleanups ran without it, so the run is not reported as a success.
    """
    exclusive = _TEST_DATABASE.session
    if exclusive is not None:
        try:
            exclusive.assert_held()
        except test_database_safety.DatabaseExclusivityLost as exc:
            _FINAL_EXCLUSIVITY_PROBLEMS.append(str(exc))
            session.exitstatus = pytest.ExitCode.INTERRUPTED
            return
    if _REQUIRE_LIVE["enabled"] and _LIVE_TEST_SKIPS and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Say which database, if any, this run was allowed to touch — identity only, never credentials."""
    target = _TEST_DATABASE.target
    if target is None:
        terminalreporter.write_line("test database: none configured - live-database tests were skipped")
    else:
        terminalreporter.write_line(f"test database: {target.identity} (settings from {_TEST_DATABASE.source})")
    for problem in _FINAL_EXCLUSIVITY_PROBLEMS:
        terminalreporter.write_line(f"FAILED: test database exclusivity lost at the end of the run: {problem}", red=True)
    if _REQUIRE_LIVE["enabled"] and _LIVE_TEST_SKIPS:
        terminalreporter.write_line(
            f"FAILED: {len(_LIVE_TEST_SKIPS)} integration test(s) skipped in a required live run:", red=True
        )
        for nodeid in _LIVE_TEST_SKIPS:
            terminalreporter.write_line(f"  {nodeid}", red=True)


def pytest_unconfigure(config) -> None:
    """Release the exclusive lock and its connection after every fixture cleanup has run."""
    test_database_safety.release_exclusive_session()


def _seed_integration_data(conn: _DbConnection) -> None:
    """Insert both tenants and their associated test rows.

    Tenant B's control_id deliberately contains "tenant_b" so RLS-blocking
    tests can detect a real data leak rather than an empty-table result.
    """
    conn.execute(
        """
        INSERT INTO tenants (tenant_id, display_name, is_active)
        VALUES (%s, %s, true), (%s, %s, true)
        ON CONFLICT (tenant_id) DO NOTHING
        """,
        [
            str(TENANT_A_ID), "Integration Test Tenant A",
            str(TENANT_B_ID), "Integration Test Tenant B",
        ],
    )
    # FORCE RLS (migration 018): even the table-owner role obeys the tenant
    # policies, so each tenant's rows must be inserted under that tenant's context.
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(TENANT_A_ID)])
    conn.execute(
        """
        INSERT INTO tenant_embeddings (tenant_id, control_id, embedding)
        VALUES (%s, %s, %s::vector)
        """,
        [str(TENANT_A_ID), "tenant_a_control_001", _format_vector(_TENANT_A_EMBEDDING)],
    )
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(TENANT_B_ID)])
    conn.execute(
        """
        INSERT INTO tenant_embeddings (tenant_id, control_id, embedding)
        VALUES (%s, %s, %s::vector)
        """,
        [str(TENANT_B_ID), "tenant_b_control_001", _format_vector(_TENANT_B_EMBEDDING)],
    )
    _seed_tenant_b_supplemental(conn)


def _seed_tenant_b_supplemental(conn: _DbConnection) -> None:
    """Insert Tenant B's override and retrieval_bias rows.

    These are the rows the RLS cross-tenant tests check for: if RLS is working,
    Tenant A's queries must not return either of these rows even though they
    exist in the database. Called by ``_seed_integration_data``.
    """
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(TENANT_B_ID)])
    conn.execute(
        """
        INSERT INTO overrides
            (tenant_id, reviewer_id, reviewer_role, action_type,
             original_control_id, reviewer_confidence_weight)
        VALUES (%s, %s, 'vciso', 'approve', 'tenant_b_control_001', 1.0)
        """,
        [str(TENANT_B_ID), str(uuid.uuid4())],
    )
    conn.execute(
        """
        INSERT INTO retrieval_bias (tenant_id, bias_vector, override_count)
        VALUES (%s, %s::vector, 0)
        ON CONFLICT (tenant_id) DO NOTHING
        """,
        [str(TENANT_B_ID), _format_vector(_ZERO_VECTOR)],
    )


def _teardown_seed_data(conn: _DbConnection) -> None:
    """Delete all rows seeded by ``_seed_integration_data``, in FK-safe order.

    Child tables must be deleted before the parent tenants table to satisfy
    the foreign key constraints. Each DELETE is scoped to both test tenant IDs
    so only rows owned by this fixture are removed.

    Two tables refuse deletes and need their guards lifted for the cleanup.
    audit_log is append-only outright (KER-107). ai_decision_log refuses deletes
    only for rows inside the retention window (migration 023) — and every row a
    test seeds is by definition inside it, so the window guard has to come off
    too. All four ALTERs run inside the caller's transaction, so a failed
    teardown rolls back to triggers-enabled.

    FORCE RLS (migration 018) means even the owner role only sees one tenant's
    rows at a time, so the cleanup iterates the tenants and deletes each
    tenant's rows under that tenant's context.

    control_evidence_links is deleted FIRST and by subquery, not by the
    standard pattern: it holds a foreign key to context_records (so it must go
    before them) and it has NO tenant_id column of its own — its tenant
    identity is inherited from the record it points at.
    """
    conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only")
    conn.execute(
        "ALTER TABLE ai_decision_log DISABLE TRIGGER ai_decision_log_retain_window"
    )
    for tenant_id in (TENANT_A_ID, TENANT_B_ID):
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        conn.execute(
            "DELETE FROM control_evidence_links WHERE record_id IN "
            "(SELECT record_id FROM context_records WHERE tenant_id = %s)",
            [str(tenant_id)],
        )
        for table in (
            "audit_log", "ai_decision_log", "overrides", "retrieval_bias",
            "tenant_embeddings", "context_records", "remediation_tasks",
            "remediation_routing_rules",
            # KER-409 — both were missing, so DORA rows written by a test
            # survived it. That made any later test that reads the register
            # order-dependent (an "empty register" was only empty if it ran
            # first), and it leaked regulatory-looking rows into the dev tenant.
            # Runs go before entries: neither references the other, but both
            # must precede the tenants DELETE below.
            "dora_submission_runs", "dora_register_entries",
            # DORA-V2-002A (migration 026) — parties reference both a contract
            # and an organisation, so they go first; contracts before the
            # organisation tables only by convention (no FK between them).
            "dora_contract_parties", "dora_contracts",
            # DORA-V2-001 (migration 025) — child-first: roles and identifiers
            # carry a composite FK to organisations, and all three carry an FK
            # to tenants.
            "dora_organization_roles", "dora_organization_identifiers",
            "dora_organizations",
            # KER-205 (migration 022) — both hold FKs to tenants, so the final
            # DELETE FROM tenants fails if they are left behind.
            "webhook_ingest_dedup", "webhook_registrations",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = %s",
                [str(tenant_id)],
            )
    conn.execute(
        "ALTER TABLE ai_decision_log ENABLE TRIGGER ai_decision_log_retain_window"
    )
    conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only")
    conn.execute(
        "DELETE FROM tenants WHERE tenant_id IN (%s, %s)",
        [str(TENANT_A_ID), str(TENANT_B_ID)],
    )
