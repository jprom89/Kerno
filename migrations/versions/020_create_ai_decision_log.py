"""Create the ai_decision_log table — retained record of every AI mapping decision (KER-203).

Alembic revision chain:
  Revises: t5u6v7w8 (019_create_users_table)
  Next:    (none — this is the head revision)

Plain-English summary
---------------------
Every time the AI produces a compliance recommendation, Kerno must keep a
retained, queryable record of that decision: which control, which evidence,
what the model said, how confident it was, and which model version said it.
EU AI Act Articles 12/19/26 require this record-keeping for high-risk AI
systems (Annex III deadline: 2 December 2027); NIS2/DORA enterprise buyers ask
for it during procurement. This migration creates that table.

This log is SEPARATE from the KER-107 human-decision audit ledger: it is
append-only in practice but NOT hash-chained — it has a different volume,
retention (AI_DECISION_LOG_RETENTION_DAYS = 180 days, pruned nightly), and
query profile. GDPR alignment: the table stores input_snapshot_hash (SHA-256
of the canonical input JSON) — never the raw snapshot — so no personal data
enters the log.

Column-type correction (decided 10 July 2026, documented per §11): the §13
spec typed control_id as UUID and evidence_ids as UUID[], but the entire
existing pipeline keys controls and evidence by TEXT refs
(recommendations.control_id TEXT, recommendations.evidence_ids TEXT[],
overrides.original_control_id TEXT, tenant_embeddings.control_id TEXT).
UUID columns would have made the same-transaction insert (AC-2) fail for
every existing control ref. control_id is therefore TEXT and evidence_ids
TEXT[], matching the recommendation row each log entry describes.
correlation_id stays UUID (Kerno-generated), tenant_id stays UUID (real FK
to tenants).

Row-Level Security: ENABLE + FORCE + tenant_isolation_policy. This is a pure
tenant-data table only ever read or written under an authenticated tenant
context, so the users-table auth-bootstrap exception (migration 019) does NOT
apply — even the table-owner role obeys the policy, matching migration 018.

How to run or test
------------------
Apply:      alembic upgrade u6v7w8x9   (or: alembic upgrade head)
Roll back:  alembic downgrade t5u6v7w8
Verified by tests/integration/test_ker203_ai_decision_log.py (live DB).
"""

from alembic import op

revision = "u6v7w8x9"
down_revision = "t5u6v7w8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create ai_decision_log with FORCE RLS, the tenant policy, and its three indexes.

    Order matters: table first, then RLS activation, then the policy, then the
    indexes. FORCE is applied in the same step as ENABLE so there is no window
    where the owner role could bypass the policy.
    """
    op.execute(
        """
        CREATE TABLE ai_decision_log (
            correlation_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID        NOT NULL REFERENCES tenants(tenant_id),
            control_id          TEXT        NOT NULL,
            evidence_ids        TEXT[]      NOT NULL,
            input_snapshot_hash TEXT        NOT NULL,
            output_status       TEXT        NOT NULL,
            confidence_score    FLOAT       NOT NULL,
            rationale_extract   TEXT        NOT NULL,
            model_version       TEXT        NOT NULL,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE ai_decision_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ai_decision_log FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON ai_decision_log
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )
    op.execute(
        "CREATE INDEX ix_ai_decision_log_tenant_created "
        "ON ai_decision_log (tenant_id, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_ai_decision_log_control ON ai_decision_log (control_id)"
    )
    op.execute(
        "CREATE INDEX ix_ai_decision_log_confidence "
        "ON ai_decision_log (confidence_score)"
    )


def downgrade() -> None:
    """Drop the ai_decision_log table and everything attached to it.

    The policy and indexes are dropped implicitly with the table. Rolling back
    deletes all retained AI-decision records — acceptable only outside
    production once the Article 19 retention duty is active.
    """
    op.execute("DROP TABLE ai_decision_log")
