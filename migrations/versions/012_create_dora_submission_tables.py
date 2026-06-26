"""Create the DORA submission workflow tables for KER-106 (Document 16).

What:  Creates two tables for the DORA RoI submission workflow:

  dora_submission_windows — global reference data listing the submission windows
    defined by competent authorities for each reporting year. No tenant_id column;
    no Row-Level Security. Readable by any authenticated session.

  dora_submission_runs — tenant-scoped records tracking each submission attempt
    for a given (tenant, window, year) slot. Enabled with Row-Level Security
    using the direct tenant_id column predicate (same pattern as migration 011).

Why:   Document 16 (KER-106 part 3) adds submission lifecycle tracking on top of
       the live register (Doc 14) and export/validation (Doc 15). Kerno must persist
       the status of each submission attempt (draft → ready → submitted) so that
       a future authority-portal integration can pick up the latest ready package and
       mark it as submitted without losing the audit trail of prior attempts.

How to run or test:
    alembic upgrade l7m8n9o0

Roll back (drops both tables):
    alembic downgrade k6l7m8n9

Service-layer unit tests live in:
    tests/unit/services/test_dora_roi_submission_service.py
"""

from alembic import op

revision = "l7m8n9o0"
down_revision = "k6l7m8n9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create dora_submission_windows and dora_submission_runs with indexes and RLS.

    Order: create tables, then indexes, then RLS on dora_submission_runs.
    dora_submission_windows intentionally receives no RLS policy.
    """
    _create_submission_windows_table()
    _create_submission_runs_table()
    _create_submission_windows_index()
    _create_submission_runs_index()
    _enable_rls_on_runs()
    _create_runs_tenant_policy()


def downgrade() -> None:
    """Drop both tables and all associated objects.

    Dropping the tables implicitly drops their indexes and RLS policies.
    dora_submission_runs is dropped first because it logically depends on windows.
    """
    op.execute("DROP TABLE IF EXISTS dora_submission_runs")
    op.execute("DROP TABLE IF EXISTS dora_submission_windows")


def _create_submission_windows_table() -> None:
    """Create dora_submission_windows as global reference data without tenant_id.

    The composite unique constraint on (authority_code, reporting_year,
    register_reference_date) prevents duplicate window definitions. No RLS
    is applied; this table is readable by any authenticated session.
    """
    op.execute(
        """
        CREATE TABLE dora_submission_windows (
            id                      UUID         NOT NULL,
            authority_code          VARCHAR(32)  NOT NULL,
            reporting_year          INTEGER      NOT NULL,
            register_reference_date DATE         NOT NULL,
            window_open_date        DATE         NOT NULL,
            window_close_date       DATE         NOT NULL,
            created_at              TIMESTAMP    NOT NULL DEFAULT now(),
            updated_at              TIMESTAMP    NOT NULL DEFAULT now(),
            PRIMARY KEY (id),
            CONSTRAINT uq_submission_window_authority_year_ref
                UNIQUE (authority_code, reporting_year, register_reference_date)
        )
        """
    )


def _create_submission_runs_table() -> None:
    """Create dora_submission_runs as a tenant-scoped table for submission lifecycle tracking.

    submission_window_id references dora_submission_windows.id at the application
    layer; no FOREIGN KEY constraint is defined here consistent with existing
    tenant-scoped tables in this project. submitted_at and submission_reference are
    nullable because they are only populated by a future authority-portal integration.
    """
    op.execute(
        """
        CREATE TABLE dora_submission_runs (
            id                       UUID        NOT NULL,
            tenant_id                UUID        NOT NULL,
            submission_window_id     UUID        NOT NULL,
            reporting_year           INTEGER     NOT NULL,
            status                   VARCHAR(16) NOT NULL,
            validation_overall_status VARCHAR(8) NOT NULL,
            validation_issue_count   INTEGER     NOT NULL,
            entry_count              INTEGER     NOT NULL,
            created_at               TIMESTAMP   NOT NULL DEFAULT now(),
            updated_at               TIMESTAMP   NOT NULL DEFAULT now(),
            submitted_at             TIMESTAMP,
            submission_reference     TEXT,
            PRIMARY KEY (id)
        )
        """
    )


def _create_submission_windows_index() -> None:
    """Create a covering index on dora_submission_windows for window-open date range queries.

    Supports the common query pattern: 'find all windows open on a given date',
    which filters by window_open_date and window_close_date.
    """
    op.execute(
        """
        CREATE INDEX idx_submission_windows_dates
        ON dora_submission_windows (window_open_date, window_close_date)
        """
    )


def _create_submission_runs_index() -> None:
    """Create a composite index on dora_submission_runs for tenant + window lookups.

    Supports the most common access patterns: finding all runs for a tenant
    (tenant_id, reporting_year) and finding the latest run for a specific slot
    (tenant_id, submission_window_id, reporting_year).
    """
    op.execute(
        """
        CREATE INDEX idx_submission_runs_tenant_window
        ON dora_submission_runs (tenant_id, submission_window_id, reporting_year)
        """
    )


def _enable_rls_on_runs() -> None:
    """Enable Row-Level Security on dora_submission_runs.

    Must be called before creating any policy on this table. Without this call
    a CREATE POLICY would succeed but RLS would not be enforced for table owners.
    """
    op.execute(
        "ALTER TABLE dora_submission_runs ENABLE ROW LEVEL SECURITY"
    )


def _create_runs_tenant_policy() -> None:
    """Create the tenant isolation policy on dora_submission_runs.

    Uses the direct tenant_id column (same pattern as dora_register_entries in
    migration 011). The current_setting call reads the session variable set by
    set_tenant_context() in src/db/rls.py before any tenant-scoped query.
    """
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON dora_submission_runs
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )
