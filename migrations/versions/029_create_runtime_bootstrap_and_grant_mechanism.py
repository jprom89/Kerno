"""Create the C2 runtime-boundary mechanism: bootstrap helpers, strict bootstrap policies, grant procedures.

What:  creates the owner-owned schema kerno_bootstrap with
       - H1 resolve_login(slug, email) and H2 webhook_verification(id):
         fixed, exact-key SECURITY DEFINER lookups with a pinned search_path,
         at most one row per call, EXECUTE revoked from PUBLIC;
       - apply_runtime_grants(role, profile) and revoke_runtime_grants(role):
         owner-only procedures implementing the runtime privilege matrix of
         docs/pilot_readiness_kickoff.md §1.4 (profiles 'full' and 'pilot');
       - runtime_assignment: a one-row, owner-only record of which role holds
         which profile, written and cleared only by those procedures;
       and replaces the context-optional policies on users and
       webhook_registrations with strict USING + WITH CHECK policies. Their
       existing ENABLE-without-FORCE status is kept, and every other policy
       and FORCE flag is unchanged.
Why:   PILOT-READY-002 (issue #18). The application has been the table owner,
       so it could disable the ledger triggers, and the two bootstrap tables
       were readable in full before any tenant context existed. A non-owner
       runtime identity needs exact-key bootstrap reads and an owner-applied
       grant set; this migration provides both, and grants nothing itself.
How:   python scripts/migrate_test_database.py upgrade head     (test database)
       python scripts/migrate_test_database.py downgrade c4d5e6f7
       Grants are applied separately by the owner:
       python scripts/migrate_test_database.py grant-runtime
       Proven by tests/unit/models/test_runtime_bootstrap_migration.py and the
       runtime-identity tests in tests/integration and tests/security.

Alembic revision chain:
  Revises: c4d5e6f7 (028_bind_overrides_to_reviewed_recommendations)
  Next:    (none - this is the head revision)

Decisions recorded here because the migration is where they take effect
---------------------------------------------------------------------

The migration grants nothing to any role and creates no role. Runtime access
exists only after the owner runs apply_runtime_grants through a guarded
operation. A later table-creating migration extends the matrix by replacing
apply_runtime_grants and then calls it for the role and profile in
runtime_assignment, when a row exists.

The assignment is a table, not the database settings kerno.runtime_role and
kerno.runtime_profile that the kickoff (§1.4, §2.7 step 5) proposed. Writing
or resetting a custom placeholder setting with ALTER DATABASE requires
superuser or a SET grant on that parameter (PostgreSQL's
validate_option_array_item), so the non-superuser owner could neither record
nor clear them, and the downgrade's RESET would fail even when nothing was
set. The table needs no new privilege: the owner creates and writes it, and
the procedure that applies the grants records them in the same transaction,
so the record and the grants cannot drift. It holds no tenant data, so it has
no tenant_id and no RLS policy; PUBLIC holds nothing on it and the runtime is
never granted it. It allows one row: one runtime role per database, and
applying grants for a second role is refused until the first is revoked.

No FORCE on users and webhook_registrations is a PROPOSED EXCEPTION awaiting
owner approval for operational use (kickoff §1.5). The helpers execute as the
owner and rely on the owner's RLS bypass on these two tables; under FORCE and
a strict policy they would see nothing before a tenant context exists. For a
non-owner runtime, FORCE would add nothing.

The helpers limit the query interface; they do not withhold the material.
H1 returns the password hash of any account whose exact slug and email are
supplied, as often as it is called. H2 returns the signing secret of any
registration id. A compromised runtime credential can therefore collect the
hashes of accounts it can name and, in the full profile, every webhook secret
(it can list registration ids under any context it sets). Accepting that
residual is an owner decision; it is not accepted here.

The procedures run with the caller's rights (SECURITY INVOKER) and only the
owner can execute them: EXECUTE is revoked from PUBLIC, and only the owner of
the tables can grant on them. apply_runtime_grants refuses a role that does
not exist, cannot log in, is the caller or the database owner, has an
elevated attribute, has a membership in either direction, or owns any object.
It first revokes whatever the runtime holds on these objects, so applying a
profile is idempotent and switching profiles removes privileges.

The downgrade revokes the recorded runtime role's grants and CONNECT, drops
the procedures, helpers, record and schema, and restores the original 019
and 022 policies. It deletes no role and no application data. Historical
migrations are not edited.
"""

from alembic import op

revision = "d5e6f7g8"
down_revision = "c4d5e6f7"
branch_labels = None
depends_on = None

_STRICT_TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.current_tenant_id', TRUE), '')::uuid"

# The original context-optional predicate of migrations 019 and 022, restored
# verbatim by the downgrade.
_CONTEXT_OPTIONAL_PREDICATE = """
    tenant_id = NULLIF(current_setting('app.current_tenant_id', TRUE), '')::uuid
    OR current_setting('app.current_tenant_id', TRUE) IS NULL
    OR current_setting('app.current_tenant_id', TRUE) = ''
"""

_BOOTSTRAP_TABLES = ("users", "webhook_registrations")

_CREATE_SCHEMA = """
CREATE SCHEMA kerno_bootstrap;
REVOKE ALL ON SCHEMA kerno_bootstrap FROM PUBLIC;
"""

_CREATE_ASSIGNMENT_RECORD = """
CREATE TABLE kerno_bootstrap.runtime_assignment (
  singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  role_name name NOT NULL,
  profile text NOT NULL CHECK (profile IN ('full', 'pilot')),
  assigned_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON TABLE kerno_bootstrap.runtime_assignment FROM PUBLIC;
"""

_CREATE_RESOLVE_LOGIN = """
CREATE FUNCTION kerno_bootstrap.resolve_login(p_tenant_slug text, p_email text)
RETURNS TABLE (user_id uuid, tenant_id uuid, password_hash text, role text, is_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
  SELECT u.user_id, u.tenant_id, u.password_hash, u.role, u.is_active
  FROM public.users AS u
  JOIN public.tenants AS t ON t.tenant_id = u.tenant_id
  WHERE t.tenant_slug = p_tenant_slug AND u.email = p_email
  LIMIT 1
$$;
REVOKE ALL ON FUNCTION kerno_bootstrap.resolve_login(text, text) FROM PUBLIC;
"""

_CREATE_WEBHOOK_VERIFICATION = """
CREATE FUNCTION kerno_bootstrap.webhook_verification(p_registration_id uuid)
RETURNS TABLE (tenant_id uuid, signing_secret text, is_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
  SELECT r.tenant_id, r.signing_secret, r.is_active
  FROM public.webhook_registrations AS r
  WHERE r.id = p_registration_id
  LIMIT 1
$$;
REVOKE ALL ON FUNCTION kerno_bootstrap.webhook_verification(uuid) FROM PUBLIC;
"""

# Revokes only what the runtime holds on the objects this mechanism governs.
# PUBLIC's own defaults are untouched and are covered by the effective-
# privilege tests, never assumed away by this revoke.
_CREATE_REVOKE_PROCEDURE = """
CREATE PROCEDURE kerno_bootstrap.revoke_runtime_grants(p_role name)
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = p_role) THEN
    RAISE EXCEPTION 'runtime role % does not exist', p_role;
  END IF;
  EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', p_role);
  EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', p_role);
  EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA kerno_bootstrap FROM %I', p_role);
  EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA kerno_bootstrap FROM %I', p_role);
  EXECUTE format('REVOKE ALL ON SCHEMA kerno_bootstrap FROM %I', p_role);
  EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', p_role);
  DELETE FROM kerno_bootstrap.runtime_assignment WHERE role_name = p_role;
END
$$;
REVOKE ALL ON PROCEDURE kerno_bootstrap.revoke_runtime_grants(name) FROM PUBLIC;
"""

# The matrix of docs/pilot_readiness_kickoff.md §1.4. "pilot" is the shared
# part; "full" adds only what the features excluded from the pilot need.
_CREATE_APPLY_PROCEDURE = """
CREATE PROCEDURE kerno_bootstrap.apply_runtime_grants(p_role name, p_profile text)
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  runtime pg_catalog.pg_roles%ROWTYPE;
  grant_sql text;
  shared_grants text[] := ARRAY[
    'GRANT USAGE ON SCHEMA public TO %I',
    'GRANT USAGE ON SCHEMA kerno_bootstrap TO %I',
    'GRANT EXECUTE ON FUNCTION kerno_bootstrap.resolve_login(text, text) TO %I',
    'GRANT SELECT, INSERT ON public.context_records TO %I',
    'GRANT SELECT, INSERT ON public.control_evidence_links TO %I',
    'GRANT UPDATE (linked_by, linked_at, relevance_score, note, removed_at) ON public.control_evidence_links TO %I',
    'GRANT SELECT ON public.compliance_controls TO %I',
    'GRANT SELECT, INSERT ON public.recommendations TO %I',
    'GRANT UPDATE (is_superseded) ON public.recommendations TO %I',
    'GRANT SELECT, INSERT ON public.overrides TO %I',
    'GRANT SELECT, INSERT ON public.audit_log TO %I',
    'GRANT USAGE ON SEQUENCE public.audit_log_sequence_number_seq TO %I',
    'GRANT INSERT ON public.ai_decision_log TO %I',
    'GRANT SELECT ON public.retrieval_bias TO %I',
    'GRANT SELECT, INSERT ON public.dora_register_entries TO %I',
    'GRANT UPDATE (provider_name, service_name, provider_type, criticality_level, business_function, data_types, countries_supported, contract_start_date, contract_end_date, exit_strategy_summary, is_active, source_record_id, updated_at) ON public.dora_register_entries TO %I',
    'GRANT SELECT ON public.dora_submission_windows TO %I',
    'GRANT SELECT, INSERT ON public.dora_submission_runs TO %I',
    'GRANT UPDATE (status, validation_overall_status, validation_issue_count, entry_count, frozen_package_json, updated_at) ON public.dora_submission_runs TO %I'
  ];
  full_only_grants text[] := ARRAY[
    'GRANT SELECT (tenant_id, tenant_slug, trust_center_public, is_active) ON public.tenants TO %I',
    'GRANT UPDATE (trust_center_public) ON public.tenants TO %I',
    'GRANT SELECT (id, tenant_id, source_system, created_at, is_active) ON public.webhook_registrations TO %I',
    'GRANT INSERT (id, tenant_id, source_system, signing_secret) ON public.webhook_registrations TO %I',
    'GRANT UPDATE (signing_secret) ON public.webhook_registrations TO %I',
    'GRANT EXECUTE ON FUNCTION kerno_bootstrap.webhook_verification(uuid) TO %I',
    'GRANT SELECT ON public.webhook_ingest_dedup TO %I',
    'GRANT INSERT (tenant_id, source_system, external_ref) ON public.webhook_ingest_dedup TO %I',
    'GRANT UPDATE (received_at) ON public.webhook_ingest_dedup TO %I',
    'GRANT SELECT ON public.ai_decision_log TO %I',
    'GRANT INSERT ON public.retrieval_bias TO %I',
    'GRANT UPDATE (bias_vector, override_count, last_recalculated_at) ON public.retrieval_bias TO %I',
    'GRANT SELECT ON public.tenant_embeddings TO %I',
    'GRANT SELECT ON public.remediation_routing_rules TO %I',
    'GRANT SELECT, INSERT ON public.remediation_tasks TO %I',
    'GRANT UPDATE (closed_at, re_review_flagged_at) ON public.remediation_tasks TO %I',
    'GRANT SELECT ON public.dora_reporting_windows TO %I'
  ];
BEGIN
  IF p_profile IS NULL OR p_profile NOT IN ('full', 'pilot') THEN
    RAISE EXCEPTION 'runtime profile must be full or pilot';
  END IF;
  SELECT * INTO runtime FROM pg_catalog.pg_roles WHERE rolname = p_role;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'runtime role % does not exist', p_role;
  END IF;
  IF NOT runtime.rolcanlogin THEN
    RAISE EXCEPTION 'runtime role % cannot log in', p_role;
  END IF;
  IF runtime.rolsuper OR runtime.rolbypassrls OR runtime.rolcreaterole
     OR runtime.rolcreatedb OR runtime.rolreplication THEN
    RAISE EXCEPTION 'runtime role % has an elevated attribute', p_role;
  END IF;
  IF p_role = current_user OR p_role = session_user
     OR p_role = (SELECT pg_catalog.pg_get_userbyid(d.datdba) FROM pg_catalog.pg_database AS d
                  WHERE d.datname = pg_catalog.current_database()) THEN
    RAISE EXCEPTION 'runtime role % is the owner or the caller', p_role;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members AS m
             WHERE m.member = runtime.oid OR m.roleid = runtime.oid) THEN
    RAISE EXCEPTION 'runtime role % has a role membership', p_role;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend AS s
             WHERE s.refclassid = 'pg_catalog.pg_authid'::regclass
               AND s.refobjid = runtime.oid AND s.deptype = 'o') THEN
    RAISE EXCEPTION 'runtime role % owns objects', p_role;
  END IF;
  IF EXISTS (SELECT 1 FROM kerno_bootstrap.runtime_assignment AS a WHERE a.role_name <> p_role) THEN
    RAISE EXCEPTION 'another runtime role is assigned; revoke it before assigning %', p_role;
  END IF;
  CALL kerno_bootstrap.revoke_runtime_grants(p_role);
  FOREACH grant_sql IN ARRAY shared_grants LOOP
    EXECUTE format(grant_sql, p_role);
  END LOOP;
  IF p_profile = 'full' THEN
    FOREACH grant_sql IN ARRAY full_only_grants LOOP
      EXECUTE format(grant_sql, p_role);
    END LOOP;
  END IF;
  INSERT INTO kerno_bootstrap.runtime_assignment (role_name, profile) VALUES (p_role, p_profile);
END
$$;
REVOKE ALL ON PROCEDURE kerno_bootstrap.apply_runtime_grants(name, text) FROM PUBLIC;
"""

# Revokes the recorded runtime role's grants and CONNECT, if a role is recorded
# and still exists, so a downgraded database leaves the runtime nothing on
# objects that are about to lose their bootstrap path.
_DOWNGRADE_RUNTIME_STATE = """
DO $$
DECLARE
  recorded name;
BEGIN
  SELECT a.role_name INTO recorded FROM kerno_bootstrap.runtime_assignment AS a;
  IF recorded IS NOT NULL AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = recorded) THEN
    CALL kerno_bootstrap.revoke_runtime_grants(recorded);
    EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM %I', current_database(), recorded);
  END IF;
END
$$;
"""

_DROP_MECHANISM = """
DROP PROCEDURE kerno_bootstrap.apply_runtime_grants(name, text);
DROP PROCEDURE kerno_bootstrap.revoke_runtime_grants(name);
DROP FUNCTION kerno_bootstrap.webhook_verification(uuid);
DROP FUNCTION kerno_bootstrap.resolve_login(text, text);
DROP TABLE kerno_bootstrap.runtime_assignment;
DROP SCHEMA kerno_bootstrap;
"""


def upgrade() -> None:
    """Create the schema, the assignment record, the two helpers, the two procedures, and the strict policies."""
    op.execute(_CREATE_SCHEMA)
    op.execute(_CREATE_ASSIGNMENT_RECORD)
    op.execute(_CREATE_RESOLVE_LOGIN)
    op.execute(_CREATE_WEBHOOK_VERIFICATION)
    op.execute(_CREATE_REVOKE_PROCEDURE)
    op.execute(_CREATE_APPLY_PROCEDURE)
    for table in _BOOTSTRAP_TABLES:
        _replace_policy(table, using=_STRICT_TENANT_PREDICATE, with_check=_STRICT_TENANT_PREDICATE)


def downgrade() -> None:
    """Revoke the recorded runtime role's grants, drop the mechanism, and restore the 019 and 022 policies."""
    op.execute(_DOWNGRADE_RUNTIME_STATE)
    op.execute(_DROP_MECHANISM)
    for table in _BOOTSTRAP_TABLES:
        _replace_policy(table, using=_CONTEXT_OPTIONAL_PREDICATE, with_check=None)


def _replace_policy(table: str, using: str, with_check: str | None) -> None:
    """Drop and recreate tenant_isolation_policy; ALTER POLICY cannot remove a WITH CHECK clause."""
    op.execute(f"DROP POLICY tenant_isolation_policy ON {table}")
    check_clause = f" WITH CHECK ({with_check})" if with_check else ""
    op.execute(f"CREATE POLICY tenant_isolation_policy ON {table} USING ({using}){check_clause}")
