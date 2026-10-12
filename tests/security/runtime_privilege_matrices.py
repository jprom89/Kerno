"""runtime_privilege_matrices.py — the runtime login's full and pilot privilege matrices, and how to read them back.

What:  docs/pilot_readiness_kickoff.md §1.4 written out as two literal
       PrivilegeMatrix values, FULL_PROFILE and PILOT_PROFILE; the catalog SQL
       that reads a role's effective table, column, sequence, routine, schema
       and database privileges (rights PUBLIC holds included) and PUBLIC's own
       ACL entries; and the functions that compare the two in both directions.
       Migration 030 adds one routine to both columns: EXECUTE on
       kerno_bootstrap.session_runtime_profile().
Why:   PILOT-READY-003 (issue #20). The full-profile boundary, the pilot-profile
       boundary and the profile-transition tests assert against these same
       literals, so a changed grant fails a test instead of quietly redefining
       policy, and neither matrix can drift between files.
How:   Imported by tests/security/test_runtime_identity_boundary.py,
       test_runtime_pilot_profile_boundary.py and
       test_runtime_profile_transition.py; it holds no tests. Every reader
       takes fetch(sql, params) -> rows, so one check runs as the runtime login
       or inside an open owner transaction.
"""

from __future__ import annotations

import dataclasses

TABLE_PRIVILEGE_NAMES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN")
COLUMN_PRIVILEGE_NAMES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
SEQUENCE_PRIVILEGE_NAMES = ("USAGE", "SELECT", "UPDATE")

TABLE_PRIVILEGES_SQL = """
SELECT n.nspname || '.' || c.relname, p.privilege, has_table_privilege(%s, c.oid, p.privilege)
FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
CROSS JOIN unnest(%s::text[]) AS p(privilege)
WHERE n.nspname IN ('public', 'kerno_bootstrap') AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
"""
COLUMN_PRIVILEGES_SQL = """
SELECT c.relname, a.attname, p.privilege, has_column_privilege(%s, c.oid, a.attnum, p.privilege)
FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
CROSS JOIN unnest(%s::text[]) AS p(privilege)
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
"""
SEQUENCE_PRIVILEGES_SQL = """
SELECT c.relname, p.privilege, has_sequence_privilege(%s, c.oid, p.privilege)
FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
CROSS JOIN unnest(%s::text[]) AS p(privilege)
WHERE n.nspname IN ('public', 'kerno_bootstrap') AND c.relkind = 'S'
"""
ROUTINE_PRIVILEGES_SQL = """
SELECT p.oid::regprocedure::text, has_function_privilege(%s, p.oid, 'EXECUTE')
FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace
WHERE n.nspname = 'kerno_bootstrap'
"""
SCHEMA_PRIVILEGE_SQL = "SELECT has_schema_privilege(%s, %s, %s)"
DATABASE_PRIVILEGE_SQL = "SELECT has_database_privilege(%s, current_database(), %s)"
# PUBLIC is grantee 0 in aclexplode. acldefault supplies the implicit ACL when
# the stored one is NULL — a NULL function ACL means PUBLIC may EXECUTE. Its
# first argument is PostgreSQL's internal "char": a quoted one-letter literal
# resolves to it, but a CASE of literals resolves to text, so that one is cast.
PUBLIC_ACL_ENTRIES_SQL = """
SELECT 'routine ' || p.oid::regprocedure::text, acl.privilege_type
FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid = p.pronamespace,
     aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) AS acl
WHERE n.nspname = 'kerno_bootstrap' AND acl.grantee = 0
UNION ALL
SELECT 'schema ' || n.nspname, acl.privilege_type
FROM pg_namespace AS n, aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) AS acl
WHERE n.nspname IN ('public', 'kerno_bootstrap') AND acl.grantee = 0
  AND NOT (n.nspname = 'public' AND acl.privilege_type = 'USAGE')
UNION ALL
SELECT 'database ' || d.datname, acl.privilege_type
FROM pg_database AS d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) AS acl
WHERE d.datname = current_database() AND acl.grantee = 0
UNION ALL
SELECT 'relation ' || n.nspname || '.' || c.relname, acl.privilege_type
FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace,
     aclexplode(coalesce(c.relacl, acldefault(
         (CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END)::"char", c.relowner))) AS acl
WHERE n.nspname IN ('public', 'kerno_bootstrap') AND acl.grantee = 0
"""


@dataclasses.dataclass(frozen=True)
class PrivilegeMatrix:
    """One grant profile written out as literals; anything it does not name must be absent."""

    profile: str
    tables: dict[str, set[str]]
    columns: dict[str, dict[str, set[str]]]
    sequences: dict[str, set[str]]
    routines: dict[str, bool]
    schemas: dict[tuple[str, str], bool]
    database: dict[str, bool]


# The column lists below are migration 029's arrays, unchanged by 030.
_LINK_UPDATE_COLUMNS = {"linked_by", "linked_at", "relevance_score", "note", "removed_at"}
_REGISTER_ENTRY_UPDATE_COLUMNS = {
    "provider_name", "service_name", "provider_type", "criticality_level", "business_function",
    "data_types", "countries_supported", "contract_start_date", "contract_end_date",
    "exit_strategy_summary", "is_active", "source_record_id", "updated_at",
}
_SUBMISSION_RUN_UPDATE_COLUMNS = {
    "status", "validation_overall_status", "validation_issue_count", "entry_count",
    "frozen_package_json", "updated_at",
}

# Shared by both profiles (kickoff §1.4, "Grants shared by both profiles").
RUNTIME_SEQUENCE_PRIVILEGES = {"audit_log_sequence_number_seq": {"USAGE"}}
RUNTIME_SCHEMA_PRIVILEGES = {
    ("public", "USAGE"): True, ("public", "CREATE"): False,
    ("kerno_bootstrap", "USAGE"): True, ("kerno_bootstrap", "CREATE"): False,
}
RUNTIME_DATABASE_PRIVILEGES = {"CONNECT": True, "CREATE": False, "TEMPORARY": False}

# Full profile. Table-level privileges (they imply every column):
FULL_PROFILE_TABLE_PRIVILEGES = {
    "ai_decision_log": {"SELECT", "INSERT"},
    "audit_log": {"SELECT", "INSERT"},
    "compliance_controls": {"SELECT"},
    "context_records": {"SELECT", "INSERT"},
    "control_evidence_links": {"SELECT", "INSERT"},
    "dora_register_entries": {"SELECT", "INSERT"},
    "dora_reporting_windows": {"SELECT"},
    "dora_submission_runs": {"SELECT", "INSERT"},
    "dora_submission_windows": {"SELECT"},
    "overrides": {"SELECT", "INSERT"},
    "recommendations": {"SELECT", "INSERT"},
    "remediation_routing_rules": {"SELECT"},
    "remediation_tasks": {"SELECT", "INSERT"},
    "retrieval_bias": {"SELECT", "INSERT"},
    "tenant_embeddings": {"SELECT"},
    "webhook_ingest_dedup": {"SELECT"},
}
# Column-level grants, beyond the table-level ones:
FULL_PROFILE_COLUMN_PRIVILEGES = {
    "control_evidence_links": {"UPDATE": _LINK_UPDATE_COLUMNS},
    "dora_register_entries": {"UPDATE": _REGISTER_ENTRY_UPDATE_COLUMNS},
    "dora_submission_runs": {"UPDATE": _SUBMISSION_RUN_UPDATE_COLUMNS},
    "recommendations": {"UPDATE": {"is_superseded"}},
    "remediation_tasks": {"UPDATE": {"closed_at", "re_review_flagged_at"}},
    "retrieval_bias": {"UPDATE": {"bias_vector", "override_count", "last_recalculated_at"}},
    "tenants": {
        "SELECT": {"tenant_id", "tenant_slug", "trust_center_public", "is_active"},
        "UPDATE": {"trust_center_public"},
    },
    "webhook_ingest_dedup": {"INSERT": {"tenant_id", "source_system", "external_ref"}, "UPDATE": {"received_at"}},
    "webhook_registrations": {
        "SELECT": {"id", "tenant_id", "source_system", "created_at", "is_active"},
        "INSERT": {"id", "tenant_id", "source_system", "signing_secret"},
        "UPDATE": {"signing_secret"},
    },
}
FULL_PROFILE_ROUTINE_PRIVILEGES = {
    "kerno_bootstrap.apply_runtime_grants(name,text)": False,
    "kerno_bootstrap.resolve_login(text,text)": True,
    "kerno_bootstrap.revoke_runtime_grants(name)": False,
    # Explicit matrix addition, migration 030 (PILOT-READY-003): the runtime's
    # own profile lookup. It takes no argument and answers only for session_user.
    "kerno_bootstrap.session_runtime_profile()": True,
    "kerno_bootstrap.webhook_verification(uuid)": True,
}

# Pilot profile: nothing at all on tenants, users, webhook_registrations,
# webhook_ingest_dedup, tenant_embeddings, remediation_*, dora_reporting_windows,
# control_crosswalks, alembic_version, the DORA v2 tables or runtime_assignment.
PILOT_PROFILE_TABLE_PRIVILEGES = {
    "ai_decision_log": {"INSERT"},
    "audit_log": {"SELECT", "INSERT"},
    "compliance_controls": {"SELECT"},
    "context_records": {"SELECT", "INSERT"},
    "control_evidence_links": {"SELECT", "INSERT"},
    "dora_register_entries": {"SELECT", "INSERT"},
    "dora_submission_runs": {"SELECT", "INSERT"},
    "dora_submission_windows": {"SELECT"},
    "overrides": {"SELECT", "INSERT"},
    "recommendations": {"SELECT", "INSERT"},
    "retrieval_bias": {"SELECT"},
}
PILOT_PROFILE_COLUMN_PRIVILEGES = {
    "control_evidence_links": {"UPDATE": _LINK_UPDATE_COLUMNS},
    "dora_register_entries": {"UPDATE": _REGISTER_ENTRY_UPDATE_COLUMNS},
    "dora_submission_runs": {"UPDATE": _SUBMISSION_RUN_UPDATE_COLUMNS},
    "recommendations": {"UPDATE": {"is_superseded"}},
}
PILOT_PROFILE_ROUTINE_PRIVILEGES = {
    "kerno_bootstrap.apply_runtime_grants(name,text)": False,
    "kerno_bootstrap.resolve_login(text,text)": True,
    "kerno_bootstrap.revoke_runtime_grants(name)": False,
    "kerno_bootstrap.session_runtime_profile()": True,
    "kerno_bootstrap.webhook_verification(uuid)": False,
}
# Relations the pilot profile reaches by no privilege at all, named so the
# absence is asserted against tables that provably exist.
PILOT_PROFILE_UNREACHABLE_RELATIONS = (
    "public.tenants", "public.users", "public.webhook_registrations", "public.webhook_ingest_dedup",
    "public.tenant_embeddings", "public.remediation_routing_rules", "public.remediation_tasks",
    "public.dora_reporting_windows", "public.control_crosswalks", "public.alembic_version",
    "public.dora_organizations", "public.dora_organization_identifiers", "public.dora_organization_roles",
    "public.dora_contracts", "public.dora_contract_parties", "public.dora_contract_relationships",
    "kerno_bootstrap.runtime_assignment",
)

FULL_PROFILE = PrivilegeMatrix(
    profile="full",
    tables=FULL_PROFILE_TABLE_PRIVILEGES,
    columns=FULL_PROFILE_COLUMN_PRIVILEGES,
    sequences=RUNTIME_SEQUENCE_PRIVILEGES,
    routines=FULL_PROFILE_ROUTINE_PRIVILEGES,
    schemas=RUNTIME_SCHEMA_PRIVILEGES,
    database=RUNTIME_DATABASE_PRIVILEGES,
)
PILOT_PROFILE = PrivilegeMatrix(
    profile="pilot",
    tables=PILOT_PROFILE_TABLE_PRIVILEGES,
    columns=PILOT_PROFILE_COLUMN_PRIVILEGES,
    sequences=RUNTIME_SEQUENCE_PRIVILEGES,
    routines=PILOT_PROFILE_ROUTINE_PRIVILEGES,
    schemas=RUNTIME_SCHEMA_PRIVILEGES,
    database=RUNTIME_DATABASE_PRIVILEGES,
)


def table_privileges(fetch, role: str, matrix: PrivilegeMatrix) -> tuple[set, set, set]:
    """Return the held and expected (relation, privilege) pairs, and every relation the catalog listed."""
    rows = fetch(TABLE_PRIVILEGES_SQL, [role, list(TABLE_PRIVILEGE_NAMES)])
    held = {(name, privilege) for name, privilege, granted in rows if granted}
    expected = {
        (f"public.{table}", privilege) for table, privileges in matrix.tables.items() for privilege in privileges
    }
    return held, expected, {name for name, _, _ in rows}


