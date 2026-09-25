"""Create the DORA v2 contract foundation: contracts and their signing parties.

What:  creates dora_contracts and dora_contract_parties — tenant-owned, each
       under ENABLE + FORCE ROW LEVEL SECURITY with the standard
       tenant_isolation_policy, with composite (tenant_id, …) foreign keys so a
       party row can never point at another tenant's contract or organisation.
Why:   DORA_MODEL_V2.md §7.1, §7.2 and §20. A contract is a reusable record
       that several organisations sign and that one organisation may sign many
       times over; who signs is a different fact from who consumes a service.
       This is the DORA-V2-002A slice of §36 and nothing more: no contract
       hierarchy, no costs, no ICT services, no functions, no service usages,
       no provenance columns, no template columns. The physical shape is a
       Kerno engineering choice, not a regulator-prescribed schema.
How:   alembic upgrade a2b3c4d5   (roll back: alembic downgrade z1a2b3c4)
       Proven by tests/security/test_dora_contract_isolation.py,
       tests/integration/test_dora_v2_002a_contracts.py,
       tests/integration/test_dora_v2_002a_concurrency.py and
       tests/integration/test_dora_v2_002a_schema_parity.py against a live
       database.

Alembic revision chain:
  Revises: z1a2b3c4 (025_create_dora_organization_foundation)
  Next:    (none - this is the head revision)

Design decisions recorded here because the migration is where they take effect
----------------------------------------------------------------------------

One whitespace policy, enforced identically on both sides. A contract
reference is identity within a tenant, so "MSA-1" and "MSA-1\\t" must not be
able to coexist. Migration 025's btrim() strips ASCII spaces only while the
organisation service strips all Unicode whitespace — two guarantees that do
not match. Here both sides use one explicit set: the 29 characters Python's
str.isspace() accepts (tab, LF, VT, FF, CR, the four information separators,
space, NEL, NBSP, the Unicode space separators, and the line and paragraph
separators). The CHECK passes that set to btrim() as \\u escapes; the service
passes the same set, spelled out in src/models/dora_contract.py, to
str.strip(). Only the two ends are canonicalised: case, punctuation and any
interior whitespace are preserved exactly. display_name follows the same rule
and is NULL rather than empty when not supplied, so there is one way to say
"no name", not two.

What the policy does not do, stated so it is not assumed. It trims; it does
not normalise. "Café" in NFC and in NFD, a reference with a trailing zero-width
space (U+200B) or a leading byte-order mark (U+FEFF), and full-width
"ＭＳＡ-1" beside "MSA-1" are distinct references, and a reference made only of
invisible non-whitespace characters is not blank. Unicode normalisation would
itself rewrite punctuation the ticket requires to be preserved (NFC maps
U+037E GREEK QUESTION MARK to ";"), so it is not applied here; spotting
look-alike references belongs to import staging and reconciliation
(DORA-V2-004), not to the identity constraint.

The reference is bounded at 255 characters (ck_dora_contracts_reference_length)
so an over-long value is a refusal, not a btree "index row size exceeds
maximum" error that aborts the caller's transaction. A Kerno engineering bound,
not a regulatory one.

Dates are contract-level and optional. NULL means not supplied, never a
derived conclusion; the only rule is that an end date may not precede a start
date when both exist.

Composite foreign keys, as in migration 025. dora_contracts carries UNIQUE
(tenant_id, contract_id) as the candidate key that
dora_contract_parties references, and the party row references
dora_organizations by its (tenant_id, organization_id) candidate key.
PostgreSQL's referential checks bypass RLS, so a single-column FK would accept
a party in Tenant A pointing at Tenant B's contract; the composite FK cannot.

Uniqueness is the database's final word, not the service's pre-check.
uq_dora_contracts_tenant_reference and uq_dora_contract_parties_tenant_tuple
are the constraints the service names in INSERT ... ON CONFLICT ON CONSTRAINT
... DO NOTHING, so a concurrent duplicate becomes a controlled conflict rather
than a driver exception or a second row.

Indexes follow the access paths. Parties of one contract and the
(tenant_id, contract_id) FK are served by the leading columns of the party
UNIQUE; the only explicit index is the reverse lookup — contracts signed by
one organisation — on (tenant_id, organization_id, contract_id), which also
serves the organisation FK.

party_role is an explicit CHECK over exactly {recipient_signatory,
provider_signatory, intragroup_provider_signatory}. Signing is not consuming
(§20): nothing here records who uses a service. Historical migrations are not
edited.
"""

from alembic import op

revision = "a2b3c4d5"
down_revision = "z1a2b3c4"
branch_labels = None
depends_on = None

# The exact initial party-role vocabulary (DORA_MODEL_V2.md §7.2).
_INITIAL_PARTY_ROLES = (
    "recipient_signatory",
    "provider_signatory",
    "intragroup_provider_signatory",
)

# The longest contract_reference, in characters. Hardcoded for the same
# reason as the trim set below; must equal
# config.constants.CONTRACT_REFERENCE_MAX_CHARACTERS (drift fails
# tests/unit/models/test_dora_contract_models.py).
_REFERENCE_MAX_CHARACTERS = 255

# The whitespace set trimmed from both ends of contract text, as a PostgreSQL
# escape-string literal. Hardcoded here on purpose — a migration is history
# and must not import application code — and must equal
# CONTRACT_TEXT_TRIM_CHARACTERS in src/models/dora_contract.py;
# tests/unit/models/test_dora_contract_models.py and the live CHECK tests fail
# on drift.
_TRIM_CHARACTERS_SQL = (
    "E'\\u0009\\u000a\\u000b\\u000c\\u000d\\u001c\\u001d\\u001e\\u001f"
    "\\u0020\\u0085\\u00a0\\u1680\\u2000\\u2001\\u2002\\u2003\\u2004\\u2005"
    "\\u2006\\u2007\\u2008\\u2009\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'"
)

_TABLES_CHILD_FIRST = (
    "dora_contract_parties",
    "dora_contracts",
)


def upgrade() -> None:
    """Create the two tables parent-first, then activate and force RLS on each."""
    _create_contracts_table()
    _create_contract_parties_table()
    for table in reversed(_TABLES_CHILD_FIRST):
        _enable_and_force_rls(table)


def downgrade() -> None:
    """Drop the two tables child-first, returning the schema to head z1a2b3c4.

    Policies, constraints and indexes are dropped with their tables. Nothing
    here touches the V2-001 organisation tables or any DORA v1 table.
    """
    for table in _TABLES_CHILD_FIRST:
        op.execute(f"DROP TABLE IF EXISTS {table}")


def _create_contracts_table() -> None:
    """Create dora_contracts: a tenant-owned contract identified by its reference."""
    op.execute(
        f"""
        CREATE TABLE dora_contracts (
            contract_id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID        NOT NULL REFERENCES tenants(tenant_id),
            contract_reference  TEXT        NOT NULL,
            display_name        TEXT        NULL,
            contract_start_date DATE        NULL,
            contract_end_date   DATE        NULL,
            is_active           BOOLEAN     NOT NULL DEFAULT TRUE,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_dora_contracts_tenant_contract
                UNIQUE (tenant_id, contract_id),
            CONSTRAINT uq_dora_contracts_tenant_reference
                UNIQUE (tenant_id, contract_reference),
            CONSTRAINT ck_dora_contracts_reference_canonical
                CHECK (contract_reference = btrim(contract_reference, {_TRIM_CHARACTERS_SQL})
                       AND contract_reference <> ''),
            CONSTRAINT ck_dora_contracts_reference_length
                CHECK (char_length(contract_reference) <= {_REFERENCE_MAX_CHARACTERS}),
            CONSTRAINT ck_dora_contracts_display_name_canonical
                CHECK (display_name IS NULL
                       OR (display_name = btrim(display_name, {_TRIM_CHARACTERS_SQL})
                           AND display_name <> '')),
            CONSTRAINT ck_dora_contracts_date_order
                CHECK (contract_start_date IS NULL
                       OR contract_end_date IS NULL
                       OR contract_end_date >= contract_start_date)
        )
        """
    )


def _create_contract_parties_table() -> None:
    """Create dora_contract_parties with both composite FKs, the role CHECK and the reverse index."""
    allowed = ", ".join(f"'{role}'" for role in _INITIAL_PARTY_ROLES)
    op.execute(
        f"""
        CREATE TABLE dora_contract_parties (
            contract_party_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id         UUID        NOT NULL REFERENCES tenants(tenant_id),
            contract_id       UUID        NOT NULL,
            organization_id   UUID        NOT NULL,
            party_role        VARCHAR(32) NOT NULL,
            is_active         BOOLEAN     NOT NULL DEFAULT TRUE,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_dora_contract_parties_contract
                FOREIGN KEY (tenant_id, contract_id)
                REFERENCES dora_contracts (tenant_id, contract_id),
            CONSTRAINT fk_dora_contract_parties_organization
                FOREIGN KEY (tenant_id, organization_id)
                REFERENCES dora_organizations (tenant_id, organization_id),
            CONSTRAINT uq_dora_contract_parties_tenant_tuple
                UNIQUE (tenant_id, contract_id, organization_id, party_role),
            CONSTRAINT ck_dora_contract_parties_party_role
                CHECK (party_role IN ({allowed}))
        )
        """
    )
    # Contracts signed by one organisation. The UNIQUE above leads with
    # (tenant_id, contract_id) and so cannot serve a lookup by organisation.
    op.execute(
        "CREATE INDEX ix_dora_contract_parties_tenant_org_contract "
        "ON dora_contract_parties (tenant_id, organization_id, contract_id)"
    )


def _enable_and_force_rls(table: str) -> None:
    """Enable RLS, force it for the owner, and attach the standard tenant policy.

    Same statements, same order and same policy expression as migrations 020
    and 025.
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
