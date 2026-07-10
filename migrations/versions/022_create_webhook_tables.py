"""Create the webhook_registrations and webhook_ingest_dedup tables (KER-205).

Alembic revision chain:
  Revises: v7w8x9y0 (021_add_trust_center_fields)
  Next:    (none — this is the head revision)

Plain-English summary
---------------------
Generic webhook ingestion (KER-205) needs two tables. webhook_registrations
holds each tenant's registered source systems and the HMAC signing secret
that authenticates their deliveries. webhook_ingest_dedup remembers which
(source_system, external_ref) pairs a tenant has recently received, so a
repeat delivery inside the WEBHOOK_DEDUP_WINDOW_HOURS window is acknowledged
without being processed twice.

Signing-secret storage (§13 KER-205 decision 1, decided 9 July 2026):
signing_secret is stored as PLAINTEXT. HMAC verification requires the raw
secret — it cannot be derived from a hash. Compensating controls: the column
sits behind RLS; the API returns the secret exactly once (the 201 creation
response); no read endpoint ever returns it again; and a dedicated rotate
endpoint overwrites it. At-rest column encryption (pgcrypto) is deferred to
Sprint 3.

Row-Level Security — deliberately different per table (§13 KER-205
decision 2):
  * webhook_registrations: ENABLE, NOT FORCE, context-optional policy — the
    exact users-table pattern from migration 019. The ingest endpoint is
    unauthenticated (the signature IS the authentication), so the
    registration lookup must run BEFORE any tenant context exists. Without
    FORCE the owner role bypasses the policy, so isolation for this table
    additionally relies on the fact that only the ingest lookup reads it
    pre-context and every management query filters by the JWT tenant.
  * webhook_ingest_dedup: ENABLE + FORCE + tenant_isolation_policy — it is
    only ever touched AFTER the tenant is resolved, so it gets the full
    migration-018 treatment.

The UNIQUE (tenant_id, source_system, external_ref) constraint makes the
dedup check race-safe; re-ingestion after the window is handled by the
service upserting received_at rather than inserting a second row.

How to run or test
------------------
Apply:      alembic upgrade w8x9y0z1   (or: alembic upgrade head)
Roll back:  alembic downgrade v7w8x9y0
Verified by tests/unit/api/test_webhooks.py and the dev-DB checks in the §11
review block for this file.
"""

from alembic import op

revision = "w8x9y0z1"
down_revision = "v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create both webhook tables with their per-table RLS postures.

    Order: registrations table, its RLS + context-optional policy; then the
    dedup table, its RLS + FORCE + tenant policy, and the dedup uniqueness
    constraint's supporting index.
    """
    op.execute(
        """
        CREATE TABLE webhook_registrations (
            id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id      UUID        NOT NULL REFERENCES tenants(tenant_id),
            source_system  TEXT        NOT NULL,
            signing_secret TEXT        NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            is_active      BOOLEAN     NOT NULL DEFAULT TRUE
        )
        """
    )
    op.execute("CREATE INDEX ix_webhook_registrations_tenant ON webhook_registrations (tenant_id)")
    op.execute("ALTER TABLE webhook_registrations ENABLE ROW LEVEL SECURITY")
    # NOT FORCE — auth-bootstrap exception (migration 019 pattern): the ingest
    # lookup runs before any tenant context exists. NULLIF keeps the ::uuid
    # cast from erroring on an empty setting.
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON webhook_registrations
          USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', TRUE), '')::uuid
            OR current_setting('app.current_tenant_id', TRUE) IS NULL
            OR current_setting('app.current_tenant_id', TRUE) = ''
          )
        """
    )

    op.execute(
        """
        CREATE TABLE webhook_ingest_dedup (
            id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     UUID        NOT NULL REFERENCES tenants(tenant_id),
            source_system TEXT        NOT NULL,
            external_ref  TEXT        NOT NULL,
            received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_webhook_dedup_tenant_source_ref
                UNIQUE (tenant_id, source_system, external_ref)
        )
        """
    )
    op.execute("ALTER TABLE webhook_ingest_dedup ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE webhook_ingest_dedup FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON webhook_ingest_dedup
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )


def downgrade() -> None:
    """Drop both webhook tables (policies and constraints drop with them)."""
    op.execute("DROP TABLE webhook_ingest_dedup")
    op.execute("DROP TABLE webhook_registrations")
