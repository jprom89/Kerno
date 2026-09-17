"""Force Row-Level Security on every tenant-scoped table (KER-113 hardening).

Plain-English summary
---------------------
PostgreSQL table owners silently bypass RLS policies unless FORCE ROW LEVEL
SECURITY is set. The dev and default deployment role (kerno_dev) owns every
table, so until this migration the tenant-isolation policies protected nothing
against the role the application actually connects as — the application-layer
tenant filters were the only working lock. This migration turns the database
safety net back on by forcing owners to obey the same policies.

Tables forced (every table carrying a tenant isolation policy):
  tenant_embeddings, retrieval_bias, overrides, audit_log, context_records,
  control_evidence_links, recommendations, dora_register_entries,
  dora_submission_runs, remediation_routing_rules, remediation_tasks
(retrieval_bias's policy is named tenant_bias_isolation_policy; all others are
tenant_isolation_policy. The policies themselves are unchanged.)

Deliberately NOT forced:
  tenants                 — carries no RLS policy: the login flow must look up a
                            tenant row by email before any tenant context exists.
  compliance_controls,
  control_crosswalks,
  dora_submission_windows — global platform reference data with no policies.

Consequence for privileged sessions: any INSERT/UPDATE/DELETE/SELECT on a
forced table now requires SET LOCAL app.current_tenant_id, including seed
scripts and test fixtures. Tooling that must operate cross-tenant sets the
context per tenant, one tenant at a time.

Alembic revision chain:
  Revises: 017_create_remediation_routing_rules (r3s4t5u6)

How to run or test
------------------
Apply:     alembic upgrade s4t5u6v7
Roll back: alembic downgrade r3s4t5u6

Verification: pytest tests/security/test_tenant_isolation.py -m integration -v
"""

from alembic import op

revision = "s4t5u6v7"
down_revision = "r3s4t5u6"
branch_labels = None
depends_on = None

# Every table that carries a tenant isolation RLS policy (see module docstring).
_FORCED_TABLES = (
    "tenant_embeddings",
    "retrieval_bias",
    "overrides",
    "audit_log",
    "context_records",
    "control_evidence_links",
    "recommendations",
    "dora_register_entries",
    "dora_submission_runs",
    "remediation_routing_rules",
    "remediation_tasks",
)


def upgrade() -> None:
    """Force owners to obey the existing RLS policies on every tenant-scoped table."""
    for table in _FORCED_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Restore owner bypass (NO FORCE). The policies themselves are untouched."""
    for table in _FORCED_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
