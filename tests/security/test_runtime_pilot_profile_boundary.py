"""test_runtime_pilot_profile_boundary.py — what kerno_test_app can and cannot do once it holds the pilot grants.

What:  Live probes, run as kerno_test_app on the disposable kerno_test database
       after the owner has switched it to the pilot grant set: its effective
       privileges equal the pilot matrix exactly, in both directions; the
       profile lookup answers 'pilot'; every full-only table, column and
       routine is denied (tenants, webhook registrations and dedup, the AI-log
       read, tenant embeddings, remediation, reporting windows, H2), as are
       users, the DORA v2 tables and the assignment record; the AI-log insert
       and the coverage read of retrieval_bias the retained workflow needs
       still work; PUBLIC holds nothing; the role has no memberships and owns
       nothing; and the API startup gate accepts a pilot process and refuses a
       full one.
Why:   PILOT-READY-003 (issue #20, gate G10). The pilot app only serves the
       retained workflow, and the runtime must hold exactly what that workflow
       needs. The matrix literals are shared with the full boundary and the
       transition tests (tests/security/runtime_privilege_matrices.py).
How:   Only in the pilot run, after the guarded switch to the pilot grants:
       python scripts/migrate_test_database.py grant-runtime --profile pilot
       python -m pytest --require-live-database --require-runtime-identity --runtime-profile pilot
       Every test is marked pilot_profile, so the full-profile run deselects
       them; without runtime settings they skip by name ("runtime identity not
       configured", or "live database test skipped" with no database at all).
"""

from __future__ import annotations

import functools
import re
import uuid

import pytest

from src.api import runtime_identity
from tests.security import runtime_privilege_matrices as matrices
from tests.security.test_runtime_identity_boundary import (
    OWNER_ROLE,
    RUNTIME_ROLE,
    _assert_denied,
    _has_tenant_column,
    _owner_rows,
    _rows,
)

pytestmark = pytest.mark.pilot_profile

_PROBE_CONTROL = "pilot-boundary-probe-control"
_PROBE_REGISTRATION = "c3000000-0000-4000-c000-000000000001"
_PROBE_SNAPSHOT_HASH = "0" * 64


def _runtime_fetch(runtime_connection):
    """A matrices fetch(sql, params) that runs each read as the runtime in its own rolled-back transaction."""
    return functools.partial(_rows, runtime_connection)


# ── Identity and the assigned profile ────────────────────────────────────────


def test_the_runtime_connection_is_still_the_fixed_restricted_role(runtime_connection):
    rows = _rows(
        runtime_connection,
        """SELECT session_user, current_user, r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole,
                  r.rolreplication, r.rolcanlogin
           FROM pg_roles AS r WHERE r.rolname = session_user""",
    )
    assert rows == [(RUNTIME_ROLE, RUNTIME_ROLE, False, False, False, False, False, True)]


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


def test_the_profile_lookup_answers_pilot_and_the_record_agrees(runtime_connection, db_connection):
    assert _rows(runtime_connection, "SELECT kerno_bootstrap.session_runtime_profile()") == [("pilot",)]
    assert _owner_rows(db_connection, "SELECT role_name, profile FROM kerno_bootstrap.runtime_assignment") == [
        (RUNTIME_ROLE, "pilot"),
    ]


# ── The pilot matrix, both directions ────────────────────────────────────────


def test_public_holds_nothing_on_the_helpers_procedures_bootstrap_schema_database_or_relations(runtime_connection):
    assert _rows(runtime_connection, matrices.PUBLIC_ACL_ENTRIES_SQL) == []


def test_effective_table_privileges_equal_the_pilot_profile_matrix_in_both_directions(runtime_connection):
    actual, expected, listed = matrices.table_privileges(_runtime_fetch(runtime_connection), RUNTIME_ROLE, matrices.PILOT_PROFILE)
    assert listed >= {f"public.{table}" for table in matrices.PILOT_PROFILE.tables}
    assert actual == expected, f"missing {sorted(expected - actual)}; unexpected {sorted(actual - expected)}"


def test_effective_column_privileges_equal_the_pilot_profile_matrix_in_both_directions(runtime_connection):
    actual, expected, named = matrices.column_privileges(_runtime_fetch(runtime_connection), RUNTIME_ROLE, matrices.PILOT_PROFILE)
    assert named <= expected, f"matrix names columns that do not exist: {sorted(named - expected)}"
    assert actual == expected, f"missing {sorted(expected - actual)}; unexpected {sorted(actual - expected)}"


