"""Unit tests for src/services/dora_roi_scheduler_service.py.

Plain-English summary
---------------------
Two tests verify the scheduler service without a live database.
Test 1 proves that _SELECT_OPEN_WINDOWS is the same object imported from
dora_roi_submission_service, not a local copy (Doc 17B item 8 deduplication).
Test 2 proves that _upsert_submission_run in the submission service recovers
silently from a concurrent INSERT race, issuing exactly one INSERT attempt and
falling through to UPDATE (Doc 17B item 9 guard).

How to run
----------
    pytest tests/unit/services/test_dora_roi_scheduler_service.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.services.dora_roi_submission_service import _upsert_submission_run

_TENANT_ID = "c0000000-0000-4000-a000-000000000066"
_WINDOW_ID = "w0000000-0000-4000-w000-000000000001"
_RUN_ID = "r0000000-0000-4000-r000-000000000001"
_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── Infrastructure ─────────────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT result — fetchone/fetchall return empty."""

    def fetchone(self):
        """Return None."""
        return None

    def fetchall(self) -> list:
        """Return an empty list."""
        return []


class _SelectResult:
    """Simulates a SELECT result returning a fixed list of row tuples."""

    def __init__(self, rows: list) -> None:
        """Store rows to return from fetchall and fetchone."""
        self._rows = rows

    def fetchone(self):
        """Return the first row, or None."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        """Return all rows."""
        return self._rows


def _make_run_row() -> tuple:
    """Return a 12-column row tuple matching the dora_submission_runs SELECT order."""
    return (
        _RUN_ID, _TENANT_ID, _WINDOW_ID, 2025,
        "draft", "fail", 0, 0,
        _NOW, _NOW, None, None,
    )


def _make_package(overall_status: str = "pass", issue_count: int = 0, entry_count: int = 2):
    """Return a mock DORAExportPackage with a validation summary."""
    summary = MagicMock()
    summary.overall_status = overall_status
    summary.issue_count = issue_count
    package = MagicMock()
    package.validation_summary = summary
    package.entry_count = entry_count
    return package


class _RaceConn:
    """Spy connection that simulates the INSERT race: INSERT raises IntegrityError.

    On the first SELECT for an existing run, returns nothing (no run exists yet).
    On INSERT, raises IntegrityError (another process won the race).
    On the second SELECT for an existing run (re-fetch), returns the pre-built row.
    On UPDATE, records the call and returns a null result.
    """

    def __init__(self, existing_row: tuple) -> None:
        """Store the row to return after race recovery and initialise call counters."""
        self._existing_row = existing_row
        self._existing_run_selects = 0
        self.insert_calls: list = []
        self.update_calls: list = []

    def execute(self, sql, params=None):
        """Route SQL to the appropriate simulated response."""
        sql_str = str(sql)
        if "INSERT INTO dora_submission_runs" in sql_str:
            self.insert_calls.append((sql, params))
            raise IntegrityError(
                "unique constraint violated", None, Exception("duplicate key")
            )
        if (
            "FROM dora_submission_runs" in sql_str
            and "ORDER BY created_at DESC" in sql_str
        ):
            self._existing_run_selects += 1
            if self._existing_run_selects == 1:
                return _NullResult()
            return _SelectResult([self._existing_row])
        if "UPDATE dora_submission_runs" in sql_str:
            self.update_calls.append((sql, params))
        return _NullResult()


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_select_open_windows_is_shared_with_submission_service() -> None:
    """_SELECT_OPEN_WINDOWS in dora_roi_scheduler_service is the same object as in the submission service.

    Proves that no local copy of the SQL exists in the scheduler service —
    it is the identical constant imported from the authoritative source
    (Doc 17B item 8 deduplication).
    """
    from src.services.dora_roi_scheduler_service import (
        _SELECT_OPEN_WINDOWS as scheduler_sql,
    )
    from src.services.dora_roi_submission_service import (
        _SELECT_OPEN_WINDOWS as submission_sql,
    )
    assert scheduler_sql is submission_sql, (
        "_SELECT_OPEN_WINDOWS must be the same object in both modules — "
        "the scheduler must import from submission service, not define its own copy"
    )


def test_upsert_run_duplicate_insert_is_silently_ignored() -> None:
    """_upsert_submission_run silently recovers from a concurrent INSERT race.

    Simulates the scenario where the initial SELECT finds no existing run, the
    INSERT raises IntegrityError (another process won the race), the re-fetch
    SELECT finds the run the winner created, and the function falls through to
    UPDATE. No exception must be raised and exactly one INSERT must be attempted.
    """
    existing_row = _make_run_row()
    package = _make_package()
    conn = _RaceConn(existing_row)
    result = _upsert_submission_run(conn, _TENANT_ID, _WINDOW_ID, 2025, package)
    assert result is not None, "Must return a valid SubmissionRunOutput after race recovery"
    assert len(conn.insert_calls) == 1, (
        "Exactly one INSERT must be attempted — the race guard must not retry INSERT"
    )
    assert len(conn.update_calls) == 1, (
        "Must fall through to UPDATE after recovering from IntegrityError"
    )
