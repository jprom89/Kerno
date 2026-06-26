"""Create the audit_log table with Row-Level Security.

Plain-English summary
---------------------
Every override decision is permanently recorded in this table. Unlike the
overrides table (which can in principle be queried and updated by the
application), the audit log exists solely for accountability: once a row is
written, it must never change. The application enforces this by never issuing
UPDATE or DELETE on this table; in production the database role should be
granted INSERT-only permissions.

The table is intentionally denormalised: every auditable field from the
override record is copied here so an auditor can read a complete event from a
single row without performing any JOINs. Role changes or record corrections
after the fact must not silently alter the historical record, so the values
are snapshots captured at decision time.

``reviewer_role`` and ``action_type`` are stored as TEXT (not ENUMs) for two
reasons:
  1. Audit rows are historical records — if an ENUM value is ever removed or
     renamed, old audit rows should remain readable.
  2. The overrides table already enforces valid values at write time via its
     ENUMs; the audit log trusts that constraint.

Row-Level Security is applied so a compliance engineer from Tenant A cannot
read Tenant B's audit log — even though both sets of rows live in the same table.

Alembic revision chain:
  Revises: 003_create_override_table (b2c3d4e5)
  Next:     005_noop_justification_text (e5f6a7b8)

How to run or test
------------------
Apply:

    alembic upgrade c3d4e5f6

Roll back:

    alembic downgrade b2c3d4e5

Integration tests that verify the audit log is written on every override are in
tests/security/test_tenant_isolation.py (marked @pytest.mark.integration).
"""

from alembic import op

revision = "c3d4e5f6"
down_revision = "b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the audit_log table and activate its RLS policy.

    The table must be created before the policy — no other ordering constraint
    exists for this migration (the ENUM types it depends on were created in 003).
    """
    _create_audit_log_table()
    _apply_rls_to_audit_log()


def downgrade() -> None:
    """Drop the RLS policy, disable RLS, then drop the audit_log table.

    The policy must be dropped before the table. The overrides table (created
    in migration 003) is unaffected by this downgrade.
    """
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON audit_log")
    op.execute("ALTER TABLE audit_log DISABLE ROW LEVEL SECURITY")
    op.drop_table("audit_log")


def _create_audit_log_table() -> None:
    """Create the audit_log table with all production columns.

    ``override_id`` references the overrides table so each audit entry is
    traceable to its source override. The FK is intentional: an audit entry
    without a parent override should never exist.

    ``reviewer_role`` and ``action_type`` are TEXT, not ENUMs, to preserve the
    immutability guarantee: historical rows must remain readable even if the
    set of valid ENUM values changes in the future.

    ``justification_text`` mirrors the override column — already anonymised
    before reaching this table (see ``src/services/anonymisation.py``).
    """
    op.execute(
        """
        CREATE TABLE audit_log (
            id                   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            override_id          UUID        NOT NULL REFERENCES overrides(override_id),
            tenant_id            UUID        NOT NULL REFERENCES tenants(tenant_id),
            reviewer_id          UUID        NOT NULL,
            reviewer_role        TEXT        NOT NULL,
            action_type          TEXT        NOT NULL,
            original_control_id  TEXT        NOT NULL,
            corrected_control_id TEXT,
            justification_text   TEXT,
            timestamp            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ON audit_log (override_id)")
    op.execute("CREATE INDEX ON audit_log (tenant_id)")


def _apply_rls_to_audit_log() -> None:
    """Enable Row-Level Security on audit_log and create the tenant isolation policy.

    The policy uses the same session variable as every other RLS policy in this
    codebase: ``app.current_tenant_id``, set by ``set_tenant_context()`` in
    ``src/db/rls.py``. Rows belonging to other tenants are invisible at the
    database level. (LEARNING_PIPELINE_SPEC.md Section 3.2.)
    """
    op.execute("ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON audit_log
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )
