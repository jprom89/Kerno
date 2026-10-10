"""Bind each new review decision to the exact recommendation it reviewed (SEC-REMED-005).

What:  adds a nullable overrides.recommendation_id; a UNIQUE
       (tenant_id, recommendation_id, control_id) candidate key on
       recommendations; a composite foreign key from overrides
       (tenant_id, recommendation_id, original_control_id) to that key; and the
       index that "decisions bound to this recommendation" reads use. No table
       is created, no row is written and no existing value changes.
Why:   finding integrity.stale-recommendation-approval. A decision that names
       only a control cannot say which recommendation a human approved, so an
       approval of R1 was read as an approval of R2 once R2 replaced it. The
       composite key makes the database refuse a binding to another tenant's
       recommendation or to another control's.
How:   python scripts/migrate_test_database.py upgrade head     (test database)
       python scripts/migrate_test_database.py downgrade b3c4d5e6
       Proven by tests/integration/test_sec_remed_005_*.py against a live
       database.

Alembic revision chain:
  Revises: b3c4d5e6 (027_create_dora_contract_relationships)
  Next:    (none - this is the head revision)

Design decisions recorded here because the migration is where they take effect
----------------------------------------------------------------------------

Existing overrides stay unbound. Their recommendation_id is NULL and, under the
default MATCH SIMPLE, a NULL in any foreign-key column skips the check. Nothing
is backfilled: a binding inferred from timestamps, or one attached to the
latest recommendation, would be invented. The services treat an unbound
decision as history that confirms nothing.

The candidate key is a superset of the primary key, so it always holds. It
exists because a composite foreign key needs a unique target that covers all
of its columns. The key is composite, and not recommendation_id alone, for the
reason recorded in migrations 025-027: PostgreSQL's referential checks bypass
RLS, so a single-column key would let a Tenant A decision point at a Tenant B
recommendation. Including the control makes the same refusal hold for a
decision filed against one control that names another control's
recommendation.

Currency is not a constraint. Whether the reviewed recommendation is still its
control's current one changes after the decision is written (is_superseded
flips), so no row constraint can express it. The review service enforces it
under a per-(tenant, control) advisory lock; see recommendation_service.

The index leads with (tenant_id, recommendation_id), which serves both the
foreign key and the coverage read of a recommendation's newest decision;
created_at follows for that read's ordering.

No new table, so no new RLS: both tables keep ENABLE + FORCE and their
unchanged tenant_isolation_policy. The downgrade drops everything this
migration added, in reverse; it discards every binding recorded since the
upgrade, while the decisions themselves survive, unbound. Historical
migrations are not edited.
"""

from alembic import op

revision = "c4d5e6f7"
down_revision = "b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the binding column, its candidate key, the composite foreign key and its index."""
    op.execute(
        "ALTER TABLE recommendations ADD CONSTRAINT "
        "uq_recommendations_tenant_recommendation_control "
        "UNIQUE (tenant_id, recommendation_id, control_id)"
    )
    op.execute("ALTER TABLE overrides ADD COLUMN recommendation_id UUID")
    op.execute(
        "ALTER TABLE overrides ADD CONSTRAINT fk_overrides_reviewed_recommendation "
        "FOREIGN KEY (tenant_id, recommendation_id, original_control_id) "
        "REFERENCES recommendations (tenant_id, recommendation_id, control_id)"
    )
    op.execute(
        "CREATE INDEX ix_overrides_tenant_recommendation "
        "ON overrides (tenant_id, recommendation_id, created_at)"
    )


def downgrade() -> None:
    """Drop what upgrade() added, in reverse order; recorded bindings are lost."""
    op.execute("DROP INDEX IF EXISTS ix_overrides_tenant_recommendation")
    op.execute("ALTER TABLE overrides DROP CONSTRAINT IF EXISTS fk_overrides_reviewed_recommendation")
    op.execute("ALTER TABLE overrides DROP COLUMN IF EXISTS recommendation_id")
    op.execute(
        "ALTER TABLE recommendations DROP CONSTRAINT IF EXISTS "
        "uq_recommendations_tenant_recommendation_control"
    )
