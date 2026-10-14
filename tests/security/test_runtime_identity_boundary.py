"""test_runtime_identity_boundary.py — what the non-owner runtime login kerno_test_app can and cannot do.

What:  Live probes, run as kerno_test_app on the disposable kerno_test database
       (docs/pilot_readiness_kickoff.md §2.5): direct reads of users,
       webhook_registrations.signing_secret and the legacy tenants credential
       columns are denied; the strict bootstrap policies return nothing without
       a tenant context; tenant A's context never reads tenant B's rows; ledger
       and AI-log mutation is denied by privilege before any trigger runs;
       DDL, policy, RLS and trigger changes need the owner; the runtime cannot
       assume the owner role or run the grant procedures; H1 and H2 match only
       an exact key and return at most one row per call, and H1 still returns
       the password hash for an exact pair (the documented residual, pinned
       rather than hidden); PUBLIC holds nothing on the helpers, procedures,
       bootstrap schema or database; the runtime's effective privileges equal
       the full-profile matrix exactly, in both directions; it has no
       memberships and owns nothing; the profile lookup of migration 030
       answers 'full' for the runtime, nothing for any other login, takes no
       role argument, and leaves the assignment record unreadable and
       unwritable; and the API startup gate accepts the runtime pool for a
       full process and refuses the owner and a pilot process.
Why:   C2 (PILOT-READY-002) and PILOT-READY-003. The rest of the suite runs as
       the owner and is regression evidence only; this file is the non-owner
       boundary under the full grants. The matrix literals live in
       tests/security/runtime_privilege_matrices.py, shared with the pilot
       boundary and the transition tests. Owner seeding goes through
       db_connection and never counts as runtime evidence.
How:   python -m pytest tests/security/test_runtime_identity_boundary.py --require-live-database --require-runtime-identity
       Runs in the full-profile run (the default --runtime-profile full).
       Without runtime settings every live test here skips by name ("runtime
       identity not configured"), or fails under --require-runtime-identity.
"""

from __future__ import annotations

import functools
import re
import uuid

import psycopg2
import psycopg2.pool
import pytest

from src.api import dependencies, runtime_identity
from src.services.auth_service import hash_password
from tests import _database_safety as test_database_safety
from tests.security import runtime_privilege_matrices as matrices

RUNTIME_ROLE = test_database_safety.TEST_RUNTIME_ROLE_NAME
OWNER_ROLE = test_database_safety.TEST_ROLE_NAME
_INSUFFICIENT_PRIVILEGE = "42501"
_UNDEFINED_FUNCTION = "42883"
_H1_EMAIL = "c2-runtime-h1@example.test"
_H1_SECOND_EMAIL = "c2-runtime-h1-second@example.test"
_H1_PASSWORD = "c2-runtime-boundary-password"
_REGISTRATION_ID = "c2000000-0000-4000-c000-000000000001"
_REGISTRATION_SECRET = "c2-runtime-boundary-signing-secret"

_PROFILE_LOOKUP = "kerno_bootstrap.session_runtime_profile()"
_PROFILE_LOOKUP_SQL = f"SELECT {_PROFILE_LOOKUP}"
_PROFILE_LOOKUP_DEFINITION_SQL = """
SELECT p.prosecdef, p.provolatile, p.proconfig, pg_get_userbyid(p.proowner), p.pronargs, p.prorettype::regtype::text
FROM pg_proc AS p WHERE p.oid = 'kerno_bootstrap.session_runtime_profile()'::regprocedure
"""
_PROFILE_LOOKUP_ACL_SQL = """
SELECT pg_get_userbyid(acl.grantee), acl.privilege_type, acl.grantee = 0
FROM pg_proc AS p, aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) AS acl
WHERE p.oid = 'kerno_bootstrap.session_runtime_profile()'::regprocedure
ORDER BY 1
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _probe(connection, sql: str, params=None, tenant_id=None):
    """Run one statement as the runtime in its own transaction; return (rows, error) and always roll back."""
    raw_conn = connection._conn
    cursor = raw_conn.cursor()
    try:
        if tenant_id is not None:
            cursor.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        cursor.execute(sql, params)
        rows = cursor.fetchall() if cursor.description else []
        return rows, None
    except psycopg2.Error as exc:
        return None, exc
    finally:
        raw_conn.rollback()


def _rows(connection, sql: str, params=None, tenant_id=None) -> list:
    """Return the rows of a runtime statement that must succeed."""
    rows, error = _probe(connection, sql, params, tenant_id)
    assert error is None, f"expected success, got {type(error).__name__}: {error}"
    return rows


def _assert_denied(connection, sql: str, expected_fragment: str, tenant_id=None) -> None:
    """Assert the runtime statement fails with insufficient_privilege and the expected message fragment."""
    rows, error = _probe(connection, sql, tenant_id=tenant_id)
    assert error is not None, f"expected a privilege error, the statement succeeded: {sql}"
    assert error.pgcode == _INSUFFICIENT_PRIVILEGE, f"{error.pgcode}: {error}"
    assert expected_fragment in str(error), str(error)


def _owner_rows(db_connection, sql: str, params=None, tenant_id=None) -> list:
    """Read as the owner (fixture side, never runtime evidence)."""
    with db_connection.transaction():
        if tenant_id is not None:
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return db_connection.execute(sql, params).fetchall()


def _tenant_slug(db_connection, tenant_id) -> str:
    """The conftest tenant's slug, read as the owner."""
    return _owner_rows(db_connection, "SELECT tenant_slug FROM tenants WHERE tenant_id = %s", [str(tenant_id)])[0][0]


def _has_tenant_column(runtime_connection, table: str) -> bool:
    """True when the public table has a tenant_id column."""
    rows = _rows(
        runtime_connection,
        "SELECT 1 FROM pg_attribute WHERE attrelid = %s::regclass AND attname = 'tenant_id' AND NOT attisdropped",
        [f"public.{table}"],
    )
    return bool(rows)


def _runtime_fetch(runtime_connection):
    """A matrices fetch(sql, params) that runs each read as the runtime in its own rolled-back transaction."""
    return functools.partial(_rows, runtime_connection)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def h1_users(db_connection, tenant_a_id):
    """Seed two tenant A users as the owner; delete them before db_connection's own cleanup."""
    with db_connection.transaction():
        for email, active in ((_H1_EMAIL, True), (_H1_SECOND_EMAIL, False)):
            db_connection.execute(
                """INSERT INTO users (user_id, tenant_id, email, password_hash, role, is_active)
                   VALUES (%s, %s, %s, %s, 'vciso', %s)""",
                [str(uuid.uuid4()), str(tenant_a_id), email, hash_password(_H1_PASSWORD), active],
            )
    yield _tenant_slug(db_connection, tenant_a_id)
    with db_connection.transaction():
        db_connection.execute("DELETE FROM users WHERE tenant_id = %s", [str(tenant_a_id)])


@pytest.fixture
def registration(db_connection, tenant_a_id):
    """Seed one tenant A webhook registration as the owner; conftest deletes it with the tenant's rows."""
    with db_connection.transaction():
        db_connection.execute(
            """INSERT INTO webhook_registrations (id, tenant_id, source_system, signing_secret, is_active)
               VALUES (%s, %s, 'jira', %s, TRUE)""",
            [_REGISTRATION_ID, str(tenant_a_id), _REGISTRATION_SECRET],
        )
    return _REGISTRATION_ID


# ── Identity, memberships and ownership ──────────────────────────────────────


def test_the_runtime_connection_is_the_fixed_restricted_role(runtime_connection):
    rows = _rows(
        runtime_connection,
        """SELECT session_user, current_user, r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole,
                  r.rolreplication, r.rolinherit, r.rolcanlogin
           FROM pg_roles AS r WHERE r.rolname = session_user""",
    )
    assert rows == [(RUNTIME_ROLE, RUNTIME_ROLE, False, False, False, False, False, False, True)]


def test_the_runtime_role_has_no_membership_in_either_direction_and_owns_nothing(runtime_connection):
    rows = _rows(
        runtime_connection,
        """SELECT (SELECT count(*) FROM pg_auth_members WHERE member = r.oid OR roleid = r.oid),
                  (SELECT count(*) FROM pg_shdepend
                   WHERE refclassid = 'pg_authid'::regclass AND refobjid = r.oid AND deptype = 'o')
           FROM pg_roles AS r WHERE r.rolname = %s""",
        [RUNTIME_ROLE],
    )
    assert rows == [(0, 0)]


def test_the_runtime_cannot_assume_the_owner_role(runtime_connection):
    _assert_denied(runtime_connection, f"SET ROLE {OWNER_ROLE}", "permission denied")


# ── Credential material: no direct access ────────────────────────────────────


@pytest.mark.parametrize("with_context", [False, True])
def test_users_cannot_be_read_directly_with_or_without_context(runtime_connection, tenant_a_id, with_context):
    tenant_id = tenant_a_id if with_context else None
    _assert_denied(runtime_connection, "SELECT * FROM users", "permission denied for table users", tenant_id)


def test_signing_secrets_cannot_be_selected(runtime_connection, registration, tenant_a_id):
    _assert_denied(
        runtime_connection, "SELECT signing_secret FROM webhook_registrations",
        "permission denied for table webhook_registrations", tenant_a_id,
    )


def test_the_legacy_tenant_credential_columns_cannot_be_read(runtime_connection):
    _assert_denied(runtime_connection, "SELECT email, password_hash FROM tenants", "permission denied for table tenants")


def test_the_runtime_assignment_record_is_not_readable(runtime_connection):
    _assert_denied(
        runtime_connection, "SELECT * FROM kerno_bootstrap.runtime_assignment",
        "permission denied for table runtime_assignment",
    )


# ── The profile lookup (migration 030) ───────────────────────────────────────


def test_the_profile_lookup_answers_full_for_the_runtime_and_nothing_for_the_owner(runtime_connection, db_connection):
    assert _rows(runtime_connection, _PROFILE_LOOKUP_SQL) == [("full",)]
    # The record names kerno_test_app; asked by the owner's session it has no answer.
    assert _owner_rows(db_connection, _PROFILE_LOOKUP_SQL) == [(None,)]


@pytest.mark.parametrize("argument", ["'kerno_test'", "'kerno_test_app'::name", "'kerno_test_app'::text"])
def test_the_profile_lookup_takes_no_role_argument(runtime_connection, argument):
    rows, error = _probe(runtime_connection, f"SELECT kerno_bootstrap.session_runtime_profile({argument})")
    assert rows is None and error is not None and error.pgcode == _UNDEFINED_FUNCTION, error


def test_the_profile_lookup_is_a_pinned_read_only_owner_function(runtime_connection):
    rows = _rows(runtime_connection, _PROFILE_LOOKUP_DEFINITION_SQL)
    assert rows == [(True, "s", ["search_path=pg_catalog, pg_temp"], OWNER_ROLE, 0, "text")]


def test_only_the_owner_and_the_runtime_may_execute_the_profile_lookup_never_public(runtime_connection):
    assert _rows(runtime_connection, _PROFILE_LOOKUP_ACL_SQL) == [
        (OWNER_ROLE, "EXECUTE", False),
        (RUNTIME_ROLE, "EXECUTE", False),
    ]


@pytest.mark.parametrize(
    "statement, fragment",
    [
        ("INSERT INTO kerno_bootstrap.runtime_assignment (role_name, profile) VALUES ('kerno_test_app', 'pilot')",
         "permission denied for table runtime_assignment"),
        ("UPDATE kerno_bootstrap.runtime_assignment SET profile = 'pilot'", "permission denied for table runtime_assignment"),
        ("DELETE FROM kerno_bootstrap.runtime_assignment", "permission denied for table runtime_assignment"),
        ("TRUNCATE kerno_bootstrap.runtime_assignment", "permission denied for table runtime_assignment"),
        (f"SET SESSION AUTHORIZATION {OWNER_ROLE}", "permission denied to set session authorization"),
    ],
)
def test_the_runtime_can_neither_write_the_assignment_nor_change_the_identity_the_lookup_reads(
    runtime_connection, statement, fragment
):
    _assert_denied(runtime_connection, statement, fragment)


# ── Strict bootstrap policy and cross-tenant isolation ───────────────────────


def test_webhook_registrations_show_nothing_without_a_tenant_context(runtime_connection, registration, tenant_a_id, tenant_b_id):
    query = "SELECT id FROM webhook_registrations"
    assert _rows(runtime_connection, query) == []
    assert _rows(runtime_connection, query, tenant_id="") == []
    assert _rows(runtime_connection, query, tenant_id=tenant_b_id) == []
    assert [str(row[0]) for row in _rows(runtime_connection, query, tenant_id=tenant_a_id)] == [registration]


def test_tenant_a_context_reads_no_tenant_b_row_in_any_readable_tenant_table(runtime_connection, db_connection, tenant_a_id, tenant_b_id):
    readable = [
        table for table, privileges in matrices.FULL_PROFILE.tables.items()
        if "SELECT" in privileges and _has_tenant_column(runtime_connection, table)
    ]
    assert readable, "no tenant-scoped readable table found; the probe would be vacuous"
    for table in readable:
        rows = _rows(runtime_connection, f"SELECT count(*) FROM {table} WHERE tenant_id <> %s", [str(tenant_a_id)], tenant_a_id)
        assert rows == [(0,)], f"{table} leaked another tenant's rows to tenant A's context"
    for table in ("overrides", "retrieval_bias", "tenant_embeddings"):
        assert _owner_rows(db_connection, f"SELECT count(*) FROM {table}", tenant_id=tenant_b_id)[0][0] >= 1


@pytest.mark.parametrize("table", ["overrides", "retrieval_bias", "tenant_embeddings"])
def test_a_forced_table_returns_no_rows_when_no_tenant_context_was_ever_set(runtime_connection, table):
    assert _rows(runtime_connection, f"SELECT count(*) FROM {table}") == [(0,)]


def test_an_empty_session_context_on_a_cast_policy_is_the_documented_pool_behaviour(runtime_connection):
    # Pre-existing and unchanged (CLAUDE.md §17, pool RESET backlog): a policy
    # that casts the setting straight to uuid errors on '' rather than hiding rows.
    rows, error = _probe(runtime_connection, "SELECT count(*) FROM overrides", tenant_id="")
    assert rows is None and error is not None and error.pgcode == "22P02", error


# ── Ledger and AI log: privilege, before any trigger ─────────────────────────


@pytest.mark.parametrize(
    "statement, table",
    [
        ("UPDATE audit_log SET action_type = 'edit'", "audit_log"),
        ("DELETE FROM audit_log", "audit_log"),
        ("TRUNCATE audit_log", "audit_log"),
        ("UPDATE ai_decision_log SET output_status = 'met'", "ai_decision_log"),
        ("DELETE FROM ai_decision_log", "ai_decision_log"),
        ("TRUNCATE ai_decision_log", "ai_decision_log"),
        ("DELETE FROM context_records", "context_records"),
        ("TRUNCATE context_records", "context_records"),
        ("INSERT INTO users (tenant_id) VALUES (gen_random_uuid())", "users"),
        ("UPDATE tenants SET tenant_slug = 'renamed'", "tenants"),
        ("UPDATE webhook_registrations SET tenant_id = gen_random_uuid()", "webhook_registrations"),
    ],
)
def test_mutations_outside_the_matrix_are_denied_by_privilege(runtime_connection, tenant_a_id, statement, table):
    _assert_denied(runtime_connection, statement, f"permission denied for table {table}", tenant_a_id)


# ── Owner-only changes ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "statement, fragment",
    [
        ("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only", "must be owner of table audit_log"),
        ("ALTER TABLE ai_decision_log DISABLE TRIGGER ai_decision_log_retain_window", "must be owner of table ai_decision_log"),
        ("ALTER TABLE overrides NO FORCE ROW LEVEL SECURITY", "must be owner of table overrides"),
        ("ALTER TABLE overrides DISABLE ROW LEVEL SECURITY", "must be owner of table overrides"),
        ("CREATE POLICY c2_probe ON overrides USING (true)", "must be owner of table overrides"),
        ("DROP POLICY tenant_isolation_policy ON overrides", "must be owner of relation overrides"),
        ("DROP POLICY tenant_isolation_policy ON users", "must be owner of relation users"),
        ("CREATE TABLE public.c2_probe (id int)", "permission denied for schema public"),
        ("CREATE FUNCTION kerno_bootstrap.c2_probe() RETURNS int LANGUAGE sql AS 'SELECT 1'", "permission denied for schema kerno_bootstrap"),
        ("CREATE TEMPORARY TABLE c2_probe (id int)", "permission denied to create temporary tables"),
        ("CALL kerno_bootstrap.apply_runtime_grants('kerno_test_app', 'full')", "permission denied for procedure apply_runtime_grants"),
        ("CALL kerno_bootstrap.revoke_runtime_grants('kerno_test_app')", "permission denied for procedure revoke_runtime_grants"),
    ],
)
def test_ddl_policy_trigger_and_grant_changes_need_the_owner(runtime_connection, statement, fragment):
    _assert_denied(runtime_connection, statement, fragment)


# ── H1 and H2: the interface, and the residual it does not remove ────────────


def test_h1_returns_the_exact_account_including_its_hash_the_documented_residual(runtime_connection, db_connection, h1_users, tenant_a_id):
    rows = _rows(
        runtime_connection,
        "SELECT tenant_id, password_hash, role, is_active FROM kerno_bootstrap.resolve_login(%s, %s)",
        [h1_users, _H1_EMAIL],
    )
    stored = _owner_rows(db_connection, "SELECT password_hash FROM users WHERE email = %s", [_H1_EMAIL])[0][0]
    assert rows == [(str(tenant_a_id), stored, "vciso", True)]


@pytest.mark.parametrize(
    "slug_variant, email",
    [
        ("{slug}", _H1_EMAIL.upper()),
        ("{slug}", f" {_H1_EMAIL}"),
        ("{slug}", "c2-runtime-h1%"),
        ("{slug}", "c2-runtime-h1_example.test"),
        ("{slug}", "%"),
        ("{slug_upper}", _H1_EMAIL),
        ("%", _H1_EMAIL),
        ("", _H1_EMAIL),
        ("{slug}", ""),
    ],
)
def test_h1_matches_only_an_exact_slug_and_email_pair(runtime_connection, h1_users, slug_variant, email):
    slug = slug_variant.format(slug=h1_users, slug_upper=h1_users.upper())
    assert _rows(runtime_connection, "SELECT * FROM kerno_bootstrap.resolve_login(%s, %s)", [slug, email]) == []


def test_h1_returns_one_row_per_call_but_a_statement_may_call_it_once_per_supplied_pair(runtime_connection, h1_users):
    rows = _rows(
        runtime_connection,
        """SELECT found.is_active FROM (VALUES (%s, %s), (%s, %s)) AS pairs(slug, email),
           LATERAL kerno_bootstrap.resolve_login(pairs.slug, pairs.email) AS found""",
        [h1_users, _H1_EMAIL, h1_users, _H1_SECOND_EMAIL],
    )
    assert sorted(rows) == [(False,), (True,)]


def test_h1_and_h2_leave_no_tenant_context_behind(runtime_connection, h1_users, registration):
    rows = _rows(
        runtime_connection,
        """SELECT (SELECT count(*) FROM kerno_bootstrap.resolve_login(%s, %s)),
                  (SELECT count(*) FROM kerno_bootstrap.webhook_verification(%s)),
                  current_setting('app.current_tenant_id', TRUE)""",
        [h1_users, _H1_EMAIL, registration],
    )
    assert rows == [(1, 1, None)]


def test_h2_returns_the_secret_for_an_exact_id_only_the_documented_full_profile_residual(runtime_connection, registration, tenant_a_id):
    rows = _rows(
        runtime_connection,
        "SELECT tenant_id, signing_secret, is_active FROM kerno_bootstrap.webhook_verification(%s)",
        [registration],
    )
    assert rows == [(str(tenant_a_id), _REGISTRATION_SECRET, True)]
    assert _rows(runtime_connection, "SELECT * FROM kerno_bootstrap.webhook_verification(%s)", [str(uuid.uuid4())]) == []


# ── PUBLIC ACL entries and the effective-privilege matrix ────────────────────


def test_every_acldefault_call_passes_the_internal_char_type():
    # Offline guard on the SQL text; PostgreSQL's own acceptance of it is Checkpoint B evidence.
    first_arguments = re.findall(r"acldefault\(\s*(.+?),\s*\w+\.\w+\)", matrices.PUBLIC_ACL_ENTRIES_SQL, re.DOTALL)
    assert len(first_arguments) == 4
    for argument in first_arguments:
        assert re.fullmatch(r"'[a-z]'", argument) or argument.endswith('::"char"'), argument


def test_public_holds_nothing_on_the_helpers_procedures_bootstrap_schema_database_or_relations(runtime_connection):
    assert _rows(runtime_connection, matrices.PUBLIC_ACL_ENTRIES_SQL) == []


def test_effective_table_privileges_equal_the_full_profile_matrix_in_both_directions(runtime_connection):
    actual, expected, listed = matrices.table_privileges(_runtime_fetch(runtime_connection), RUNTIME_ROLE, matrices.FULL_PROFILE)
    assert listed >= {f"public.{table}" for table in matrices.FULL_PROFILE.tables}
    assert actual == expected, f"missing {sorted(expected - actual)}; unexpected {sorted(actual - expected)}"


def test_effective_column_privileges_equal_the_full_profile_matrix_in_both_directions(runtime_connection):
    actual, expected, named = matrices.column_privileges(_runtime_fetch(runtime_connection), RUNTIME_ROLE, matrices.FULL_PROFILE)
    assert named <= expected, f"matrix names columns that do not exist: {sorted(named - expected)}"
    assert actual == expected, f"missing {sorted(expected - actual)}; unexpected {sorted(actual - expected)}"


def test_effective_sequence_routine_schema_and_database_privileges_equal_the_matrix(runtime_connection):
    fetch = _runtime_fetch(runtime_connection)
    held_sequences, expected_sequences = matrices.sequence_privileges(fetch, RUNTIME_ROLE, matrices.FULL_PROFILE)
    assert held_sequences == expected_sequences
    assert matrices.routine_privileges(fetch, RUNTIME_ROLE) == matrices.FULL_PROFILE.routines
    assert matrices.schema_privileges(fetch, RUNTIME_ROLE, matrices.FULL_PROFILE) == matrices.FULL_PROFILE.schemas
    assert matrices.database_privileges(fetch, RUNTIME_ROLE, matrices.FULL_PROFILE) == matrices.FULL_PROFILE.database


def test_the_recorded_assignment_is_the_runtime_role_with_the_full_profile(db_connection, runtime_connection):
    assert _owner_rows(db_connection, "SELECT role_name, profile FROM kerno_bootstrap.runtime_assignment") == [
        (RUNTIME_ROLE, "full"),
    ]


# ── The API startup gate against real connections ────────────────────────────


def test_the_startup_gate_accepts_the_runtime_pool(runtime_pool, monkeypatch):
    monkeypatch.delenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, raising=False)
    identity = runtime_identity.read_connection_identity()
    assert identity.session_user == RUNTIME_ROLE and runtime_identity.identity_problems(identity) == []
    assert runtime_identity.read_assigned_profile() == "full"
    runtime_identity.enforce_runtime_identity(development=False, serving_profile="full")


@pytest.mark.parametrize("development", [False, True])
def test_the_startup_gate_refuses_a_pilot_process_under_the_full_grants(runtime_pool, monkeypatch, development):
    monkeypatch.delenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, raising=False)
    expected = "the database assigns this login the 'full' grant profile but this process serves 'pilot'"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        runtime_identity.enforce_runtime_identity(development=development, serving_profile="pilot")


@pytest.mark.integration
def test_the_startup_gate_refuses_the_owner_login(db_connection, monkeypatch):
    owner_url = test_database_safety.current_state().target.url
    owner_pool = psycopg2.pool.ThreadedConnectionPool(1, 1, dsn=owner_url)

    def owner_pool_only():
        """Return the owner pool in place of the application's."""
        return owner_pool

    monkeypatch.setattr(dependencies, "_get_pool", owner_pool_only)
    monkeypatch.delenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, raising=False)
    try:
        problems = runtime_identity.identity_problems(runtime_identity.read_connection_identity())
        assert "the login owns the database" in problems and "the login owns database objects" in problems
        assert runtime_identity.read_assigned_profile() is None
        with pytest.raises(RuntimeError, match="owns the database") as refused:
            runtime_identity.enforce_runtime_identity(development=False, serving_profile="full")
        assert "no grant profile is assigned to this login" in str(refused.value)
    finally:
        owner_pool.closeall()
