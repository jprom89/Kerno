"""Static tests of migration 029 (the C2 runtime-boundary mechanism) and its privilege matrix.

What:  Runs migration 029's upgrade and downgrade against a recording op and
       checks every statement: the schema, the one-row assignment record, the
       two SECURITY DEFINER helpers, the two owner-only procedures, the strict
       policies and their exact restoration, and that the migration grants
       nothing itself. Parses the procedure's grant arrays and compares them
       with the matrix of docs/pilot_readiness_kickoff.md §1.4, written out
       literally, then with the models and with every write path in src/.
Why:   PILOT-READY-002 (issue #18). The matrix decides what the runtime login
       may do; a missing grant breaks a route only in production and an extra
       one widens the boundary silently. Nothing here connects to a database;
       the live proof is in tests/security/test_runtime_identity_boundary.py.
How:   pytest tests/unit/models/test_runtime_bootstrap_migration.py -v
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pkgutil
import re
import types

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests import _database_safety as safety
from tests.unit.test_runtime_sql_boundary import src_string_literals

_VERSIONS = safety.REPOSITORY_ROOT / "migrations" / "versions"
_MIGRATION_029 = _VERSIONS / "029_create_runtime_bootstrap_and_grant_mechanism.py"
_MIGRATION_019 = _VERSIONS / "019_create_users_table.py"
_MIGRATION_022 = _VERSIONS / "022_create_webhook_tables.py"
_ALEMBIC_ENV = safety.REPOSITORY_ROOT / "migrations" / "env.py"

STRICT_PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', TRUE), '')::uuid"
SEARCH_PATH = "SET search_path = pg_catalog, pg_temp"
BOOTSTRAP_TABLES = ("users", "webhook_registrations")

# docs/pilot_readiness_kickoff.md §1.4, both profiles ("pilot" column).
EXPECTED_SHARED_GRANTS = [
    "GRANT USAGE ON SCHEMA public TO %I",
    "GRANT USAGE ON SCHEMA kerno_bootstrap TO %I",
    "GRANT EXECUTE ON FUNCTION kerno_bootstrap.resolve_login(text, text) TO %I",
    "GRANT SELECT, INSERT ON public.context_records TO %I",
    "GRANT SELECT, INSERT ON public.control_evidence_links TO %I",
    "GRANT UPDATE (linked_by, linked_at, relevance_score, note, removed_at) ON public.control_evidence_links TO %I",
    "GRANT SELECT ON public.compliance_controls TO %I",
    "GRANT SELECT, INSERT ON public.recommendations TO %I",
    "GRANT UPDATE (is_superseded) ON public.recommendations TO %I",
    "GRANT SELECT, INSERT ON public.overrides TO %I",
    "GRANT SELECT, INSERT ON public.audit_log TO %I",
    "GRANT USAGE ON SEQUENCE public.audit_log_sequence_number_seq TO %I",
    "GRANT INSERT ON public.ai_decision_log TO %I",
    "GRANT SELECT ON public.retrieval_bias TO %I",
    "GRANT SELECT, INSERT ON public.dora_register_entries TO %I",
    "GRANT UPDATE (provider_name, service_name, provider_type, criticality_level, business_function, data_types, "
    "countries_supported, contract_start_date, contract_end_date, exit_strategy_summary, is_active, "
    "source_record_id, updated_at) ON public.dora_register_entries TO %I",
    "GRANT SELECT ON public.dora_submission_windows TO %I",
    "GRANT SELECT, INSERT ON public.dora_submission_runs TO %I",
    "GRANT UPDATE (status, validation_overall_status, validation_issue_count, entry_count, frozen_package_json, "
    "updated_at) ON public.dora_submission_runs TO %I",
]

# docs/pilot_readiness_kickoff.md §1.4, "added only in the full profile".
EXPECTED_FULL_ONLY_GRANTS = [
    "GRANT SELECT (tenant_id, tenant_slug, trust_center_public, is_active) ON public.tenants TO %I",
    "GRANT UPDATE (trust_center_public) ON public.tenants TO %I",
    "GRANT SELECT (id, tenant_id, source_system, created_at, is_active) ON public.webhook_registrations TO %I",
    "GRANT INSERT (id, tenant_id, source_system, signing_secret) ON public.webhook_registrations TO %I",
    "GRANT UPDATE (signing_secret) ON public.webhook_registrations TO %I",
    "GRANT EXECUTE ON FUNCTION kerno_bootstrap.webhook_verification(uuid) TO %I",
    "GRANT SELECT ON public.webhook_ingest_dedup TO %I",
    "GRANT INSERT (tenant_id, source_system, external_ref) ON public.webhook_ingest_dedup TO %I",
    "GRANT UPDATE (received_at) ON public.webhook_ingest_dedup TO %I",
    "GRANT SELECT ON public.ai_decision_log TO %I",
    "GRANT INSERT ON public.retrieval_bias TO %I",
    "GRANT UPDATE (bias_vector, override_count, last_recalculated_at) ON public.retrieval_bias TO %I",
    "GRANT SELECT ON public.tenant_embeddings TO %I",
    "GRANT SELECT ON public.remediation_routing_rules TO %I",
    "GRANT SELECT, INSERT ON public.remediation_tasks TO %I",
    "GRANT UPDATE (closed_at, re_review_flagged_at) ON public.remediation_tasks TO %I",
    "GRANT SELECT ON public.dora_reporting_windows TO %I",
]

NEVER_GRANTED = (
    "users", "control_crosswalks", "alembic_version", "runtime_assignment", "apply_runtime_grants",
    "revoke_runtime_grants", "dora_organizations", "dora_organization_roles", "dora_organization_identifiers",
    "dora_contracts", "dora_contract_parties", "dora_contract_relationships",
)

# Modules no mounted route imports, so their writes are not runtime writes:
# the reference-data loader used by the seed scripts, and the DORA v2 services
# (kickoff §1.9: their tables get no grant).
# Columns a migration created that the SQLAlchemy model never gained. The
# migration is the schema, so the grant is right and the model is stale; the
# guard test below fails once the model catches up, so the entry gets removed.
KNOWN_MODEL_GAPS = {
    ("control_evidence_links", "removed_at"): (
        "009_add_rls_to_evidence_links.py", "ALTER TABLE control_evidence_links ADD COLUMN removed_at TIMESTAMPTZ NULL",
    ),
}

UNMOUNTED_WRITERS = {
    "src/services/control_service.py",
    "src/services/dora_organization_service.py",
    "src/services/dora_contract_service.py",
    "src/services/dora_contract_hierarchy_service.py",
}

_GRANT_SHAPE = re.compile(r"^GRANT (?P<privileges>.+?) ON (?P<object>.+) TO %I$")
_PRIVILEGE_ITEM = re.compile(r"(?P<name>[A-Z]+)(?: \((?P<columns>[^)]*)\))?")
_UPDATE_SET = re.compile(r"\bUPDATE\s+(?:public\.)?(\w+)\s+SET\s+(.+?)(?=\s+WHERE\b|\s+RETURNING\b|$)", re.IGNORECASE)
_UPSERT_SET = re.compile(
    r"\bINSERT\s+INTO\s+(?:public\.)?(\w+)\b.*?\bDO\s+UPDATE\s+SET\s+(.+?)(?=\s+WHERE\b|\s+RETURNING\b|$)",
    re.IGNORECASE,
)
_INSERT_COLUMNS = re.compile(r"\bINSERT\s+INTO\s+(?:public\.)?(\w+)\s*\(([^)]*)\)", re.IGNORECASE)
_ASSIGNED_COLUMN = re.compile(r"(?:^|,)\s*(\w+)\s*=")


class _RecordingOp:
    """Stands in for alembic's op and keeps every statement; it offers execute() only."""

    def __init__(self) -> None:
        """Start with no recorded statements."""
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        """Record one statement exactly as given."""
        self.statements.append(sql)


def _load(path, name: str):
    """Import a migration by path; its file name starts with digits."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorded(path, step_name: str) -> list[str]:
    """Run a migration step against a recording op and return its statements as given."""
    migration = _load(path, f"migration_{path.stem[:3]}")
    recorder = _RecordingOp()
    migration.op = recorder
    getattr(migration, step_name)()
    return recorder.statements


def _normalised(sql: str) -> str:
    """sql with every run of whitespace collapsed to one space."""
    return " ".join(sql.split())


def _without_paren_spacing(sql: str) -> str:
    """Normalised sql with no space beside a parenthesis, so layout differences do not count."""
    return re.sub(r"\s*([()])\s*", r"\1", _normalised(sql))


def _outside_bodies(sql: str) -> str:
    """The text of sql outside its $$-quoted bodies."""
    return " ".join(sql.split("$$")[0::2])


def _bodies(sql: str) -> list[str]:
    """The $$-quoted bodies of sql, normalised."""
    return [_normalised(body) for body in sql.split("$$")[1::2]]


def _statement(step_name: str, prefix: str) -> str:
    """The one migration-029 statement of step_name whose normalised text starts with prefix."""
    matches = [sql for sql in _recorded(_MIGRATION_029, step_name) if _normalised(sql).startswith(prefix)]
    assert len(matches) == 1, prefix
    return matches[0]


def _apply_procedure() -> str:
    """The CREATE PROCEDURE statement for apply_runtime_grants."""
    return _statement("upgrade", "CREATE PROCEDURE kerno_bootstrap.apply_runtime_grants")


def _grant_array(name: str) -> list[str]:
    """The elements of one text[] grant array declared in apply_runtime_grants."""
    declaration = re.search(rf"{name} text\[\] := ARRAY\[(.*?)\];", _apply_procedure(), re.DOTALL)
    assert declaration, name
    return re.findall(r"'((?:[^']|'')*)'", declaration.group(1))


def _all_grants() -> list[str]:
    """Every grant either profile applies (full = shared + full-only)."""
    return _grant_array("shared_grants") + _grant_array("full_only_grants")


def _privileges_by_table(grants: list[str]) -> dict[tuple[str, str], set[str] | None]:
    """Map (privilege, table) to its granted columns, or None for a whole-table grant, over public tables."""
    granted: dict[tuple[str, str], set[str] | None] = {}
    for grant in grants:
        shape = _GRANT_SHAPE.match(grant)
        if not shape.group("object").startswith("public."):
            continue
        table = shape.group("object").removeprefix("public.")
        for item in _PRIVILEGE_ITEM.finditer(shape.group("privileges")):
            key = (item.group("name"), table)
            if item.group("columns") is None or granted.get(key, set()) is None:
                granted[key] = None
            else:
                granted.setdefault(key, set()).update(_split_columns(item.group("columns")))
    return granted


def _split_columns(column_list: str) -> set[str]:
    """The column names in a comma-separated list."""
    return {column.strip() for column in column_list.split(",") if column.strip()}


def _model_tables():
    """Every SQLAlchemy table defined under src/models, by name."""
    import src.models as models

    for module in pkgutil.iter_modules(models.__path__):
        importlib.import_module(f"src.models.{module.name}")
    return models.Base.metadata.tables


def _application_writes(pattern: re.Pattern) -> dict[str, set[str]]:
    """Columns written per table by every runtime-reachable SQL literal in src/ that matches pattern."""
    written: dict[str, set[str]] = {}
    for path, literals in src_string_literals().items():
        if path in UNMOUNTED_WRITERS:
            continue
        for literal in literals:
            for table, columns in pattern.findall(_normalised(literal)):
                written.setdefault(table, set()).update(_written_columns(pattern, columns))
    return written


def _written_columns(pattern: re.Pattern, captured: str) -> set[str]:
    """The column names in an INSERT column list or a SET clause."""
    if pattern is _INSERT_COLUMNS:
        return _split_columns(captured)
    return set(_ASSIGNED_COLUMN.findall(captured))


# ── Revision chain ──────────────────────────────────────────────────────────


def test_migration_029_sits_on_028_and_its_only_successor_030_is_the_single_head():
    migration = _load(_MIGRATION_029, "migration_029")
    assert (migration.revision, migration.down_revision) == ("d5e6f7g8", "c4d5e6f7")
    config = Config()
    config.set_main_option("script_location", str(safety.REPOSITORY_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    # 029 was the head until PILOT-READY-003 added 030 (the profile lookup) on top of it.
    assert script.get_heads() == ["e6f7g8h9"]
    assert script.get_revision("e6f7g8h9").down_revision == "d5e6f7g8"
    successors = [revision.revision for revision in script.walk_revisions() if revision.down_revision == "d5e6f7g8"]
    assert successors == ["e6f7g8h9"]


def test_upgrade_runs_in_dependency_order():
    prefixes = [
        "CREATE SCHEMA kerno_bootstrap", "CREATE TABLE kerno_bootstrap.runtime_assignment",
        "CREATE FUNCTION kerno_bootstrap.resolve_login", "CREATE FUNCTION kerno_bootstrap.webhook_verification",
        "CREATE PROCEDURE kerno_bootstrap.revoke_runtime_grants",
        "CREATE PROCEDURE kerno_bootstrap.apply_runtime_grants",
        "DROP POLICY tenant_isolation_policy ON users", "CREATE POLICY tenant_isolation_policy ON users",
        "DROP POLICY tenant_isolation_policy ON webhook_registrations",
        "CREATE POLICY tenant_isolation_policy ON webhook_registrations",
    ]
    statements = [_normalised(sql) for sql in _recorded(_MIGRATION_029, "upgrade")]
    assert len(statements) == len(prefixes)
    for statement, prefix in zip(statements, prefixes):
        assert statement.startswith(prefix), prefix


# ── Schema and assignment record ────────────────────────────────────────────


def test_the_schema_is_created_with_public_privileges_revoked():
    statement = _statement("upgrade", "CREATE SCHEMA kerno_bootstrap")
    assert _normalised(statement) == "CREATE SCHEMA kerno_bootstrap; REVOKE ALL ON SCHEMA kerno_bootstrap FROM PUBLIC;"


def test_the_assignment_record_is_a_one_row_owner_only_table():
    statement = _normalised(_statement("upgrade", "CREATE TABLE kerno_bootstrap.runtime_assignment"))
    assert "singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton)" in statement
    assert "role_name name NOT NULL" in statement
    assert "profile text NOT NULL CHECK (profile IN ('full', 'pilot'))" in statement
    assert statement.endswith("REVOKE ALL ON TABLE kerno_bootstrap.runtime_assignment FROM PUBLIC;")
    for absent in ("tenant_id", "ROW LEVEL SECURITY", "POLICY", "GRANT"):
        assert absent not in statement


# ── The two SECURITY DEFINER helpers ────────────────────────────────────────

HELPERS = [
    pytest.param(
        "CREATE FUNCTION kerno_bootstrap.resolve_login",
        "CREATE FUNCTION kerno_bootstrap.resolve_login(p_tenant_slug text, p_email text) "
        "RETURNS TABLE (user_id uuid, tenant_id uuid, password_hash text, role text, is_active boolean)",
        "SELECT u.user_id, u.tenant_id, u.password_hash, u.role, u.is_active FROM public.users AS u "
        "JOIN public.tenants AS t ON t.tenant_id = u.tenant_id "
        "WHERE t.tenant_slug = p_tenant_slug AND u.email = p_email LIMIT 1",
        "REVOKE ALL ON FUNCTION kerno_bootstrap.resolve_login(text, text) FROM PUBLIC;",
        id="resolve_login",
    ),
    pytest.param(
        "CREATE FUNCTION kerno_bootstrap.webhook_verification",
        "CREATE FUNCTION kerno_bootstrap.webhook_verification(p_registration_id uuid) "
        "RETURNS TABLE (tenant_id uuid, signing_secret text, is_active boolean)",
        "SELECT r.tenant_id, r.signing_secret, r.is_active FROM public.webhook_registrations AS r "
        "WHERE r.id = p_registration_id LIMIT 1",
        "REVOKE ALL ON FUNCTION kerno_bootstrap.webhook_verification(uuid) FROM PUBLIC;",
        id="webhook_verification",
    ),
]


@pytest.mark.parametrize("prefix, signature, body, revoke", HELPERS)
def test_each_helper_is_a_pinned_security_definer_sql_function(prefix, signature, body, revoke):
    statement = _statement("upgrade", prefix)
    header = _normalised(_outside_bodies(statement))
    assert header.startswith(f"{signature} LANGUAGE sql STABLE SECURITY DEFINER {SEARCH_PATH} AS ")
    assert header.endswith(revoke)
    assert _bodies(statement) == [body]


@pytest.mark.parametrize("prefix, signature, body, revoke", HELPERS)
def test_each_helper_reads_schema_qualified_tables_by_exact_key_at_most_once(prefix, signature, body, revoke):
    statement = _statement("upgrade", prefix)
    helper_body = _bodies(statement)[0]
    for table in re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", helper_body):
        assert table.startswith("public."), table
    assert helper_body.endswith("LIMIT 1")
    assert not re.search(r"\b(I?LIKE|SIMILAR|ANY|IN)\b|~|<|>", helper_body)
    for forbidden in (r"\bset_config\b", r"\bEXECUTE\b", r"\bformat\s*\(", r"\bGRANT\b", r"\bplpgsql\b"):
        assert not re.search(forbidden, statement, re.IGNORECASE), forbidden


# ── The two owner-only procedures ───────────────────────────────────────────

PROCEDURES = [
    pytest.param(
        "CREATE PROCEDURE kerno_bootstrap.revoke_runtime_grants(p_role name)",
        "REVOKE ALL ON PROCEDURE kerno_bootstrap.revoke_runtime_grants(name) FROM PUBLIC;",
        id="revoke_runtime_grants",
    ),
    pytest.param(
        "CREATE PROCEDURE kerno_bootstrap.apply_runtime_grants(p_role name, p_profile text)",
        "REVOKE ALL ON PROCEDURE kerno_bootstrap.apply_runtime_grants(name, text) FROM PUBLIC;",
        id="apply_runtime_grants",
    ),
]


@pytest.mark.parametrize("signature, revoke", PROCEDURES)
def test_each_procedure_is_pinned_runs_with_the_callers_rights_and_is_revoked_from_public(signature, revoke):
    statement = _statement("upgrade", signature)
    header = _normalised(_outside_bodies(statement))
    assert header.startswith(f"{signature} LANGUAGE plpgsql {SEARCH_PATH} AS ")
    assert header.endswith(revoke)
    assert "SECURITY DEFINER" not in statement.upper()


def test_the_revoke_procedure_withdraws_everything_it_governs_and_clears_the_assignment():
    body = _bodies(_statement("upgrade", "CREATE PROCEDURE kerno_bootstrap.revoke_runtime_grants"))[0]
    assert "IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = p_role) THEN" in body
    assert re.findall(r"EXECUTE format\('([^']*)', p_role\)", body) == [
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I",
        "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I",
        "REVOKE ALL ON ALL TABLES IN SCHEMA kerno_bootstrap FROM %I",
        "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA kerno_bootstrap FROM %I",
        "REVOKE ALL ON SCHEMA kerno_bootstrap FROM %I",
        "REVOKE ALL ON SCHEMA public FROM %I",
    ]
    assert body.endswith("DELETE FROM kerno_bootstrap.runtime_assignment WHERE role_name = p_role; END")


def test_the_apply_procedure_validates_the_profile_and_the_role_before_granting():
    body = _bodies(_apply_procedure())[0]
    for check in (
        "p_profile IS NULL OR p_profile NOT IN ('full', 'pilot')",
        "IF NOT FOUND THEN", "IF NOT runtime.rolcanlogin THEN",
        "runtime.rolsuper OR runtime.rolbypassrls OR runtime.rolcreaterole "
        "OR runtime.rolcreatedb OR runtime.rolreplication",
        "p_role = current_user OR p_role = session_user", "d.datdba",
        "m.member = runtime.oid OR m.roleid = runtime.oid", "s.refobjid = runtime.oid AND s.deptype = 'o'",
        "a.role_name <> p_role",
    ):
        assert check in body, check
    assert body.index("CALL kerno_bootstrap.revoke_runtime_grants(p_role)") < body.index("FOREACH")


def test_the_apply_procedure_grants_full_extras_only_for_the_full_profile_then_records_the_assignment():
    body = _bodies(_apply_procedure())[0]
    loops = re.findall(r"FOREACH grant_sql IN ARRAY (\w+) LOOP EXECUTE format\(grant_sql, p_role\); END LOOP;", body)
    assert loops == ["shared_grants", "full_only_grants"]
    assert "IF p_profile = 'full' THEN FOREACH grant_sql IN ARRAY full_only_grants" in body
    assert body.endswith(
        "INSERT INTO kerno_bootstrap.runtime_assignment (role_name, profile) VALUES (p_role, p_profile); END"
    )


# ── The migration grants nothing and touches no role, database or FORCE ─────


def test_upgrade_executes_no_grant_outside_the_apply_procedure_body():
    for statement in _recorded(_MIGRATION_029, "upgrade"):
        assert not re.search(r"\bGRANT\b", _outside_bodies(statement)), _normalised(statement)[:80]
    with_grants = [sql for sql in _recorded(_MIGRATION_029, "upgrade") if re.search(r"\bGRANT\b", sql)]
    assert with_grants == [_apply_procedure()]


@pytest.mark.parametrize("step_name", ["upgrade", "downgrade"])
def test_neither_step_touches_a_role_a_database_setting_force_or_another_table(step_name):
    text = " ".join(_normalised(sql) for sql in _recorded(_MIGRATION_029, step_name))
    for forbidden in (
        r"\b(CREATE|ALTER|DROP)\s+(ROLE|USER|DATABASE)\b", r"\bFORCE\s+ROW\s+LEVEL\s+SECURITY\b", r"\bNO\s+FORCE\b",
        r"\bALTER\s+TABLE\b", r"\b(ENABLE|DISABLE)\s+(ROW\s+LEVEL\s+SECURITY|TRIGGER)\b", r"\bRESET\b",
        r"\bALTER\s+DEFAULT\s+PRIVILEGES\b", r"\bDROP\s+OWNED\b", r"\bREASSIGN\b", r"\bTRUNCATE\b", r"\bkerno_dev\b",
    ):
        assert not re.search(forbidden, text, re.IGNORECASE), forbidden


# ── Policies ────────────────────────────────────────────────────────────────


def test_upgrade_makes_exactly_the_two_bootstrap_policies_strict_with_using_and_with_check():
    policies = [_normalised(sql) for sql in _recorded(_MIGRATION_029, "upgrade") if "POLICY" in sql]
    expected = []
    for table in BOOTSTRAP_TABLES:
        expected.append(f"DROP POLICY tenant_isolation_policy ON {table}")
        expected.append(
            f"CREATE POLICY tenant_isolation_policy ON {table} "
            f"USING ({STRICT_PREDICATE}) WITH CHECK ({STRICT_PREDICATE})"
        )
    assert policies == expected


def _original_policy(path, table: str) -> str:
    """The CREATE POLICY statement migration 019 or 022 runs for table."""
    prefix = f"CREATE POLICY tenant_isolation_policy ON {table} "
    matches = [sql for sql in _recorded(path, "upgrade") if _normalised(sql).startswith(prefix)]
    assert len(matches) == 1, table
    return matches[0]


def test_downgrade_restores_the_019_and_022_context_optional_policies_exactly():
    restored = [sql for sql in _recorded(_MIGRATION_029, "downgrade") if "CREATE POLICY" in sql]
    originals = [_original_policy(_MIGRATION_019, "users"), _original_policy(_MIGRATION_022, "webhook_registrations")]
    assert [_without_paren_spacing(sql) for sql in restored] == [_without_paren_spacing(sql) for sql in originals]
    for statement in restored:
        assert "WITH CHECK" not in statement
        assert "current_setting('app.current_tenant_id', TRUE) IS NULL" in _normalised(statement)
    dropped = [_normalised(sql) for sql in _recorded(_MIGRATION_029, "downgrade") if "DROP POLICY" in sql]
    assert dropped == [f"DROP POLICY tenant_isolation_policy ON {table}" for table in BOOTSTRAP_TABLES]


# ── Downgrade ───────────────────────────────────────────────────────────────


def test_downgrade_first_revokes_the_recorded_roles_grants_and_connect():
    first = _normalised(_recorded(_MIGRATION_029, "downgrade")[0])
    assert first.startswith("DO $$")
    for step in (
        "SELECT a.role_name INTO recorded FROM kerno_bootstrap.runtime_assignment AS a;",
        "IF recorded IS NOT NULL AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = recorded) THEN",
        "CALL kerno_bootstrap.revoke_runtime_grants(recorded);",
        "EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM %I', current_database(), recorded);",
    ):
        assert step in first, step


def test_downgrade_drops_the_procedures_helpers_record_and_schema_and_nothing_else():
    statements = _recorded(_MIGRATION_029, "downgrade")
    drops = [_normalised(part) for part in statements[1].split(";") if part.strip()]
    assert drops == [
        "DROP PROCEDURE kerno_bootstrap.apply_runtime_grants(name, text)",
        "DROP PROCEDURE kerno_bootstrap.revoke_runtime_grants(name)",
        "DROP FUNCTION kerno_bootstrap.webhook_verification(uuid)",
        "DROP FUNCTION kerno_bootstrap.resolve_login(text, text)",
        "DROP TABLE kerno_bootstrap.runtime_assignment",
        "DROP SCHEMA kerno_bootstrap",
    ]
    every_drop = re.findall(r"\bDROP\s+\w+\s+[\w.]+", " ".join(_normalised(sql) for sql in statements))
    assert len(every_drop) == len(drops) + len(BOOTSTRAP_TABLES)


# ── The privilege matrix ────────────────────────────────────────────────────


def test_the_shared_grants_are_exactly_the_pilot_column_of_the_matrix():
    assert _grant_array("shared_grants") == EXPECTED_SHARED_GRANTS


def test_the_full_only_grants_are_exactly_the_full_profile_additions():
    assert _grant_array("full_only_grants") == EXPECTED_FULL_ONLY_GRANTS


def test_every_grant_is_a_plain_role_grant_of_an_allowed_privilege_on_a_qualified_object():
    allowed_object = re.compile(
        r"^(SCHEMA (public|kerno_bootstrap)|SEQUENCE public\.\w+|FUNCTION kerno_bootstrap\.\w+\([\w, ]*\)|public\.\w+)$"
    )
    for grant in _all_grants():
        shape = _GRANT_SHAPE.match(grant)
        assert shape, grant
        assert allowed_object.match(shape.group("object")), grant
        names = {item.group("name") for item in _PRIVILEGE_ITEM.finditer(shape.group("privileges"))}
        assert names <= {"SELECT", "INSERT", "UPDATE", "USAGE", "EXECUTE"}, grant
        assert "GRANT OPTION" not in grant


def test_no_grant_carries_a_forbidden_privilege():
    forbidden = re.compile(
        r"\b(TRUNCATE|TRIGGER|REFERENCES|DELETE|MAINTAIN|CREATE|TEMP|TEMPORARY|CONNECT|ALL|PRIVILEGES)\b"
    )
    for grant in _all_grants():
        assert not forbidden.search(grant), grant


@pytest.mark.parametrize("name", NEVER_GRANTED)
def test_no_grant_names_an_object_the_runtime_must_never_reach(name):
    for grant in _all_grants():
        assert not re.search(rf"\b{name}\b", grant), grant


def test_every_column_grant_names_only_columns_the_model_defines():
    tables = _model_tables()
    column_grants = {key: columns for key, columns in _privileges_by_table(_all_grants()).items() if columns}
    assert {table for _, table in column_grants} == {
        "control_evidence_links", "recommendations", "dora_register_entries", "dora_submission_runs", "tenants",
        "webhook_registrations", "webhook_ingest_dedup", "retrieval_bias", "remediation_tasks",
    }
    for (privilege, table), columns in column_grants.items():
        assert table in tables, table
        defined = {column.name for column in tables[table].columns}
        defined.update(column for gap_table, column in KNOWN_MODEL_GAPS if gap_table == table)
        unknown = columns - defined
        assert unknown == set(), f"{privilege} on {table}: {sorted(unknown)}"


@pytest.mark.parametrize("table, column", list(KNOWN_MODEL_GAPS))
def test_each_known_model_gap_is_still_a_gap_and_its_migration_creates_the_column(table, column):
    migration_name, added_by = KNOWN_MODEL_GAPS[(table, column)]
    assert column not in {defined.name for defined in _model_tables()[table].columns}
    assert added_by in [_normalised(sql) for sql in _recorded(_VERSIONS / migration_name, "upgrade")]


def test_every_granted_table_is_a_model_or_is_created_by_a_migration():
    tables = _model_tables()
    migrations = " ".join(path.read_text(encoding="utf-8") for path in _VERSIONS.glob("*.py"))
    for _, table in _privileges_by_table(_all_grants()):
        assert table in tables or re.search(rf"CREATE TABLE {table} \(", migrations), table


def test_the_granted_sequence_is_the_one_the_audit_log_draws_from():
    sequence_number = _model_tables()["audit_log"].columns["sequence_number"]
    assert "nextval('audit_log_sequence_number_seq')" in str(sequence_number.server_default.arg)


# ── The matrix against the application's real write paths ──────────────────


def test_update_grants_are_exactly_the_columns_the_application_updates():
    updated = _application_writes(_UPDATE_SET)
    for table, columns in _application_writes(_UPSERT_SET).items():
        updated.setdefault(table, set()).update(columns)
    granted = {table: columns for (privilege, table), columns in _privileges_by_table(_all_grants()).items()
               if privilege == "UPDATE"}
    assert updated == granted


def test_insert_grants_cover_exactly_the_tables_and_columns_the_application_inserts():
    inserted = _application_writes(_INSERT_COLUMNS)
    granted = {table: columns for (privilege, table), columns in _privileges_by_table(_all_grants()).items()
               if privilege == "INSERT"}
    assert set(inserted) == set(granted)
    for table, columns in granted.items():
        if columns is not None:
            assert inserted[table] == columns, table


# ── migrations/env.py: the owner URL first, a blank one counts as unset ─────


def _env_py_database_url(environ: dict):
    """Evaluate migrations/env.py's own database_url expression against environ, without running Alembic."""
    tree = ast.parse(_ALEMBIC_ENV.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "database_url":
            expression = compile(ast.Expression(node.value), str(_ALEMBIC_ENV), "eval")
            return eval(expression, {"os": types.SimpleNamespace(environ=environ)})
    raise AssertionError("migrations/env.py no longer assigns database_url")


@pytest.mark.parametrize(
    "environ, expected",
    [
        ({"KERNO_MIGRATION_DATABASE_URL": "owner", "DATABASE_URL": "runtime"}, "owner"),
        ({"DATABASE_URL": "runtime"}, "runtime"),
        ({"KERNO_MIGRATION_DATABASE_URL": "", "DATABASE_URL": "runtime"}, "runtime"),
        ({"KERNO_MIGRATION_DATABASE_URL": "   ", "DATABASE_URL": "runtime"}, "runtime"),
        ({}, None),
    ],
    ids=["owner-preferred", "fallback", "empty-is-unset", "whitespace-is-unset", "neither"],
)
def test_alembic_prefers_the_owner_url_and_treats_a_blank_one_as_unset(environ, expected):
    assert _env_py_database_url(environ) == expected
