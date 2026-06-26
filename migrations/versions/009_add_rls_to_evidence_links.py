"""Add RLS to control_evidence_links and add the removed_at soft-delete column.

Plain-English summary
---------------------
Document 11 created control_evidence_links as a schema stub with no RLS and
no soft-delete support. Document 12 (KER-104) activates the table for real use,
which requires two changes:

  1. A removed_at column (TIMESTAMPTZ NULL) so evidence_service.remove_link()
     can soft-delete links without destroying audit history. Active links have
     removed_at IS NULL; soft-deleted links have a timestamp.

  2. Row-Level Security so a tenant's engineer can only see their own links.
     The table has no direct tenant_id column, so the policy joins through
     context_records to find the tenant that owns the linked record:

       EXISTS (
         SELECT 1 FROM context_records cr
         WHERE cr.record_id = control_evidence_links.record_id
         AND cr.tenant_id = current_setting('app.current_tenant_id', true)::uuid
       )

     This is consistent with the RLS pattern on context_records itself
     (migration 007). Note: context_records RLS must be set before this policy
     is evaluated so that the EXISTS subquery is itself tenant-filtered.

Alembic revision chain:
  Revises: 008_create_control_tables (h3i4j5k6)
  Next:    (none — this is currently the head revision)

How to run or test
------------------
Apply:

    alembic upgrade i4j5k6l7

Roll back (drops policy, disables RLS, removes removed_at column):

    alembic downgrade h3i4j5k6

Unit tests that exercise the service layer live in:
    tests/unit/services/test_evidence_service.py
"""

from alembic import op

revision = "i4j5k6l7"
down_revision = "h3i4j5k6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add removed_at column, enable RLS, and create the tenant isolation policy.

    Order matters: the column must exist before the RLS policy references it
    indirectly (queries filter removed_at IS NULL). The policy is created last.
    """
    _add_removed_at_column()
    _enable_rls()
    _create_tenant_policy()


def downgrade() -> None:
    """Drop the tenant policy, disable RLS, and remove the removed_at column.

    The policy must be dropped before RLS is disabled — otherwise the DISABLE
    call would leave an orphaned policy definition in the catalogue.
    """
    op.execute(
        "DROP POLICY IF EXISTS tenant_isolation_policy ON control_evidence_links"
    )
    op.execute(
        "ALTER TABLE control_evidence_links DISABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE control_evidence_links DROP COLUMN removed_at"
    )


def _add_removed_at_column() -> None:
    """Add the removed_at TIMESTAMPTZ column for soft-delete support.

    NULL means the link is active. A non-NULL timestamp records when the link
    was soft-deleted by evidence_service.remove_link(). The column is nullable
    so existing rows (all active) require no backfill.
    """
    op.execute(
        "ALTER TABLE control_evidence_links "
        "ADD COLUMN removed_at TIMESTAMPTZ NULL"
    )


def _enable_rls() -> None:
    """Enable Row-Level Security on control_evidence_links.

    Must be called before creating any policy; without ENABLE ROW LEVEL
    SECURITY the CREATE POLICY statement would succeed but RLS would not be
    enforced for table owners.
    """
    op.execute(
        "ALTER TABLE control_evidence_links ENABLE ROW LEVEL SECURITY"
    )


def _create_tenant_policy() -> None:
    """Create the tenant isolation policy using an EXISTS subquery through context_records.

    control_evidence_links has no direct tenant_id column. The policy joins
    through context_records so only rows whose linked context_record belongs to
    the current tenant are visible. The current_setting call reads the session
    variable set by set_tenant_context() in src/db/rls.py.
    """
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON control_evidence_links
          USING (
            EXISTS (
              SELECT 1 FROM context_records cr
              WHERE cr.record_id = control_evidence_links.record_id
              AND cr.tenant_id = current_setting('app.current_tenant_id', true)::uuid
            )
          )
        """
    )
