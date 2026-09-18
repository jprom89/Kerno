"""Create the DORA v2 organisation foundation: organisations, identifiers, roles.

What:  creates dora_organizations, dora_organization_identifiers and
       dora_organization_roles — the first three canonical DORA v2 domain
       tables — each tenant-owned, each under ENABLE + FORCE ROW LEVEL SECURITY
       with the standard tenant_isolation_policy.
Why:   DORA_MODEL_V2.md §5 establishes legal-organisation identity as a
       reusable object that exists once per tenant and participates in many
       arrangements, separately from the Kerno tenant (the customer and
       security boundary), from contracts, services, functions and Register
       profiles, and from the regulatory templates. This is the DORA-V2-001
       slice of §36 and nothing more: no contracts, no services, no functions,
       no profiles, no provenance columns, no template columns.
How:   alembic upgrade z1a2b3c4   (roll back: alembic downgrade y0z1a2b3)
       Proven by tests/security/test_dora_organization_isolation.py and
       tests/integration/test_dora_v2_001_organizations.py against a live
       database.

Alembic revision chain:
  Revises: y0z1a2b3 (024_add_frozen_package_json_to_submission_runs)
  Next:    (none - this is the head revision)

Design decisions recorded here because the migration is where they take effect
----------------------------------------------------------------------------

Composite foreign keys, on purpose. dora_organizations carries a UNIQUE on
(tenant_id, organization_id) even though organization_id is already the
primary key. That candidate key exists so the two child tables can declare
FOREIGN KEY (tenant_id, organization_id) REFERENCES dora_organizations
(tenant_id, organization_id) — which makes it impossible at the database
level for a child row in Tenant A to point at an organisation in Tenant B,
independently of anything the service layer checks. A child that carried its
own tenant_id and referenced the parent by organization_id alone could
silently disagree with the parent's tenant. Each child also keeps a direct
tenant_id REFERENCES tenants so that an orphaned row fails the integration
teardown loudly, like every other tenant-owned table here.

Canonical form is enforced where the value is persisted. The uniqueness rule
"within one tenant, the same (identifier_type, identifier_value) must not
identify two organisations" is only as strong as the values in the column.
So identifier_type must equal upper(btrim(identifier_type)),
identifier_value must equal btrim(identifier_value), legal_name must equal
btrim(legal_name) and be non-blank, and country_code — when present — must
match ^[A-Z]{2}$. The service normalises before insert; the CHECKs make sure
that a raw-SQL caller cannot store 'lei' beside 'LEI', or ' LEI' beside 'LEI',
and defeat the UNIQUE. Recorded precisely: btrim() strips ASCII spaces only,
so the database guarantee covers case and space padding; Python's str.strip()
in the service covers all Unicode whitespace, so service-written rows always
satisfy the CHECK. Widening the CHECK to tabs and newlines is a later, explicit
decision, not something to slip in here.

legal_name is deliberately NOT unique. "Example Group Services Ltd",
"Example Group Services Limited" and "AWS EMEA SARL" are distinct rows until a
human, or a later reconciliation ticket, says otherwise. Fuzzy matching is out
of scope and would fabricate identity.

role_type is an explicit CHECK over exactly {financial_entity, ict_provider}.
Relationship-specific facts — direct provider, subcontractor, rank, signatory,
consumer — are NOT organisation roles and are not admitted here
(DORA_MODEL_V2.md §5.3, §9, §16, §20).

Historical migrations are not edited. Migration 018's _FORCED_TABLES tuple
records what 018 forced at the time; these tables force themselves.
"""

from alembic import op

revision = "z1a2b3c4"
down_revision = "y0z1a2b3"
branch_labels = None
depends_on = None

# The exact initial role vocabulary (DORA_MODEL_V2.md §5.3). Kept as a module
# constant so the CHECK below and any reader agree on the same two strings.
_INITIAL_ROLE_TYPES = ("financial_entity", "ict_provider")

_TABLES_CHILD_FIRST = (
    "dora_organization_roles",
    "dora_organization_identifiers",
    "dora_organizations",
)


