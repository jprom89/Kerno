"""Create the remediation routing tables with Row-Level Security (KER-110).

Plain-English summary
---------------------
When a control is confirmed as a gap, remediation is routed to a Jira assignee
with an SLA-based due date. Two tables support this:

  1. remediation_routing_rules — per-tenant configuration: which Jira account
     handles gaps in each control category and how many days the SLA allows.
     A row with control_category NULL is the tenant's default rule.
  2. remediation_tasks — one row per Jira issue created, tracking the control,
     the assignee snapshot, the due date, and the closure / re-review flags.
     The close-callback validates issue keys against this table, so a caller
     cannot flag arbitrary controls for re-review.

Both tables carry the same tenant_isolation_policy as every other tenant-owned
table (LEARNING_PIPELINE_SPEC.md Section 3.2).

Alembic revision chain:
  Revises: 016_harden_audit_log_ledger (q2r3s4t5)

How to run or test
------------------
Apply:     alembic upgrade r3s4t5u6
Roll back: alembic downgrade q2r3s4t5
"""

from alembic import op

revision = "r3s4t5u6"
down_revision = "q2r3s4t5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create both remediation tables, their indexes, and their RLS policies."""
    _create_routing_rules_table()
    _create_tasks_table()
    _apply_rls()


def downgrade() -> None:
    """Drop the RLS policies, then both tables. Routing config and task history are lost."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON remediation_routing_rules")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON remediation_tasks")
    op.execute("DROP TABLE IF EXISTS remediation_tasks")
    op.execute("DROP TABLE IF EXISTS remediation_routing_rules")


def _create_routing_rules_table() -> None:
    """Create remediation_routing_rules — per-tenant, per-category routing config.

    control_category is nullable: NULL marks the tenant's default rule, used
    when no category-specific rule exists.
    """
    op.execute(
        """
        CREATE TABLE remediation_routing_rules (
            rule_id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                UUID        NOT NULL REFERENCES tenants(tenant_id),
            control_category         TEXT,
            assignee_jira_account_id TEXT        NOT NULL,
            sla_days                 INTEGER     NOT NULL,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ON remediation_routing_rules (tenant_id, control_category)")


def _create_tasks_table() -> None:
    """Create remediation_tasks — one row per Jira remediation issue.

    assignee and due date are snapshotted at trigger time so later rule changes
    never silently rewrite the historical record.
    """
    op.execute(
        """
        CREATE TABLE remediation_tasks (
            task_id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                UUID        NOT NULL REFERENCES tenants(tenant_id),
            control_id               TEXT        NOT NULL,
            jira_issue_key           TEXT        NOT NULL,
            assignee_jira_account_id TEXT        NOT NULL,
            due_date                 DATE        NOT NULL,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            closed_at                TIMESTAMPTZ,
            re_review_flagged_at     TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX ON remediation_tasks (tenant_id, control_id)")
    op.execute("CREATE INDEX ON remediation_tasks (tenant_id, jira_issue_key)")


def _apply_rls() -> None:
    """Enable RLS with the standard tenant_isolation_policy on both tables."""
    for table in ("remediation_routing_rules", "remediation_tasks"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation_policy ON {table}
              USING (
                tenant_id = current_setting('app.current_tenant_id', true)::uuid
              )
            """
        )