def column_privileges(fetch, role: str, matrix: PrivilegeMatrix) -> tuple[set, set, set]:
    """Return the held and expected (table, column, privilege) triples, and the matrix's own column grants.

    A table-level privilege covers every column, so the expected set is built
    from the catalog's column list; the named set must then be a subset of it,
    or the matrix names a column that does not exist.
    """
    rows = fetch(COLUMN_PRIVILEGES_SQL, [role, list(COLUMN_PRIVILEGE_NAMES)])
    held = {(table, column, privilege) for table, column, privilege, granted in rows if granted}
    expected = set()
    for table, column, privilege, _ in rows:
        if privilege in matrix.tables.get(table, set()):
            expected.add((table, column, privilege))
        if column in matrix.columns.get(table, {}).get(privilege, set()):
            expected.add((table, column, privilege))
    named = {
        (table, column, privilege)
        for table, grants in matrix.columns.items()
        for privilege, columns in grants.items() for column in columns
    }
    return held, expected, named


def sequence_privileges(fetch, role: str, matrix: PrivilegeMatrix) -> tuple[set, set]:
    """Return the held and expected (sequence, privilege) pairs."""
    rows = fetch(SEQUENCE_PRIVILEGES_SQL, [role, list(SEQUENCE_PRIVILEGE_NAMES)])
    held = {(name, privilege) for name, privilege, granted in rows if granted}
    expected = {(name, privilege) for name, privileges in matrix.sequences.items() for privilege in privileges}
    return held, expected


def routine_privileges(fetch, role: str) -> dict[str, bool]:
    """Return {routine signature: EXECUTE held} for every routine in kerno_bootstrap."""
    return dict(fetch(ROUTINE_PRIVILEGES_SQL, [role]))


def schema_privileges(fetch, role: str, matrix: PrivilegeMatrix) -> dict[tuple[str, str], bool]:
    """Return {(schema, privilege): held} for every schema privilege the matrix names."""
    held = {}
    for schema, privilege in matrix.schemas:
        held[(schema, privilege)] = fetch(SCHEMA_PRIVILEGE_SQL, [role, schema, privilege])[0][0]
    return held


def database_privileges(fetch, role: str, matrix: PrivilegeMatrix) -> dict[str, bool]:
    """Return {privilege: held} on the current database for every database privilege the matrix names."""
    held = {}
    for privilege in matrix.database:
        held[privilege] = fetch(DATABASE_PRIVILEGE_SQL, [role, privilege])[0][0]
    return held


def matrix_differences(fetch, role: str, matrix: PrivilegeMatrix) -> list[str]:
    """Return every way the role's effective privileges differ from the matrix, in both directions; [] when exact."""
    differences = []
    held, expected, listed = table_privileges(fetch, role, matrix)
    _note_difference(differences, "table", held, expected)
    _note_missing_names(differences, "tables", {f"public.{table}" for table in matrix.tables}, listed)
    held, expected, named = column_privileges(fetch, role, matrix)
    _note_difference(differences, "column", held, expected)
    _note_missing_names(differences, "columns", named, expected)
    held, expected = sequence_privileges(fetch, role, matrix)
    _note_difference(differences, "sequence", held, expected)
    routines = routine_privileges(fetch, role)
    if routines != matrix.routines:
        differences.append(f"routine: held {sorted(routines.items())}; expected {sorted(matrix.routines.items())}")
    schemas = schema_privileges(fetch, role, matrix)
    if schemas != matrix.schemas:
        differences.append(f"schema: held {sorted(schemas.items())}; expected {sorted(matrix.schemas.items())}")
    database = database_privileges(fetch, role, matrix)
    if database != matrix.database:
        differences.append(f"database: held {sorted(database.items())}; expected {sorted(matrix.database.items())}")
    return differences


def _note_difference(differences: list[str], kind: str, held: set, expected: set) -> None:
    """Append the missing and unexpected entries of one privilege kind, when the two sets differ."""
    if held != expected:
        differences.append(f"{kind}: missing {sorted(expected - held)}; unexpected {sorted(held - expected)}")


def _note_missing_names(differences: list[str], kind: str, named: set, present: set) -> None:
    """Append the entries the matrix names that the catalog does not have, so no comparison is vacuous."""
    if not named <= present:
        differences.append(f"the matrix names {kind} that do not exist: {sorted(named - present)}")
