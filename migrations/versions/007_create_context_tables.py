"""Creates the context_records table that stores per-tenant ingested evidence records.
Migration 008 (h3i4j5k6) creates a FK to context_records(record_id) and must run after this one."""

from alembic import op

revision = "g2h3i4j5"
down_revision = "f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create context_records with indexes, RLS, and a GIN index for full-text search."""
    _create_context_records()
    _enable_rls()
    _create_tenant_policy()
    _create_fts_index()


def downgrade() -> None:
    """Drop the tenant policy, disable RLS, and drop context_records."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON context_records")
    op.execute("ALTER TABLE context_records DISABLE ROW LEVEL SECURITY")
    op.drop_table("context_records")


def _create_context_records() -> None:
    op.execute(
        """
        CREATE TABLE context_records (
            record_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID         NOT NULL REFERENCES tenants(tenant_id),
            source_system   VARCHAR(64)  NOT NULL,
            external_id     VARCHAR(255),
            record_type     VARCHAR(64)  NOT NULL,
            title           TEXT,
            body            TEXT,
            fetched_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
            content_hash    VARCHAR(64),
            is_deleted      BOOLEAN      NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ON context_records (tenant_id)")
    op.execute("CREATE INDEX ON context_records (source_system)")
    op.execute("CREATE INDEX ON context_records (record_type)")


def _enable_rls() -> None:
    op.execute("ALTER TABLE context_records ENABLE ROW LEVEL SECURITY")


def _create_tenant_policy() -> None:
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON context_records
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )


def _create_fts_index() -> None:
    # Expression must match exactly what full_text_search_service uses in to_tsvector calls.
    op.execute(
        """
        CREATE INDEX ON context_records
        USING GIN (
            to_tsvector('english',
                coalesce(title, '') || ' ' || coalesce(body, ''))
        )
        """
    )