def test_effective_sequence_routine_schema_and_database_privileges_equal_the_pilot_matrix(runtime_connection):
    fetch = _runtime_fetch(runtime_connection)
    held_sequences, expected_sequences = matrices.sequence_privileges(fetch, RUNTIME_ROLE, matrices.PILOT_PROFILE)
    assert held_sequences == expected_sequences
    assert matrices.routine_privileges(fetch, RUNTIME_ROLE) == matrices.PILOT_PROFILE.routines
    assert matrices.schema_privileges(fetch, RUNTIME_ROLE, matrices.PILOT_PROFILE) == matrices.PILOT_PROFILE.schemas
    assert matrices.database_privileges(fetch, RUNTIME_ROLE, matrices.PILOT_PROFILE) == matrices.PILOT_PROFILE.database


def test_the_unreachable_relations_exist_and_the_runtime_holds_nothing_on_any_of_their_columns(runtime_connection):
    fetch = _runtime_fetch(runtime_connection)
    held_tables, _, listed = matrices.table_privileges(fetch, RUNTIME_ROLE, matrices.PILOT_PROFILE)
    unreachable = set(matrices.PILOT_PROFILE_UNREACHABLE_RELATIONS)
    assert unreachable <= listed, f"not in the catalog: {sorted(unreachable - listed)}"
    assert {name for name, _ in held_tables} & unreachable == set()
    held_columns, _, _ = matrices.column_privileges(fetch, RUNTIME_ROLE, matrices.PILOT_PROFILE)
    assert {f"public.{table}" for table, _, _ in held_columns} & unreachable == set()


# ── Full-only surfaces are denied ────────────────────────────────────────────


@pytest.mark.parametrize(
    "statement, table",
    [
        ("SELECT tenant_id FROM tenants", "tenants"),
        ("SELECT tenant_slug FROM tenants", "tenants"),
        ("SELECT trust_center_public FROM tenants", "tenants"),
        ("UPDATE tenants SET trust_center_public = TRUE", "tenants"),
        ("SELECT * FROM users", "users"),
        ("SELECT id FROM webhook_registrations", "webhook_registrations"),
        ("UPDATE webhook_registrations SET signing_secret = 'replaced'", "webhook_registrations"),
        ("SELECT count(*) FROM webhook_ingest_dedup", "webhook_ingest_dedup"),
        ("SELECT count(*) FROM ai_decision_log", "ai_decision_log"),
        ("SELECT count(*) FROM tenant_embeddings", "tenant_embeddings"),
        ("SELECT count(*) FROM remediation_tasks", "remediation_tasks"),
        ("SELECT count(*) FROM remediation_routing_rules", "remediation_routing_rules"),
        ("SELECT count(*) FROM dora_reporting_windows", "dora_reporting_windows"),
        ("SELECT count(*) FROM control_crosswalks", "control_crosswalks"),
        ("SELECT count(*) FROM alembic_version", "alembic_version"),
        ("SELECT count(*) FROM dora_organizations", "dora_organizations"),
        ("SELECT count(*) FROM dora_contracts", "dora_contracts"),
        ("SELECT count(*) FROM dora_contract_relationships", "dora_contract_relationships"),
        ("SELECT * FROM kerno_bootstrap.runtime_assignment", "runtime_assignment"),
        ("INSERT INTO retrieval_bias (tenant_id, override_count) SELECT tenant_id, override_count FROM retrieval_bias WHERE FALSE",
         "retrieval_bias"),
        ("UPDATE retrieval_bias SET override_count = override_count", "retrieval_bias"),
        ("UPDATE recommendations SET status = status", "recommendations"),
        ("UPDATE control_evidence_links SET control_id = control_id", "control_evidence_links"),
        ("UPDATE dora_submission_runs SET tenant_id = tenant_id", "dora_submission_runs"),
        ("UPDATE audit_log SET action_type = 'edit'", "audit_log"),
        ("DELETE FROM ai_decision_log", "ai_decision_log"),
        ("TRUNCATE context_records", "context_records"),
    ],
)
def test_reads_and_writes_outside_the_pilot_matrix_are_denied_by_privilege(runtime_connection, tenant_a_id, statement, table):
    _assert_denied(runtime_connection, statement, f"permission denied for table {table}", tenant_a_id)


@pytest.mark.parametrize(
    "statement, fragment",
    [
        (f"SELECT * FROM kerno_bootstrap.webhook_verification('{_PROBE_REGISTRATION}')",
         "permission denied for function webhook_verification"),
        ("CALL kerno_bootstrap.apply_runtime_grants('kerno_test_app', 'full')", "permission denied for procedure apply_runtime_grants"),
        ("CALL kerno_bootstrap.revoke_runtime_grants('kerno_test_app')", "permission denied for procedure revoke_runtime_grants"),
        (f"SET ROLE {OWNER_ROLE}", "permission denied"),
        (f"SET SESSION AUTHORIZATION {OWNER_ROLE}", "permission denied to set session authorization"),
        ("CREATE TEMPORARY TABLE pilot_probe (id int)", "permission denied to create temporary tables"),
        ("CREATE TABLE public.pilot_probe (id int)", "permission denied for schema public"),
    ],
)
def test_h2_the_grant_procedures_and_owner_identity_are_out_of_reach(runtime_connection, statement, fragment):
    _assert_denied(runtime_connection, statement, fragment)


# ── What the retained workflow still needs ───────────────────────────────────


def test_the_ai_decision_log_takes_an_insert_it_cannot_read_back(runtime_connection, db_connection, tenant_a_id):
    privileges = _rows(
        runtime_connection,
        "SELECT has_table_privilege(%s, 'public.ai_decision_log', 'INSERT'), "
        "has_table_privilege(%s, 'public.ai_decision_log', 'SELECT')",
        [RUNTIME_ROLE, RUNTIME_ROLE],
    )
    assert privileges == [(True, False)]
    # The insert has the service's exact column list and no RETURNING; _rows rolls it back.
    _rows(
        runtime_connection,
        """INSERT INTO ai_decision_log (correlation_id, tenant_id, control_id, evidence_ids, input_snapshot_hash,
                                        output_status, confidence_score, rationale_extract, model_version)
           VALUES (%s, %s, %s, ARRAY[]::text[], %s, 'gap', 0.0, 'pilot boundary probe', 'pilot-boundary-probe')""",
        [str(uuid.uuid4()), str(tenant_a_id), _PROBE_CONTROL, _PROBE_SNAPSHOT_HASH],
        tenant_a_id,
    )
    stored = _owner_rows(
        db_connection, "SELECT count(*) FROM ai_decision_log WHERE control_id = %s", [_PROBE_CONTROL], tenant_a_id,
    )
    assert stored == [(0,)]


def test_coverage_can_read_the_calibration_date_and_login_can_call_h1(runtime_connection, tenant_b_id):
    calibrated = _rows(
        runtime_connection, "SELECT count(*) FROM retrieval_bias WHERE tenant_id = %s", [str(tenant_b_id)], tenant_b_id,
    )
    assert calibrated == [(1,)]
    no_account = _rows(
        runtime_connection, "SELECT count(*) FROM kerno_bootstrap.resolve_login(%s, %s)",
        ["pilot-boundary-no-such-org", "nobody@pilot-boundary.example"],
    )
    assert no_account == [(0,)]


def test_tenant_a_context_reads_no_tenant_b_row_in_any_pilot_readable_tenant_table(runtime_connection, tenant_a_id):
    readable = [
        table for table, privileges in matrices.PILOT_PROFILE.tables.items()
        if "SELECT" in privileges and _has_tenant_column(runtime_connection, table)
    ]
    assert {"overrides", "retrieval_bias"} <= set(readable), readable
    for table in readable:
        rows = _rows(runtime_connection, f"SELECT count(*) FROM {table} WHERE tenant_id <> %s", [str(tenant_a_id)], tenant_a_id)
        assert rows == [(0,)], f"{table} leaked another tenant's rows to tenant A's context"


# ── The API startup gate against real connections ────────────────────────────


def test_the_startup_gate_accepts_a_pilot_process_and_refuses_a_full_one(runtime_pool, monkeypatch):
    monkeypatch.delenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, raising=False)
    assert runtime_identity.read_assigned_profile() == "pilot"
    runtime_identity.enforce_runtime_identity(development=False, serving_profile="pilot")
    expected = "the database assigns this login the 'pilot' grant profile but this process serves 'full'"
    with pytest.raises(RuntimeError, match=re.escape(expected)):
        runtime_identity.enforce_runtime_identity(development=False, serving_profile="full")
