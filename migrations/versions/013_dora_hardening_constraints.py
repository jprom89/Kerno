"""Add FK, unique, timezone, and CHECK constraints to DORA RoI tables (Doc 17B items 5–7, 9).

What:  Hardens the DORA RoI schema in four ways:
       1. Converts TIMESTAMP → TIMESTAMPTZ on all audit and nullable timestamp columns
          in dora_submission_windows and dora_submission_runs (item 5).
       2. Adds a FOREIGN KEY from dora_submission_runs.submission_window_id to
          dora_submission_windows.id with ON DELETE RESTRICT (item 6).
       3. Adds a UNIQUE constraint on (submission_window_id, tenant_id) in
          dora_submission_runs to guard against concurrent duplicate inserts (item 9).
       4. Adds CHECK constraints on status, validation_overall_status,
          provider_type, and criticality_level to enforce allowed values at the
          database layer (item 7).

Why:   The Doc 17 hardening review found that timestamp columns created in
       migration 012 are timezone-naive (TIMESTAMP), inconsistent with
       dora_register_entries (TIMESTAMPTZ). Mixed timezone awareness causes silent
       comparison bugs. The FK was omitted from migration 012 and must be added
       now. The CHECK constraints close a data-integrity gap where invalid status
       strings could be written via raw SQL. The UNIQUE constraint enables safe
       recovery from the SELECT-then-INSERT race condition in _upsert_submission_run.

How to run or test:
    alembic upgrade m8n9o0p1

Roll back (drops all new constraints and reverts column types):
    alembic downgrade l7m8n9o0

Integration tests:
    pytest tests/integration/test_013_migration.py -m integration -v
"""

import sqlalchemy as sa
from alembic import op

revision = "m8n9o0p1"
down_revision = "l7m8n9o0"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Allowed value sets — derived from model and constant definitions.
# Values must stay in sync with:
#   src/models/dora_submission_run.py  (SUBMISSION_STATUS_*)
#   src/models/dora_register_entry.py  (PROVIDER_TYPE_*, CRITICALITY_*)
#   config/constants.py                (VALIDATION_SEVERITY_*)
# ---------------------------------------------------------------------------

_SUBMISSION_RUN_STATUSES = ("draft", "ready", "submitted", "failed")
_VALIDATION_OVERALL_STATUSES = ("pass", "warn", "fail")
_PROVIDER_TYPES = ("cloud", "software", "managed_service", "telecom", "other")
_CRITICALITY_LEVELS = ("critical", "high", "standard")


def upgrade() -> None:
    """Apply items 5–7 and 9 migration changes in dependency order.

    Order: timestamp alterations first (no dependencies), then FK (needs both
    tables), then unique constraint (on the same table as FK), then CHECK
    constraints (independent of FKs and UQs).
    """
    _alter_timestamps_to_timestamptz()
    _create_fk_submission_runs_window()
    _create_uq_submission_runs_window_tenant()
    _create_check_constraints()


def downgrade() -> None:
    """Reverse all changes from upgrade() in strict reverse order.

    CHECK constraints first (no dependents), then unique, then FK, then columns.
    """
    _drop_check_constraints()
    _drop_uq_submission_runs_window_tenant()
    _drop_fk_submission_runs_window()
    _revert_timestamps_to_timestamp()


# ---------------------------------------------------------------------------
# Item 5 — TIMESTAMP → TIMESTAMPTZ
# ---------------------------------------------------------------------------


def _alter_timestamps_to_timestamptz() -> None:
    """Convert all timezone-naive TIMESTAMP columns in Doc 16 tables to TIMESTAMPTZ.

    Affected columns are the audit timestamps in dora_submission_windows
    (created_at, updated_at) and dora_submission_runs (created_at, updated_at,
    submitted_at). Existing values are interpreted as UTC via AT TIME ZONE 'UTC'.
    Window open/close columns are DATE type and are not affected.
    """
    _tz_naive = sa.DateTime(timezone=False)
    _tz_aware = sa.DateTime(timezone=True)
    for col in ("created_at", "updated_at"):
        op.alter_column(
            "dora_submission_windows", col,
            existing_type=_tz_naive,
            type_=_tz_aware,
            postgresql_using=f"{col} AT TIME ZONE 'UTC'",
            existing_nullable=False,
        )
    for col in ("created_at", "updated_at"):
        op.alter_column(
            "dora_submission_runs", col,
            existing_type=_tz_naive,
            type_=_tz_aware,
            postgresql_using=f"{col} AT TIME ZONE 'UTC'",
            existing_nullable=False,
        )
    op.alter_column(
        "dora_submission_runs", "submitted_at",
        existing_type=_tz_naive,
        type_=_tz_aware,
        postgresql_using="submitted_at AT TIME ZONE 'UTC'",
        existing_nullable=True,
    )


def _revert_timestamps_to_timestamp() -> None:
    """Revert TIMESTAMPTZ columns in Doc 16 tables back to TIMESTAMP WITHOUT TIME ZONE.

    Existing values are cast back via AT TIME ZONE 'UTC'; no data is lost if the
    database was always operating in UTC (the only supported configuration).
    """
    _tz_naive = sa.DateTime(timezone=False)
    _tz_aware = sa.DateTime(timezone=True)
    op.alter_column(
        "dora_submission_runs", "submitted_at",
        existing_type=_tz_aware,
        type_=_tz_naive,
        postgresql_using="submitted_at AT TIME ZONE 'UTC'",
        existing_nullable=True,
    )
    for col in ("created_at", "updated_at"):
        op.alter_column(
            "dora_submission_runs", col,
            existing_type=_tz_aware,
            type_=_tz_naive,
            postgresql_using=f"{col} AT TIME ZONE 'UTC'",
            existing_nullable=False,
        )
    for col in ("created_at", "updated_at"):
        op.alter_column(
            "dora_submission_windows", col,
            existing_type=_tz_aware,
            type_=_tz_naive,
            postgresql_using=f"{col} AT TIME ZONE 'UTC'",
            existing_nullable=False,
        )


# ---------------------------------------------------------------------------
# Item 6 — Foreign Key
# ---------------------------------------------------------------------------


def _create_fk_submission_runs_window() -> None:
    """Add FK constraint from dora_submission_runs.submission_window_id to dora_submission_windows.id.

    ON DELETE RESTRICT prevents a submission window from being deleted while any
    run still references it. This enforces referential integrity that the original
    migration 012 omitted.
    """
    op.create_foreign_key(
        "fk_submission_runs_window_id",
        "dora_submission_runs",
        "dora_submission_windows",
        ["submission_window_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _drop_fk_submission_runs_window() -> None:
    """Drop the FK constraint added by _create_fk_submission_runs_window."""
    op.drop_constraint(
        "fk_submission_runs_window_id",
        "dora_submission_runs",
        type_="foreignkey",
    )


# ---------------------------------------------------------------------------
# Item 9 (migration part) — Unique constraint to guard the upsert race
# ---------------------------------------------------------------------------


def _create_uq_submission_runs_window_tenant() -> None:
    """Add a UNIQUE constraint on (submission_window_id, tenant_id) in dora_submission_runs.

    This makes concurrent duplicate INSERTs fail loudly with an IntegrityError
    rather than silently creating two rows for the same (window, tenant) slot.
    The service layer catches the IntegrityError and recovers gracefully.
    """
    op.create_unique_constraint(
        "uq_submission_runs_window_tenant",
        "dora_submission_runs",
        ["submission_window_id", "tenant_id"],
    )


def _drop_uq_submission_runs_window_tenant() -> None:
    """Drop the unique constraint added by _create_uq_submission_runs_window_tenant."""
    op.drop_constraint(
        "uq_submission_runs_window_tenant",
        "dora_submission_runs",
        type_="unique",
    )


# ---------------------------------------------------------------------------
# Item 7 — CHECK constraints
# ---------------------------------------------------------------------------


def _create_check_constraints() -> None:
    """Add CHECK constraints that enforce allowed values on four enum-like columns.

    All allowed value sets are derived from model and config constants; see the
    module-level tuples for the authoritative source references.
    """
    statuses = ", ".join(f"'{s}'" for s in _SUBMISSION_RUN_STATUSES)
    val_statuses = ", ".join(f"'{s}'" for s in _VALIDATION_OVERALL_STATUSES)
    provider_types = ", ".join(f"'{p}'" for p in _PROVIDER_TYPES)
    criticalities = ", ".join(f"'{c}'" for c in _CRITICALITY_LEVELS)
    op.execute(
        f"ALTER TABLE dora_submission_runs "
        f"ADD CONSTRAINT chk_submission_runs_status "
        f"CHECK (status IN ({statuses}))"
    )
    op.execute(
        f"ALTER TABLE dora_submission_runs "
        f"ADD CONSTRAINT chk_submission_runs_validation_overall_status "
        f"CHECK (validation_overall_status IN ({val_statuses}))"
    )
    op.execute(
        f"ALTER TABLE dora_register_entries "
        f"ADD CONSTRAINT chk_dora_register_entries_provider_type "
        f"CHECK (provider_type IN ({provider_types}))"
    )
    op.execute(
        f"ALTER TABLE dora_register_entries "
        f"ADD CONSTRAINT chk_dora_register_entries_criticality_level "
        f"CHECK (criticality_level IN ({criticalities}))"
    )


def _drop_check_constraints() -> None:
    """Drop all CHECK constraints added by _create_check_constraints."""
    op.execute(
        "ALTER TABLE dora_register_entries "
        "DROP CONSTRAINT IF EXISTS chk_dora_register_entries_criticality_level"
    )
    op.execute(
        "ALTER TABLE dora_register_entries "
        "DROP CONSTRAINT IF EXISTS chk_dora_register_entries_provider_type"
    )
    op.execute(
        "ALTER TABLE dora_submission_runs "
        "DROP CONSTRAINT IF EXISTS chk_submission_runs_validation_overall_status"
    )
    op.execute(
        "ALTER TABLE dora_submission_runs "
        "DROP CONSTRAINT IF EXISTS chk_submission_runs_status"
    )
