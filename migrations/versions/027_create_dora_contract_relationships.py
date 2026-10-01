"""Create dora_contract_relationships: the recorded parent/child hierarchy between two contracts.

What:  creates one tenant-owned table under ENABLE + FORCE ROW LEVEL SECURITY
       with the standard tenant_isolation_policy. A row records that, in one
       tenant, a child contract's overarching contractual arrangement is a
       parent contract. Composite (tenant_id, contract) foreign keys on both
       endpoints; a CHECK against direct self-links; a CHECK limiting the type
       to 'overarching'; a partial unique index allowing one active parent per
       child; and the two indexes the reads need.
Why:   DORA_MODEL_V2.md §7.3 — one canonical hierarchy representation, so
       dora_contracts gains no parent or overarching column. This is the
       DORA-V2-002B slice and nothing more: no costs, services, functions,
       supply chain, classification or regulatory projection. It records
       operational links only; it does not produce B_02.01.0020 or
       B_02.01.0030, and a contract with no recorded parent is not thereby
       standalone. The physical shape is a Kerno engineering choice, not a
       regulator-prescribed schema.
How:   python scripts/migrate_test_database.py upgrade head     (test database)
       python scripts/migrate_test_database.py downgrade a2b3c4d5
       Proven by tests/integration/test_dora_v2_002b_*.py and
       tests/security/test_dora_contract_relationship_isolation.py against a
       live database.

Alembic revision chain:
  Revises: a2b3c4d5 (026_create_dora_contract_foundation)
  Next:    (none - this is the head revision)

Design decisions recorded here because the migration is where they take effect
----------------------------------------------------------------------------

At most one active parent per child. That is a Kerno modelling constraint for
this slice, not a reading of the regulation: a real case with two plausible
parents needs review and is not resolved by picking one. The partial unique
index uq_dora_contract_relationships_one_active_parent enforces it in the
database for every writer, raw SQL included. Inactive rows are history and are
excluded from it, so a child may be relinked any number of times.

The self-link CHECK covers only a contract named as its own parent. A cycle
through two or more rows cannot be expressed as a row constraint; the hierarchy
service refuses it, serialised per tenant, and nothing in the database stops
the table owner from writing one with raw SQL.

Composite foreign keys, as in migrations 025 and 026: PostgreSQL's referential
checks bypass RLS, so a single-column FK would let a Tenant A link point at a
Tenant B contract; the (tenant_id, contract_id) candidate key cannot.

Indexes follow the access paths without duplicating one another. The partial
unique index serves "this child's active parent" (and each step of the cycle
check). It excludes inactive rows, so a child's full history needs its own
(tenant_id, child_contract_id) index, which also serves the child FK. A
parent's children use (tenant_id, parent_contract_id), which also serves the
parent FK.

History is append-and-deactivate. Endpoints and type never change, an inactive
row is never reactivated, and no row is deleted — by the service. No trigger
enforces this here; it is a service guarantee, like the cycle rule. Historical
migrations are not edited.
"""

from alembic import op

revision = "b3c4d5e6"
down_revision = "a2b3c4d5"
branch_labels = None
depends_on = None

_TABLE = "dora_contract_relationships"


def upgrade() -> None:
    """Create the relationship table and its indexes, then activate and force RLS."""
    _create_relationships_table()
    _create_indexes()
    _enable_and_force_rls(_TABLE)


def downgrade() -> None:
    """Drop the relationship table, returning the schema to head a2b3c4d5.

    Its policy, constraints and indexes go with it. Nothing here touches
    dora_contracts, dora_contract_parties or any other table.
    """
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")


def _create_relationships_table() -> None:
    """Create dora_contract_relationships with both composite FKs and both CHECKs."""
    op.execute(
        f"""
        CREATE TABLE {_TABLE} (
            contract_relationship_id UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                UUID        NOT NULL REFERENCES tenants(tenant_id),
            child_contract_id        UUID        NOT NULL,
            parent_contract_id       UUID        NOT NULL,
            relationship_type        TEXT        NOT NULL,
            is_active                BOOLEAN     NOT NULL DEFAULT TRUE,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT fk_dora_contract_relationships_child
                FOREIGN KEY (tenant_id, child_contract_id)
                REFERENCES dora_contracts (tenant_id, contract_id),
            CONSTRAINT fk_dora_contract_relationships_parent
                FOREIGN KEY (tenant_id, parent_contract_id)
                REFERENCES dora_contracts (tenant_id, contract_id),
            CONSTRAINT ck_dora_contract_relationships_not_self
                CHECK (child_contract_id <> parent_contract_id),
            CONSTRAINT ck_dora_contract_relationships_type
                CHECK (relationship_type = 'overarching')
        )
        """
    )


def _create_indexes() -> None:
    """Create the one-active-parent partial unique index and the child and parent lookups."""
    op.execute(
        f"CREATE UNIQUE INDEX uq_dora_contract_relationships_one_active_parent "
        f"ON {_TABLE} (tenant_id, child_contract_id, relationship_type) WHERE is_active"
    )
    op.execute(
        f"CREATE INDEX ix_dora_contract_relationships_tenant_child "
        f"ON {_TABLE} (tenant_id, child_contract_id)"
    )
    op.execute(
        f"CREATE INDEX ix_dora_contract_relationships_tenant_parent "
        f"ON {_TABLE} (tenant_id, parent_contract_id)"
    )


def _enable_and_force_rls(table: str) -> None:
    """Enable RLS, force it for the owner, and attach the standard tenant policy.

    Same statements, same order and same policy expression as migrations 020,
    025 and 026.
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
