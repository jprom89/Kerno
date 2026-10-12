"""test_runtime_profile_transition.py — switching kerno_test_app between the full and pilot grant sets, never committed.

What:  Inside one owner transaction on db_connection that is always rolled
       back, calls kerno_bootstrap.apply_runtime_grants for kerno_test_app and
       reads the runtime's effective privileges back with has_*_privilege in
       that same transaction: full -> pilot leaves exactly the pilot matrix
       (every full-only table, column and routine privilege gone, the AI-log
       insert and the profile lookup kept); pilot -> full restores exactly the
       full matrix; re-applying either profile changes nothing; and a refused
       call (an unknown profile, the owner role, a role that does not exist)
       rolls back to its savepoint with the privileges and the assignment
       record unchanged. After every test the committed state is proved
       untouched: the record still says ('kerno_test_app', 'full') and the
       runtime, on its own connection, still holds exactly the full matrix.
       One offline test checks that the literal matrices are exactly what the
       reviewed procedure text (migration 029 plus 030's one line) grants.
Why:   PILOT-READY-003 (issue #20). Switching profiles must remove privileges,
       not only add them, and a stored profile label proves nothing about
       effective rights. Rolling back keeps the full-profile run on the full
       grants: no commit, no new role, no global privilege.
How:   Runs in the full-profile run (the default --runtime-profile full):
       python -m pytest tests/security/test_runtime_profile_transition.py --require-live-database --require-runtime-identity
       It needs runtime_connection, so without runtime settings it skips by
       name; the offline matrix test always runs.
"""

from __future__ import annotations

import functools
import importlib.util
import re

import psycopg2
import pytest

from tests import _database_safety as test_database_safety
from tests.security import runtime_privilege_matrices as matrices
from tests.security.test_runtime_identity_boundary import OWNER_ROLE, RUNTIME_ROLE, _rows

_MIGRATION_030 = (
    test_database_safety.REPOSITORY_ROOT / "migrations" / "versions" / "030_add_session_runtime_profile_lookup.py"
)
_NO_SUCH_ROLE = "kerno_no_such_runtime_role"
_ASSIGNMENT_SQL = "SELECT role_name, profile FROM kerno_bootstrap.runtime_assignment"

# Named samples of what only the full profile grants, and of what the pilot
# keeps, read with has_*_privilege for the runtime role (the one parameter).
_FULL_ONLY_PRIVILEGES = {
    "SELECT tenants.trust_center_public": "has_column_privilege(%s::name, 'public.tenants', 'trust_center_public', 'SELECT')",
    "UPDATE tenants.trust_center_public": "has_column_privilege(%s::name, 'public.tenants', 'trust_center_public', 'UPDATE')",
    "SELECT tenants.tenant_slug": "has_column_privilege(%s::name, 'public.tenants', 'tenant_slug', 'SELECT')",
    "SELECT webhook_registrations.id": "has_column_privilege(%s::name, 'public.webhook_registrations', 'id', 'SELECT')",
    "INSERT webhook_registrations.signing_secret":
        "has_column_privilege(%s::name, 'public.webhook_registrations', 'signing_secret', 'INSERT')",
    "UPDATE webhook_registrations.signing_secret":
        "has_column_privilege(%s::name, 'public.webhook_registrations', 'signing_secret', 'UPDATE')",
    "SELECT webhook_ingest_dedup": "has_table_privilege(%s::name, 'public.webhook_ingest_dedup', 'SELECT')",
    "INSERT webhook_ingest_dedup.external_ref":
        "has_column_privilege(%s::name, 'public.webhook_ingest_dedup', 'external_ref', 'INSERT')",
    "UPDATE webhook_ingest_dedup.received_at":
        "has_column_privilege(%s::name, 'public.webhook_ingest_dedup', 'received_at', 'UPDATE')",
    "SELECT ai_decision_log": "has_table_privilege(%s::name, 'public.ai_decision_log', 'SELECT')",
    "INSERT retrieval_bias": "has_table_privilege(%s::name, 'public.retrieval_bias', 'INSERT')",
    "UPDATE retrieval_bias.bias_vector": "has_column_privilege(%s::name, 'public.retrieval_bias', 'bias_vector', 'UPDATE')",
    "UPDATE retrieval_bias.last_recalculated_at":
        "has_column_privilege(%s::name, 'public.retrieval_bias', 'last_recalculated_at', 'UPDATE')",
    "SELECT tenant_embeddings": "has_table_privilege(%s::name, 'public.tenant_embeddings', 'SELECT')",
    "SELECT remediation_routing_rules": "has_table_privilege(%s::name, 'public.remediation_routing_rules', 'SELECT')",
    "INSERT remediation_tasks": "has_table_privilege(%s::name, 'public.remediation_tasks', 'INSERT')",
    "UPDATE remediation_tasks.closed_at": "has_column_privilege(%s::name, 'public.remediation_tasks', 'closed_at', 'UPDATE')",
    "SELECT dora_reporting_windows": "has_table_privilege(%s::name, 'public.dora_reporting_windows', 'SELECT')",
    "EXECUTE webhook_verification": "has_function_privilege(%s::name, 'kerno_bootstrap.webhook_verification(uuid)', 'EXECUTE')",
}
_KEPT_BY_THE_PILOT = {
    "INSERT ai_decision_log": "has_table_privilege(%s::name, 'public.ai_decision_log', 'INSERT')",
    "SELECT retrieval_bias": "has_table_privilege(%s::name, 'public.retrieval_bias', 'SELECT')",
    "EXECUTE resolve_login": "has_function_privilege(%s::name, 'kerno_bootstrap.resolve_login(text, text)', 'EXECUTE')",
    "EXECUTE session_runtime_profile":
        "has_function_privilege(%s::name, 'kerno_bootstrap.session_runtime_profile()', 'EXECUTE')",
}

_SNAPSHOT_QUERIES = (
    (matrices.TABLE_PRIVILEGES_SQL, matrices.TABLE_PRIVILEGE_NAMES),
    (matrices.COLUMN_PRIVILEGES_SQL, matrices.COLUMN_PRIVILEGE_NAMES),
    (matrices.SEQUENCE_PRIVILEGES_SQL, matrices.SEQUENCE_PRIVILEGE_NAMES),
)

_GRANT_STATEMENT = re.compile(
    r"GRANT (?P<privileges>[A-Z, ]+?)(?: \((?P<columns>[^)]*)\))? ON "
    r"(?:(?P<kind>SCHEMA|SEQUENCE|FUNCTION) )?(?P<target>.+) TO %I"
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _in_transaction(owner):
    """A matrices fetch(sql, params) that reads inside the open owner transaction, never ending it."""

    def fetch(sql: str, params=None) -> list:
        """Run one read on the owner connection without commit or rollback."""
        return owner.execute(sql, params).fetchall()

    return fetch


def _apply(owner, role: str, profile: str) -> None:
    """Call the owner-only grant procedure inside the open owner transaction."""
    owner.execute("CALL kerno_bootstrap.apply_runtime_grants(%s, %s)", [role, profile])


def _held(fetch, privileges: dict[str, str]) -> dict[str, bool]:
    """Evaluate each named has_*_privilege expression for the runtime role."""
    return {label: fetch(f"SELECT {expression}", [RUNTIME_ROLE])[0][0] for label, expression in privileges.items()}


def _effective_snapshot(fetch) -> list:
    """Every table, column, sequence and routine privilege row for the runtime role, sorted, for exact comparison."""
    snapshot = [sorted(fetch(sql, [RUNTIME_ROLE, list(names)])) for sql, names in _SNAPSHOT_QUERIES]
    snapshot.append(sorted(fetch(matrices.ROUTINE_PRIVILEGES_SQL, [RUNTIME_ROLE])))
    return snapshot


def _assert_committed_state_is_the_full_profile(db_connection, runtime_connection) -> None:
    """Prove nothing was committed: the owner reads the full assignment, the runtime still holds the full matrix."""
    assignment = db_connection.execute(_ASSIGNMENT_SQL).fetchall()
    db_connection.rollback()
    assert assignment == [(RUNTIME_ROLE, "full")]
    assert _rows(runtime_connection, "SELECT kerno_bootstrap.session_runtime_profile()") == [("full",)]
    runtime_fetch = functools.partial(_rows, runtime_connection)
    assert matrices.matrix_differences(runtime_fetch, RUNTIME_ROLE, matrices.FULL_PROFILE) == []


def _procedure_grant_arrays() -> dict[str, list[str]]:
    """Return the GRANT templates of the reviewed apply_runtime_grants (029 plus 030's line), by array name."""
    spec = importlib.util.spec_from_file_location("kerno_migration_030_for_transition_tests", _MIGRATION_030)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    procedure = migration.apply_procedure_with_lookup_grant()
    arrays = re.findall(r"(\w+) text\[\] := ARRAY\[(.*?)\];", procedure, re.DOTALL)
    return {name: re.findall(r"'([^']*)'", body) for name, body in arrays}


def _granted_by(statements: list[str]) -> dict:
    """Return the tables, columns, sequences, routines and schemas a list of GRANT templates gives."""
    granted = {"tables": {}, "columns": {}, "sequences": {}, "routines": set(), "schemas": set()}
    for statement in statements:
        match = _GRANT_STATEMENT.fullmatch(statement)
        assert match is not None, f"not a recognised grant: {statement}"
        privileges = {privilege.strip() for privilege in match["privileges"].split(",")}
        target = match["target"].removeprefix("public.")
        if match["kind"] == "SCHEMA":
            granted["schemas"].update((target, privilege) for privilege in privileges)
        elif match["kind"] == "FUNCTION":
            granted["routines"].add(target.replace(" ", ""))
        elif match["kind"] == "SEQUENCE":
            granted["sequences"].setdefault(target, set()).update(privileges)
        elif match["columns"]:
            columns = {column.strip() for column in match["columns"].split(",")}
            for privilege in privileges:
                granted["columns"].setdefault(target, {}).setdefault(privilege, set()).update(columns)
        else:
            granted["tables"].setdefault(target, set()).update(privileges)
    return granted


def _named_by(matrix: matrices.PrivilegeMatrix) -> dict:
    """Return the same shape as _granted_by for what a literal matrix grants."""
    return {
        "tables": matrix.tables,
        "columns": matrix.columns,
        "sequences": matrix.sequences,
        "routines": {routine for routine, held in matrix.routines.items() if held},
        "schemas": {key for key, held in matrix.schemas.items() if held},
    }


# ── Fixture ──────────────────────────────────────────────────────────────────


@pytest.fixture
def owner_transaction(runtime_connection, db_connection):
    """Yield db_connection inside one owner transaction that is always rolled back, never committed.

    The transaction starts from the committed full assignment. After the test,
    whatever its outcome, it is rolled back and the committed state is proved
    to be the full profile, as the owner reads it and as the runtime holds it.
    """
    db_connection.rollback()
    fetch = _in_transaction(db_connection)
    try:
        assert fetch(_ASSIGNMENT_SQL) == [(RUNTIME_ROLE, "full")]
        yield db_connection
    finally:
        db_connection.rollback()
    _assert_committed_state_is_the_full_profile(db_connection, runtime_connection)


# ── Transitions ──────────────────────────────────────────────────────────────


def test_full_to_pilot_removes_every_full_only_privilege_and_leaves_exactly_the_pilot_matrix(owner_transaction):
    fetch = _in_transaction(owner_transaction)
    assert _held(fetch, _FULL_ONLY_PRIVILEGES) == dict.fromkeys(_FULL_ONLY_PRIVILEGES, True)
    _apply(owner_transaction, RUNTIME_ROLE, "pilot")
    assert matrices.matrix_differences(fetch, RUNTIME_ROLE, matrices.PILOT_PROFILE) == []
    assert _held(fetch, _FULL_ONLY_PRIVILEGES) == dict.fromkeys(_FULL_ONLY_PRIVILEGES, False)
    assert _held(fetch, _KEPT_BY_THE_PILOT) == dict.fromkeys(_KEPT_BY_THE_PILOT, True)
    assert fetch(_ASSIGNMENT_SQL) == [(RUNTIME_ROLE, "pilot")]


def test_pilot_to_full_restores_exactly_the_full_matrix(owner_transaction):
    fetch = _in_transaction(owner_transaction)
    _apply(owner_transaction, RUNTIME_ROLE, "pilot")
    _apply(owner_transaction, RUNTIME_ROLE, "full")
    assert matrices.matrix_differences(fetch, RUNTIME_ROLE, matrices.FULL_PROFILE) == []
    assert _held(fetch, _FULL_ONLY_PRIVILEGES) == dict.fromkeys(_FULL_ONLY_PRIVILEGES, True)
    assert _held(fetch, _KEPT_BY_THE_PILOT) == dict.fromkeys(_KEPT_BY_THE_PILOT, True)
    assert fetch(_ASSIGNMENT_SQL) == [(RUNTIME_ROLE, "full")]


@pytest.mark.parametrize(
    "profile, matrix", [("full", matrices.FULL_PROFILE), ("pilot", matrices.PILOT_PROFILE)], ids=["full", "pilot"]
)
def test_reapplying_a_profile_is_idempotent(owner_transaction, profile, matrix):
    fetch = _in_transaction(owner_transaction)
    _apply(owner_transaction, RUNTIME_ROLE, profile)
    first = _effective_snapshot(fetch)
    _apply(owner_transaction, RUNTIME_ROLE, profile)
    assert _effective_snapshot(fetch) == first
    assert matrices.matrix_differences(fetch, RUNTIME_ROLE, matrix) == []
    assert fetch(_ASSIGNMENT_SQL) == [(RUNTIME_ROLE, profile)]


@pytest.mark.parametrize(
    "role, profile, refusal",
    [
        (RUNTIME_ROLE, "bogus", "runtime profile must be full or pilot"),
        (OWNER_ROLE, "pilot", f"runtime role {OWNER_ROLE} is the owner or the caller"),
        (_NO_SUCH_ROLE, "pilot", f"runtime role {_NO_SUCH_ROLE} does not exist"),
    ],
    ids=["unknown-profile", "owner-role", "missing-role"],
)
def test_a_refused_apply_rolls_back_to_its_savepoint_without_effect(owner_transaction, role, profile, refusal):
    fetch = _in_transaction(owner_transaction)
    _apply(owner_transaction, RUNTIME_ROLE, "pilot")
    before = _effective_snapshot(fetch)
    owner_transaction.execute("SAVEPOINT refused_apply")
    with pytest.raises(psycopg2.Error, match=re.escape(refusal)):
        _apply(owner_transaction, role, profile)
    owner_transaction.execute("ROLLBACK TO SAVEPOINT refused_apply")
    assert _effective_snapshot(fetch) == before
    assert matrices.matrix_differences(fetch, RUNTIME_ROLE, matrices.PILOT_PROFILE) == []
    assert fetch(_ASSIGNMENT_SQL) == [(RUNTIME_ROLE, "pilot")]


# ── The literals against the reviewed procedure text (offline) ───────────────


def test_the_literal_matrices_are_exactly_what_the_reviewed_procedure_grants():
    arrays = _procedure_grant_arrays()
    assert sorted(arrays) == ["full_only_grants", "shared_grants"]
    assert _granted_by(arrays["shared_grants"]) == _named_by(matrices.PILOT_PROFILE)
    assert _granted_by(arrays["shared_grants"] + arrays["full_only_grants"]) == _named_by(matrices.FULL_PROFILE)
