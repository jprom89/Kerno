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
    python -m pytest --require-live-database --require-runtime-identity
                                                # C2 acceptance: owner and runtime, zero skips

The first thing this file does, before any repository import, is establish
that boundary (tests/_database_safety.py). Before its first write the run
opens one guard connection, verifies the live database's identity and
approval from the catalogs on it, and takes the exclusive lock. After that,
every psycopg2.connect in the process — fixtures, the tests' own sessions,
SQLAlchemy engines, the app's pool — is refused unless its effective
parameters match the approved target and the guard connection re-proves the
lock; the catalog check is not repeated per connection. The lock is also
re-proved before db_connection's own seeding and cleanup, and once more at
the end of the session.

Runtime identity (C2, PILOT-READY-002). Tests that use ``runtime_connection``
or ``runtime_pool`` run as the non-owner login kerno_test_app, configured with
KERNO_TEST_RUNTIME_DATABASE_URL and KERNO_TEST_RUNTIME_DATABASE_APPROVAL (or
the gitignored .env.test.runtime). They are marked ``runtime_identity`` and
follow one behaviour table (docs/pilot_readiness_kickoff.md §2.3 item 8):
not configured skips them by name, or fails under --require-runtime-identity;
configured but refused at login fails them (a runtime-setup failure, nonzero);
anything unsafe stops the run. Owner fixtures keep db_connection, and
DATABASE_URL stays the owner URL, so nothing an owner path does can count as
runtime evidence.
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid

import pytest

# ── Test-database boundary — must precede every repository import ──────────
from tests import _database_safety as test_database_safety

_TEST_DATABASE = test_database_safety.bootstrap_test_process(read_runtime=True)

from config.constants import EMBEDDING_DIMENSION  # noqa: E402 — after the boundary on purpose

try:
    import psycopg2  # noqa: E402
    import psycopg2.pool  # noqa: E402
    _PSYCOPG2_AVAILABLE = True
except ImportError:
    _PSYCOPG2_AVAILABLE = False

# Integration-marked tests that skipped in a --require-live-database run.
_LIVE_TEST_SKIPS: list[str] = []
_REQUIRE_LIVE = {"enabled": False}
# Runtime-identity tests that skipped, and every other skip: under
# --require-runtime-identity (final acceptance) any skip at all fails the run.
_RUNTIME_TEST_SKIPS: list[str] = []
_OTHER_SKIPS: list[str] = []
_REQUIRE_RUNTIME = {"enabled": False}
_RUNTIME_FIXTURES = frozenset({"runtime_connection", "runtime_pool"})
# One TestClient request at a time, plus headroom for a nested lease.
RUNTIME_POOL_MAX_CONNECTIONS = 4
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
# Runtime identity (C2): the non-owner login, never an owner fallback
# ---------------------------------------------------------------------------


def require_runtime_identity(config, state=None):
    """Return the verified runtime target, or skip, fail or stop the run per the §2.3 item 8 behaviour table.

    Not configured: skip by name, or fail under --require-runtime-identity.
    Configured but the login is refused: fail this test (a runtime-setup
    failure, nonzero with or without the flag). Unsafe identity, unsafe
    settings or an unproved lock: stop the run. Must be called after the
    pytest workflow's exclusive session is held (db_connection first).
    """
    state = state or _TEST_DATABASE
    if state.runtime_target is None:
        if config.getoption("require_runtime_identity"):
            pytest.fail("runtime identity required but not configured")
        pytest.skip("runtime identity not configured")
    try:
        return test_database_safety.verify_runtime_identity(state=state)
    except test_database_safety.RuntimeIdentityNotReady as exc:
        pytest.fail(f"runtime identity setup failure: {exc}")
    except test_database_safety.DatabaseExclusivityLost as exc:
        pytest.exit(f"test database exclusivity lost: {exc}", returncode=pytest.ExitCode.INTERRUPTED)
    except test_database_safety.KernoTestDatabaseError as exc:
        pytest.exit(f"runtime identity refused: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)


def _require_runtime_session_user(raw_conn) -> None:
    """Fail unless this physical connection's session_user is the fixed runtime role; leave no transaction open."""
    cursor = raw_conn.cursor()
    try:
        cursor.execute("SELECT session_user, current_user")
        session_user, current_user = cursor.fetchone()
    finally:
        raw_conn.rollback()
    expected = test_database_safety.TEST_RUNTIME_ROLE_NAME
    if session_user != expected or current_user != expected:
        pytest.fail(f"a runtime connection reported session_user {session_user!r}, not {expected!r}")


if _PSYCOPG2_AVAILABLE:

    class RuntimeIdentityPool(psycopg2.pool.ThreadedConnectionPool):
        """The application's pool type, opened as kerno_test_app, checking each new physical connection once."""

        def _connect(self, key=None):
            """Open one physical connection through the guarded psycopg2.connect, then check its identity."""
            raw_conn = super()._connect(key)
            _require_runtime_session_user(raw_conn)
            return raw_conn


@pytest.fixture
def runtime_connection(request):
    """Yield a raw-SQL connection as kerno_test_app, for negative probes; owner seeding uses db_connection.

    Requests db_connection first, so the owner fixture seeds and the
    exclusive session is held before the runtime identity is verified, and
    the runtime connection is closed before the owner's cleanup runs.
    """
    request.getfixturevalue("db_connection")
    runtime = require_runtime_identity(request.config)
    raw_conn = psycopg2.connect(runtime.url, application_name="kerno-test-runtime")
    raw_conn.autocommit = False
    _require_runtime_session_user(raw_conn)
    yield _DbConnection(raw_conn)
    raw_conn.rollback()
    raw_conn.close()


@pytest.fixture
def runtime_pool(request, monkeypatch):
    """Point the application at a real runtime pool, so get_conn and pooled_transaction run as kerno_test_app.

    Replaces both src.api.dependencies._pool and _get_pool, so a reset of the
    global still leases from the runtime pool; no dependency (get_conn,
    get_tenant_id, get_role, get_reviewer_id) is overridden, so requests need
    real JWTs. Afterwards asserts the application still held this pool.
    """
    request.getfixturevalue("db_connection")
    runtime = require_runtime_identity(request.config)
    from src.api import dependencies

    pool = RuntimeIdentityPool(1, RUNTIME_POOL_MAX_CONNECTIONS, dsn=runtime.url)

    def runtime_pool_only():
        """Return the runtime pool, whatever the module global holds."""
        return pool

    monkeypatch.setattr(dependencies, "_pool", pool)
    monkeypatch.setattr(dependencies, "_get_pool", runtime_pool_only)
    yield pool
    still_runtime = dependencies._pool is pool
    pool.closeall()
    assert still_runtime, "the application pool was replaced during a runtime-identity test"


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
    parser.addoption(
        "--require-runtime-identity",
        action="store_true",
        default=False,
        help="C2 acceptance: with --require-live-database, fail unless the kerno_test_app runtime "
        "identity is configured and verified, and fail the run if any test at all is skipped",
    )


def pytest_configure(config) -> None:
    """Fail the run on invalid test settings, missing required settings, or parallel workers."""
    _REQUIRE_LIVE["enabled"] = bool(config.getoption("require_live_database"))
    _REQUIRE_RUNTIME["enabled"] = bool(config.getoption("require_runtime_identity"))
    if _REQUIRE_RUNTIME["enabled"] and not _REQUIRE_LIVE["enabled"]:
        raise pytest.UsageError("--require-runtime-identity must be combined with --require-live-database.")
    if _TEST_DATABASE.problem:
        raise pytest.UsageError(f"test database configuration refused: {_TEST_DATABASE.problem}")
    if _REQUIRE_LIVE["enabled"] and _TEST_DATABASE.target is None:
        raise pytest.UsageError(
            "--require-live-database was given but no approved test database is configured: set "
            f"{test_database_safety.TEST_DATABASE_URL_VARIABLE} and "
            f"{test_database_safety.TEST_DATABASE_APPROVAL_VARIABLE} (docs/test_database_runbook.md)."
        )
    if _REQUIRE_RUNTIME["enabled"] and _TEST_DATABASE.runtime_target is None:
        raise pytest.UsageError(
            "--require-runtime-identity was given but no runtime identity is configured: set "
            f"{test_database_safety.TEST_RUNTIME_DATABASE_URL_VARIABLE} and "
            f"{test_database_safety.TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE} (docs/test_database_runbook.md)."
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


def pytest_collection_modifyitems(config, items) -> None:
    """Mark every test that uses a runtime fixture as runtime_identity and integration, so its skips are counted."""
    for item in items:
        if _RUNTIME_FIXTURES & set(item.fixturenames):
            item.add_marker(pytest.mark.runtime_identity)
            item.add_marker(pytest.mark.integration)


def pytest_runtest_logreport(report) -> None:
    """Remember every skip by kind, so a required live or runtime run cannot hide one.

    Runtime-identity skips are kept apart from owner integration skips: a
    --require-live-database run without runtime settings is the setup-stage
    owner regression run, in which runtime tests skip by name (row 1).
    """
    if not report.skipped:
        return
    if "runtime_identity" in report.keywords:
        _RUNTIME_TEST_SKIPS.append(report.nodeid)
    elif "integration" in report.keywords:
        _LIVE_TEST_SKIPS.append(report.nodeid)
    else:
        _OTHER_SKIPS.append(report.nodeid)


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
    if exitstatus == pytest.ExitCode.OK and _required_run_skipped_tests():
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def _required_run_skipped_tests() -> list[str]:
    """Return the skips that make a required run fail: owner integration skips, and under the runtime flag, every skip."""
    if _REQUIRE_RUNTIME["enabled"]:
        return _LIVE_TEST_SKIPS + _RUNTIME_TEST_SKIPS + _OTHER_SKIPS
    if _REQUIRE_LIVE["enabled"]:
        return list(_LIVE_TEST_SKIPS)
    return []


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Say which database and identities, if any, this run was allowed to touch — identities only, never credentials."""
    target = _TEST_DATABASE.target
    if target is None:
        terminalreporter.write_line("test database: none configured - live-database tests were skipped")
    else:
        terminalreporter.write_line(f"test database: {target.identity} (settings from {_TEST_DATABASE.source})")
    terminalreporter.write_line(_runtime_identity_summary(), red=bool(_TEST_DATABASE.runtime_not_ready))
    for problem in _FINAL_EXCLUSIVITY_PROBLEMS:
        terminalreporter.write_line(f"FAILED: test database exclusivity lost at the end of the run: {problem}", red=True)
    skipped = _required_run_skipped_tests()
    if skipped:
        if _REQUIRE_RUNTIME["enabled"]:
            heading = f"FAILED: {len(skipped)} test(s) skipped in a required runtime-identity run (zero allowed):"
        else:
            heading = f"FAILED: {len(skipped)} integration test(s) skipped in a required live run:"
        terminalreporter.write_line(heading, red=True)
        for nodeid in skipped:
            terminalreporter.write_line(f"  {nodeid}", red=True)


def _runtime_identity_summary() -> str:
    """One line on the runtime identity: not configured, verified, not ready, or configured but unused."""
    runtime = _TEST_DATABASE.runtime_target
    if runtime is None:
        return "runtime identity: not configured - runtime-identity tests were skipped"
    if _TEST_DATABASE.runtime_verified:
        return f"runtime identity: {runtime.identity} verified (settings from {_TEST_DATABASE.runtime_source})"
    if _TEST_DATABASE.runtime_not_ready:
        return f"FAILED: runtime identity setup failure: {_TEST_DATABASE.runtime_not_ready}"
    return f"runtime identity: {runtime.identity} configured, not used in this run"


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
            # DORA-V2-002B (migration 027) — relationships reference contracts
            # at both ends, so they go before contracts.
            "dora_contract_relationships",
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