def upgrade() -> None:
    """Create the three tables parent-first, then activate and force RLS on each."""
    _create_organizations_table()
    _create_identifiers_table()
    _create_roles_table()
    for table in reversed(_TABLES_CHILD_FIRST):
        _enable_and_force_rls(table)


def downgrade() -> None:
    """Drop the three tables child-first, returning the schema to head y0z1a2b3.

    Policies, constraints and indexes are dropped with their tables. Nothing
    here touches dora_register_entries, dora_submission_runs,
    dora_submission_windows or dora_reporting_windows.
    """
    for table in _TABLES_CHILD_FIRST:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def _create_organizations_table() -> None:
    """Create dora_organizations: canonical, tenant-owned legal-organisation identity."""
    op.execute(
        """
        CREATE TABLE dora_organizations (
            organization_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID        NOT NULL REFERENCES tenants(tenant_id),
            legal_name      TEXT        NOT NULL,
            country_code    VARCHAR(2)  NULL,
            is_active       BOOLEAN     NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_dora_organizations_tenant_org
                UNIQUE (tenant_id, organization_id),
            CONSTRAINT ck_dora_organizations_legal_name_canonical
                CHECK (legal_name = btrim(legal_name) AND legal_name <> ''),
            CONSTRAINT ck_dora_organizations_country_code_iso2
                CHECK (country_code IS NULL OR country_code ~ '^[A-Z]{2}$')
        )
        """
    )


def _create_identifiers_table() -> None:
    """Create dora_organization_identifiers with the composite FK and the per-tenant uniqueness."""
    op.execute(
        """
        CREATE TABLE dora_organization_identifiers (
            organization_identifier_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                  UUID        NOT NULL REFERENCES tenants(tenant_id),
            organization_id            UUID        NOT NULL,
            identifier_type            VARCHAR(64) NOT NULL,
            identifier_value           TEXT        NOT NULL,
            is_active                  BOOLEAN     NOT NULL DEFAULT TRUE,
            created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_dora_organization_identifiers_organization
                FOREIGN KEY (tenant_id, organization_id)
                REFERENCES dora_organizations (tenant_id, organization_id),
            CONSTRAINT uq_dora_organization_identifiers_tenant_type_value
                UNIQUE (tenant_id, identifier_type, identifier_value),
            CONSTRAINT ck_dora_organization_identifiers_type_canonical
                CHECK (identifier_type = upper(btrim(identifier_type))
                       AND identifier_type <> ''),
            CONSTRAINT ck_dora_organization_identifiers_value_canonical
                CHECK (identifier_value = btrim(identifier_value)
                       AND identifier_value <> '')
        )
        """
    )
    # The UNIQUE above leads with tenant_id but does not contain organization_id
    # at all, so it cannot serve the (tenant_id, organization_id) pair the FK
    # check and "list the identifiers of one organisation" both need.
    op.execute(
        "CREATE INDEX ix_dora_organization_identifiers_tenant_org "
        "ON dora_organization_identifiers (tenant_id, organization_id)"
    )


def _create_roles_table() -> None:
    """Create dora_organization_roles with the exact initial vocabulary and one-role-once uniqueness."""
    allowed = ", ".join(f"'{role}'" for role in _INITIAL_ROLE_TYPES)
    op.execute(
        f"""
        CREATE TABLE dora_organization_roles (
            organization_role_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id            UUID        NOT NULL REFERENCES tenants(tenant_id),
            organization_id      UUID        NOT NULL,
            role_type            VARCHAR(32) NOT NULL,
            is_active            BOOLEAN     NOT NULL DEFAULT TRUE,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_dora_organization_roles_organization
                FOREIGN KEY (tenant_id, organization_id)
                REFERENCES dora_organizations (tenant_id, organization_id),
            CONSTRAINT uq_dora_organization_roles_tenant_org_role
                UNIQUE (tenant_id, organization_id, role_type),
            CONSTRAINT ck_dora_organization_roles_role_type
                CHECK (role_type IN ({allowed}))
        )
        """
    )


def _enable_and_force_rls(table: str) -> None:
    """Enable RLS, force it for the owner, and attach the standard tenant policy.

    FORCE is applied in the same step as ENABLE so there is no window in which
    the owner role could bypass the policy — the same ordering migration 020
    uses.
    """
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation_policy ON {table}
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )
