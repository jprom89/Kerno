"""Integration tests for migration 013_dora_hardening_constraints.py.

What:  Verifies that migration 013 correctly applied all schema changes to a live
       PostgreSQL database: TIMESTAMPTZ column conversions, the FK constraint on
       dora_submission_runs.submission_window_id, the UNIQUE constraint on
       (submission_window_id, tenant_id), and the four CHECK constraints on
       status and enum-like columns.

Why:   Schema inspection tests catch silent migration failures where the Alembic
       script runs without error but the constraint is silently dropped or never
       created (e.g. due to a name collision or pre-existing constraint). These
       tests query information_schema directly and assert the exact constraint
       name and type, making regression unambiguous.

How to run or test:
    pytest tests/integration/test_013_migration.py -m integration -v
    (Requires DATABASE_URL environment variable pointing to a migrated DB.)
"""

import pytest

_SCHEMA = "public"

_COLUMN_TYPE_SQL = """
SELECT data_type
FROM information_schema.columns
WHERE table_schema = :schema
  AND table_name = :table_name
  AND column_name = :column_name
"""

_CONSTRAINT_SQL = """
SELECT constraint_name
FROM information_schema.table_constraints
WHERE table_schema = :schema
  AND table_name = :table_name
  AND constraint_name = :constraint_name
  AND constraint_type = :constraint_type
"""


def _assert_column_is_timestamptz(conn, table: str, column: str) -> None:
    """Assert that a column is TIMESTAMP WITH TIME ZONE in the live database."""
    row = conn.execute(
        _COLUMN_TYPE_SQL,
        {"schema": _SCHEMA, "table_name": table, "column_name": column},
    ).fetchone()
    assert row is not None, f"Column {table}.{column} not found in information_schema"
    assert row[0] == "timestamp with time zone", (
        f"{table}.{column} expected TIMESTAMPTZ but got {row[0]!r}"
    )


def _assert_constraint_exists(
    conn, table: str, constraint_name: str, constraint_type: str
) -> None:
    """Assert that a named constraint of the given type exists on the table."""
    row = conn.execute(
        _CONSTRAINT_SQL,
        {
            "schema": _SCHEMA,
            "table_name": table,
            "constraint_name": constraint_name,
            "constraint_type": constraint_type,
        },
    ).fetchone()
    assert row is not None, (
        f"{constraint_type} constraint {constraint_name!r} not found on {table}"
    )


# ---------------------------------------------------------------------------
# Item 5 — TIMESTAMPTZ column checks
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_submission_windows_timestamps_are_timezone_aware(db_connection) -> None:
    """created_at and updated_at on dora_submission_windows must be TIMESTAMPTZ after migration 013.

    Migration 013 converted these columns from TIMESTAMP (timezone-naive) to
    TIMESTAMP WITH TIME ZONE to align with all other Kerno audit columns.
    """
    for col in ("created_at", "updated_at"):
        _assert_column_is_timestamptz(db_connection, "dora_submission_windows", col)


@pytest.mark.integration
def test_submission_runs_timestamps_are_timezone_aware(db_connection) -> None:
    """created_at, updated_at, and submitted_at on dora_submission_runs must be TIMESTAMPTZ.

    Migration 013 converted these three columns from TIMESTAMP (timezone-naive) to
    TIMESTAMP WITH TIME ZONE to match the dora_register_entries convention.
    """
    for col in ("created_at", "updated_at", "submitted_at"):
        _assert_column_is_timestamptz(db_connection, "dora_submission_runs", col)


# ---------------------------------------------------------------------------
# Item 6 — Foreign Key
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_fk_submission_runs_window_id_exists(db_connection) -> None:
    """FK constraint fk_submission_runs_window_id must exist on dora_submission_runs.

    This FK enforces that every submission run references a valid submission window
    with ON DELETE RESTRICT, preventing orphaned runs.
    """
    _assert_constraint_exists(
        db_connection,
        "dora_submission_runs",
        "fk_submission_runs_window_id",
        "FOREIGN KEY",
    )


# ---------------------------------------------------------------------------
# Item 9 (migration part) — Unique constraint
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_unique_constraint_submission_runs_window_tenant_exists(db_connection) -> None:
    """UNIQUE constraint uq_submission_runs_window_tenant must exist on dora_submission_runs.

    This constraint prevents concurrent duplicate INSERTs for the same
    (submission_window_id, tenant_id) slot, enabling the IntegrityError guard
    in _upsert_submission_run to recover from the SELECT-then-INSERT race.
    """
    _assert_constraint_exists(
        db_connection,
        "dora_submission_runs",
        "uq_submission_runs_window_tenant",
        "UNIQUE",
    )


# ---------------------------------------------------------------------------
# Item 7 — CHECK constraints
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_check_constraint_submission_runs_status_exists(db_connection) -> None:
    """CHECK constraint chk_submission_runs_status must exist on dora_submission_runs."""
    _assert_constraint_exists(
        db_connection,
        "dora_submission_runs",
        "chk_submission_runs_status",
        "CHECK",
    )


@pytest.mark.integration
def test_check_constraint_validation_overall_status_exists(db_connection) -> None:
    """CHECK constraint chk_submission_runs_validation_overall_status must exist."""
    _assert_constraint_exists(
        db_connection,
        "dora_submission_runs",
        "chk_submission_runs_validation_overall_status",
        "CHECK",
    )


@pytest.mark.integration
def test_check_constraint_provider_type_exists(db_connection) -> None:
    """CHECK constraint chk_dora_register_entries_provider_type must exist on dora_register_entries."""
    _assert_constraint_exists(
        db_connection,
        "dora_register_entries",
        "chk_dora_register_entries_provider_type",
        "CHECK",
    )


@pytest.mark.integration
def test_check_constraint_criticality_exists(db_connection) -> None:
    """CHECK constraint chk_dora_register_entries_criticality_level must exist on dora_register_entries.

    Note: the spec named this constraint chk_dora_register_entries_criticality but
    the actual column is criticality_level, so the constraint follows the naming
    convention chk_{table}_{column} and uses the full column name.
    """
    _assert_constraint_exists(
        db_connection,
        "dora_register_entries",
        "chk_dora_register_entries_criticality_level",
        "CHECK",
    )
