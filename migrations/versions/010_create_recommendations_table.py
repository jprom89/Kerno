"""Create the recommendations table for KER-105 (Document 13).

What:  Creates the recommendations table, which stores one explainable
       compliance recommendation per (tenant_id, control_id) pair at any
       given time. Older recommendations for the same pair are kept with
       is_superseded=True for audit history.

Why:   KER-105 requires that every recommendation be persisted with its
       full input snapshot so it can be reproduced without querying other
       tables (AC-4), and that low-confidence outputs be flagged for human
       review (AC-3). The table is the durable store for all four acceptance
       criteria.

How to run or test:
    alembic upgrade j5k6l7m8

Roll back (drops the table entirely):
    alembic downgrade i4j5k6l7

Unit tests that exercise the service layer live in:
    tests/unit/services/test_recommendation_service.py
    tests/unit/services/test_scoring.py
"""

from alembic import op

revision = "j5k6l7m8"
down_revision = "i4j5k6l7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the recommendations table with RLS and a composite index.

    Runs each sub-step in dependency order: table first, then index,
    then RLS activation, then the tenant isolation policy.
    """
    _create_table()
    _create_index()
    _enable_rls()
    _create_tenant_policy()


def downgrade() -> None:
    """Drop the recommendations table and all associated objects.

    Dropping the table implicitly drops the index and RLS policies.
    """
    op.execute("DROP TABLE IF EXISTS recommendations")


def _create_table() -> None:
    """Create the recommendations table with all fields from §3.3.

    evidence_ids is stored as TEXT[] so the array of record_id strings
    is persisted without requiring a separate join table. input_snapshot
    is stored as JSONB so the full evidence state at generation time is
    queryable and auditable without touching other tables (AC-4).
    """
    op.execute(
        """
        CREATE TABLE recommendations (
            recommendation_id  UUID        NOT NULL,
            tenant_id          UUID        NOT NULL,
            control_id         TEXT        NOT NULL,
            status             VARCHAR(16) NOT NULL,
            confidence_level   VARCHAR(16) NOT NULL,
            confidence_score   FLOAT       NOT NULL,
            rationale          TEXT        NOT NULL,
            gaps               TEXT,
            evidence_ids       TEXT[]      NOT NULL,
            requires_review    BOOLEAN     NOT NULL,
            input_snapshot     JSONB       NOT NULL,
            generated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            is_superseded      BOOLEAN     NOT NULL DEFAULT FALSE,
            PRIMARY KEY (recommendation_id)
        )
        """
    )


def _create_index() -> None:
    """Create a composite index for fast current-recommendation lookups.

    The query pattern is: WHERE tenant_id = X AND control_id = Y AND
    is_superseded = FALSE. This index covers all three columns so the
    lookup is a fast index scan rather than a sequential scan of the table.
    """
    op.execute(
        """
        CREATE INDEX idx_recommendations_current
        ON recommendations (tenant_id, control_id, is_superseded)
        """
    )


def _enable_rls() -> None:
    """Enable Row-Level Security on the recommendations table.

    Must be called before creating any policy; without ENABLE ROW LEVEL
    SECURITY the CREATE POLICY statement would succeed but RLS would not
    be enforced for table owners.
    """
    op.execute(
        "ALTER TABLE recommendations ENABLE ROW LEVEL SECURITY"
    )


def _create_tenant_policy() -> None:
    """Create the tenant isolation policy using the direct tenant_id column.

    recommendations has a direct tenant_id column, so the policy reads it
    directly — the same pattern as context_records and other RLS tables.
    The current_setting call reads the session variable set by
    set_tenant_context() in src/db/rls.py.
    """
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON recommendations
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )
