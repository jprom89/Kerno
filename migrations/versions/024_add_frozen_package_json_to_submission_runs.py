"""Store the DORA filing JSON on the submission run that recorded it.

What:  adds nullable TEXT column frozen_package_json to dora_submission_runs.
Why:   a download that rebuilt from today's register would change when someone
       edited a vendor. The file an auditor hashes must be the package frozen
       at Start-run. TEXT, not JSONB: JSONB normalises key order and numbers,
       so a hash of the downloaded file would not match a JSONB round-trip.
How:   alembic upgrade y0z1a2b3
       Roll back: alembic downgrade x9y0z1a2
       Proven by tests/integration/test_frozen_filing_download.py against a
       live database.

The table already has ENABLE + FORCE ROW LEVEL SECURITY (migration 012/018).
This column inherits that. NULL is the honest value for pre-migration rows
and for create_submission_run drafts that never built a package; a download
of NULL is the same 404 as a missing run.
"""

from alembic import op

revision = "y0z1a2b3"
down_revision = "x9y0z1a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add frozen_package_json as nullable TEXT on dora_submission_runs."""
    op.execute(
        "ALTER TABLE dora_submission_runs "
        "ADD COLUMN frozen_package_json TEXT NULL"
    )


def downgrade() -> None:
    """Drop frozen_package_json, returning runs to counts-only storage."""
    op.execute(
        "ALTER TABLE dora_submission_runs DROP COLUMN frozen_package_json"
    )
