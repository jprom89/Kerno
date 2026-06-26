"""Create the DORA Register of Information tables for KER-106 (Document 14).

What:  Creates two tables that underpin the live DORA Register of Information:

  dora_register_entries — one row per ICT third-party/service relationship per
    tenant. Tenant-scoped with Row-Level Security; each tenant can only see
    their own entries.

  dora_reporting_windows — global reference data for competent authority
    submission windows. No tenant_id column, no RLS. Readable by any session
    without tenant context.

Why:   DORA Article 28 requires financial entities to maintain a continuously-
       updated Register of Information for ICT third-party service providers.
       Document 14 (KER-106 part 1) establishes this as a live register — not
       an annual export artifact — so entries can be queried, filtered, and
       later exported into ESA xBRL-CSV format (Document 15).

How to run or test:
    alembic upgrade k6l7m8n9

Roll back (drops both tables):
    alembic downgrade j5k6l7m8

Service-layer unit tests live in:
    tests/unit/services/test_dora_roi_service.py
"""

from alembic import op

revision = "k6l7m8n9"
down_revision = "j5k6l7m8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create dora_register_entries and dora_reporting_windows with indexes and RLS.

    Order: create tables first, then indexes, then RLS on dora_register_entries.
    dora_reporting_windows intentionally receives no RLS policy.
    """
    _create_register_entries_table()
    _create_reporting_windows_table()
    _create_register_entries_indexes()
    _enable_rls_on_entries()
    _create_entries_tenant_policy()


def downgrade() -> None:
    """Drop both tables and all associated objects.

    Dropping the tables implicitly drops their indexes and RLS policies.
    dora_register_entries is dropped first because it logically depends on tenants.
    """
    op.execute("DROP TABLE IF EXISTS dora_register_entries")
    op.execute("DROP TABLE IF EXISTS dora_reporting_windows")


def _create_register_entries_table() -> None:
    """Create dora_register_entries with all fields from §3.1.

    data_types and countries_supported are TEXT[] arrays. updated_at has a
    server default of now(); the service layer always sets it explicitly on
    UPDATE so a trigger is not required.
    """
    op.execute(
        """
        CREATE TABLE dora_register_entries (
            register_entry_id  UUID         NOT NULL,
            tenant_id          UUID         NOT NULL,
            provider_name      TEXT         NOT NULL,
            service_name       TEXT         NOT NULL,
            provider_type      VARCHAR(32)  NOT NULL,
            criticality_level  VARCHAR(16)  NOT NULL,
            business_function  TEXT         NOT NULL,
            data_types         TEXT[]       NOT NULL,
            countries_supported TEXT[]      NOT NULL,
            contract_start_date DATE,
            contract_end_date   DATE,
            exit_strategy_summary TEXT,
            is_active          BOOLEAN      NOT NULL DEFAULT TRUE,
            source_record_id   TEXT,
            created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (register_entry_id)
        )
        """
    )


def _create_reporting_windows_table() -> None:
    """Create dora_reporting_windows as global reference data without tenant_id.

    No RLS is enabled on this table. Any authenticated session may read all rows.
    Rows are managed by platform administrators, not by tenant actions.
    """
    op.execute(
        """
        CREATE TABLE dora_reporting_windows (
            reporting_window_id  UUID        NOT NULL,
            authority_code       VARCHAR(32) NOT NULL,
            authority_name       TEXT        NOT NULL,
            member_state         TEXT        NOT NULL,
            reporting_year       INTEGER     NOT NULL,
            submission_open_date DATE        NOT NULL,
            submission_close_date DATE       NOT NULL,
            notes                TEXT,
            created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (reporting_window_id)
        )
        """
    )


def _create_register_entries_indexes() -> None:
    """Create two composite indexes on dora_register_entries for fast lookups.

    The first index covers (tenant_id, criticality_level, is_active) to
    support filtered list queries. The second covers (tenant_id, updated_at)
    to support the default ordering (updated_at DESC) efficiently.
    """
    op.execute(
        """
        CREATE INDEX idx_dora_entries_criticality
        ON dora_register_entries (tenant_id, criticality_level, is_active)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_dora_entries_updated
        ON dora_register_entries (tenant_id, updated_at)
        """
    )


def _enable_rls_on_entries() -> None:
    """Enable Row-Level Security on dora_register_entries.

    Must be called before creating any policy; without ENABLE ROW LEVEL
    SECURITY the CREATE POLICY statement would succeed but RLS would not
    be enforced for table owners.
    """
    op.execute(
        "ALTER TABLE dora_register_entries ENABLE ROW LEVEL SECURITY"
    )


def _create_entries_tenant_policy() -> None:
    """Create the tenant isolation policy using the direct tenant_id column.

    dora_register_entries has a direct tenant_id column so the policy reads it
    directly — the same pattern as context_records and recommendations (migrations
    007 and 010). The current_setting call reads the session variable set by
    set_tenant_context() in src/db/rls.py.
    """
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON dora_register_entries
          USING (
            tenant_id = current_setting('app.current_tenant_id', true)::uuid
          )
        """
    )
