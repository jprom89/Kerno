"""Create the tenant_embeddings table and activate Row-Level Security.

This is the most security-critical migration in the codebase. It creates the
table where every compliance control embedding is stored and then switches on
the PostgreSQL Row-Level Security policy that prevents one tenant's rows from
ever being returned in another tenant's queries.

Two separate database mechanisms protect the data (defence-in-depth):

  1. The application layer calls ``set_tenant_context()`` before every query,
     which sets the PostgreSQL session variable ``app.current_tenant_id``.
  2. The RLS policy created here reads that variable and automatically filters
     every SELECT, INSERT, UPDATE, and DELETE so only rows belonging to the
     current tenant are visible.

If the application guard (1) fails for any reason, the RLS policy (2) still
prevents the wrong data from being returned. Both must be present.

The ``bias_vector`` column stores the tenant's learned retrieval calibration
and is referenced by the calibrated similarity query in
``src/services/retrieval_service.py``. Its width must match the embedding model
output width (1536) so the two vectors can be directly compared.

Alembic revision chain:
  Revises: 001_create_tenant_table
  Next:     003_create_override_table

Plain-English summary
---------------------
This migration creates two tables (tenant_embeddings and retrieval_bias) and
turns on separate security policies that wall off each company's rows from every
other company. Rolling it back drops both policies and then both tables, in
reverse order.

How to run or test
------------------
Apply:

    alembic upgrade a1b2c3d4

Roll back:

    alembic downgrade 001

Integration tests that verify the RLS policies are active live in
tests/security/test_tenant_isolation.py (marked @pytest.mark.integration).
"""

from alembic import op

from config.constants import EMBEDDING_DIMENSION

# Alembic revision metadata.
revision = "a1b2c3d4"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create tenant_embeddings and retrieval_bias, then activate RLS on both.

    Steps in order (must not be reordered — policies must be created after tables):
      1. Ensure the pgvector extension is present.
      2. Create tenant_embeddings with correct vector column types via raw DDL.
      3. Enable RLS and create the isolation policy on tenant_embeddings.
      4. Create retrieval_bias (one row per tenant, stores the nightly calibration).
      5. Enable RLS and create the isolation policy on retrieval_bias.
    """
    # pgvector must be present before any vector column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- tenant_embeddings ---
    # Raw DDL so the vector columns get the correct PostgreSQL type.
    op.execute(
        f"""
        CREATE TABLE tenant_embeddings (
            embedding_id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             UUID        NOT NULL REFERENCES tenants(tenant_id),
            control_id            TEXT        NOT NULL,
            embedding             vector({EMBEDDING_DIMENSION}) NOT NULL,
            retrieval_bias_vector vector({EMBEDDING_DIMENSION}),
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ON tenant_embeddings (tenant_id)")
    op.execute("ALTER TABLE tenant_embeddings ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON tenant_embeddings
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )

    # --- retrieval_bias ---
    # One row per tenant. Updated nightly by the bias recalculation batch.
    # The UNIQUE constraint on tenant_id allows the batch to use ON CONFLICT upsert.
    op.execute(
        f"""
        CREATE TABLE retrieval_bias (
            id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id            UUID        NOT NULL UNIQUE REFERENCES tenants(tenant_id),
            bias_vector          vector({EMBEDDING_DIMENSION}) NOT NULL,
            override_count       INTEGER     NOT NULL DEFAULT 0,
            last_recalculated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ON retrieval_bias (tenant_id)")
    op.execute("ALTER TABLE retrieval_bias ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_bias_isolation_policy ON retrieval_bias
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )


def downgrade() -> None:
    """Remove both RLS policies and drop both tables, in reverse creation order.

    Policies must be dropped before the tables they protect. retrieval_bias
    is dropped first because it was created second; tenant_embeddings second.
    """
    op.execute(
        "DROP POLICY IF EXISTS tenant_bias_isolation_policy ON retrieval_bias"
    )
    op.execute("ALTER TABLE retrieval_bias DISABLE ROW LEVEL SECURITY")
    op.drop_table("retrieval_bias")
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_policy ON tenant_embeddings"
    )
    op.execute("ALTER TABLE tenant_embeddings DISABLE ROW LEVEL SECURITY")
    op.drop_table("tenant_embeddings")
