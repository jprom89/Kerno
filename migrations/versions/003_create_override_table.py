"""Create the overrides table with ENUM types and Row-Level Security.

Plain-English summary
---------------------
Every time a compliance engineer corrects an AI recommendation, Kerno records
the decision as an override row. This migration creates the table that stores
those decisions, along with two database-level ENUM types that constrain the
allowed values for reviewer role and action type. Invalid values are rejected
by the database before the application can write them.

The ``justification_text`` column is created here from the start — there is no
separate ADD COLUMN migration. Anonymisation of this field happens in the
application layer (``src/services/anonymisation.py``) before the row is written;
the database column itself places no constraint on the content.

Row-Level Security is activated immediately on creation so there is never a
window where override rows are readable without a valid tenant context.

Defence-in-depth: the RLS policy here is the database-layer safety net. The
application-layer guard (``set_tenant_context()`` in ``src/db/rls.py``) is the
primary enforcement mechanism — both must be present. (CLAUDE.md Section 3,
LEARNING_PIPELINE_SPEC.md Section 3.2.)

Alembic revision chain:
  Revises: 002_create_embedding_table_with_rls (a1b2c3d4)
  Next:     004_create_audit_log_table (c3d4e5f6)

How to run or test
------------------
Apply:

    alembic upgrade b2c3d4e5

Roll back:

    alembic downgrade a1b2c3d4

Unit tests for the override flow live in
tests/unit/services/test_override_service.py.
Integration tests that verify RLS on overrides are in
tests/security/test_tenant_isolation.py (marked @pytest.mark.integration).
"""

from alembic import op

revision = "b2c3d4e5"
down_revision = "a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create ENUM types, overrides table, and apply RLS, in dependency order.

    ENUMs must be created before the table that uses them. The RLS policy must
    be created after the table. This order must not be changed.
    """
    _create_enum_types()
    _create_overrides_table()
    _apply_rls_to_overrides()


def downgrade() -> None:
    """Drop RLS policy, disable RLS, drop overrides table, then drop ENUM types.

    Order is the reverse of upgrade: policy first, then table, then ENUMs
    (a type in use by a table cannot be dropped before the table is gone).
    """
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON overrides")
    op.execute("ALTER TABLE overrides DISABLE ROW LEVEL SECURITY")
    op.drop_table("overrides")
    op.execute("DROP TYPE IF EXISTS action_type_enum")
    op.execute("DROP TYPE IF EXISTS reviewer_role_enum")


def _create_enum_types() -> None:
    """Create the reviewer_role and action_type ENUM types used by the overrides table.

    ENUMs are a database-level constraint: values not in the list are rejected
    by PostgreSQL before the application layer sees them. Must stay in sync with
    the REVIEWER_ROLES and ACTION_TYPES constants in src/models/override.py.
    """
    op.execute(
        "CREATE TYPE reviewer_role_enum AS ENUM ('vciso', 'fciso', 'internal_admin')"
    )
    op.execute(
        "CREATE TYPE action_type_enum AS ENUM ('approve', 'edit', 'reject')"
    )


def _create_overrides_table() -> None:
    """Create the overrides table with all production columns.

    ``override_id`` has a server-side default (gen_random_uuid()) as a fallback,
    but ``override_service.py`` generates it in Python before the INSERT so the
    audit log can reference it without a RETURNING round-trip. The
    ``reviewer_confidence_weight`` is set by the service layer, never from user
    input. ``justification_text`` is stored pre-anonymised.
    """
    op.execute(
        """
        CREATE TABLE overrides (
            override_id                UUID               PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                  UUID               NOT NULL REFERENCES tenants(tenant_id),
            reviewer_id                UUID               NOT NULL,
            reviewer_role              reviewer_role_enum NOT NULL,
            action_type                action_type_enum   NOT NULL,
            original_control_id        TEXT               NOT NULL,
            corrected_control_id       TEXT,
            justification_text         TEXT,
            reviewer_confidence_weight FLOAT              NOT NULL,
            created_at                 TIMESTAMPTZ        NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ON overrides (tenant_id)")


def _apply_rls_to_overrides() -> None:
    """Enable Row-Level Security on overrides and create the tenant isolation policy.

    The USING clause reads ``app.current_tenant_id``, which ``set_tenant_context()``
    sets via SET LOCAL before every transaction that touches this table. Rows
    belonging to any other tenant are invisible at the database level — not merely
    filtered by the application. (LEARNING_PIPELINE_SPEC.md Section 3.2.)
    """
    op.execute("ALTER TABLE overrides ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON overrides
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )
