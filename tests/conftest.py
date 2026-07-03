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
Requires DATABASE_URL environment variable pointing to a live PostgreSQL instance
with all migrations applied:

    DATABASE_URL=postgresql://user:pass@host/db \\
        pytest tests/security/test_tenant_isolation.py -m integration -v

Tests are skipped automatically if DATABASE_URL is not set or if the required
Python packages (psycopg2) are not installed.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid

import pytest

from config.constants import EMBEDDING_DIMENSION

try:
    import psycopg2
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

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
def db_connection() -> _DbConnection:
    """Yield a live database connection with Tenant A and Tenant B rows seeded.

    Requires ``DATABASE_URL`` in the environment; skips the test automatically
    if absent. Seeds both tenant rows, one embedding per tenant, one Tenant B
    override, and one Tenant B retrieval_bias row before the test runs. Deletes
    all seeded rows after the test completes, in foreign-key-safe order.

    The seeded Tenant B data is intentionally detectable: the control_id contains
    the string "tenant_b" so assertions like ``assert "tenant_b" not in results``
    are non-vacuous — they prove RLS blocked a real row, not an empty table.
    """
    if not _PSYCOPG2_AVAILABLE:
        pytest.skip("psycopg2 not installed — skipping integration test")

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set — skipping integration test")

    raw_conn = psycopg2.connect(database_url)
    raw_conn.autocommit = False
    conn = _DbConnection(raw_conn)

    _teardown_seed_data(conn)
    conn.commit()
    _seed_integration_data(conn)
    conn.commit()

    yield conn

    conn.rollback()
    _teardown_seed_data(conn)
    conn.commit()
    raw_conn.close()


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

    audit_log is append-only (KER-107): its trigger must be disabled for the
    cleanup DELETE and re-enabled immediately after. Both ALTERs run inside the
    caller's transaction, so a failed teardown rolls back to trigger-enabled.

    FORCE RLS (migration 018) means even the owner role only sees one tenant's
    rows at a time, so the cleanup iterates the tenants and deletes each
    tenant's rows under that tenant's context.
    """
    conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only")
    for tenant_id in (TENANT_A_ID, TENANT_B_ID):
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        for table in (
            "audit_log", "overrides", "retrieval_bias", "tenant_embeddings",
            "context_records", "remediation_tasks", "remediation_routing_rules",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE tenant_id = %s",
                [str(tenant_id)],
            )
    conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only")
    conn.execute(
        "DELETE FROM tenants WHERE tenant_id IN (%s, %s)",
        [str(TENANT_A_ID), str(TENANT_B_ID)],
    )
