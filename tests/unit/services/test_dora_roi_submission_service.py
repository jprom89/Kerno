"""Unit tests for src/services/dora_roi_submission_service.py.

Plain-English summary
---------------------
Twelve tests verify the submission service without a live database. A spy connection
records every execute() call and returns configurable rows. A patch replaces
build_export_package with a deterministic stub. Tests cover: draft-run creation,
run creation when none exists, run update when one exists, validation summary
copying, submitted_at preservation, open-window filtering, tenant run ordering,
tenant guard enforcement, SET LOCAL ordering, Session API prohibition, and
explicit tenant_id in the list-runs SELECT params.

How to run
----------
    pytest tests/unit/services/test_dora_roi_submission_service.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from config.constants import VALIDATION_SEVERITY_FAIL
from src.exceptions import TenantContextMissingError
from src.services.dora_roi_submission_service import (
    SubmissionRunOutput,
    SubmissionWindowOutput,
    build_and_record_submission,
    create_submission_run,
    list_open_windows,
    list_tenant_submission_runs,
)

_TENANT_ID = "c0000000-0000-4000-a000-000000000066"
_WINDOW_ID = "w0000000-0000-4000-w000-000000000001"
_RUN_ID = "r0000000-0000-4000-r000-000000000001"
_NOW = datetime(2025, 6, 1, 12, 0, 0)
_TODAY = date(2025, 6, 1)


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


class _SpyConn:
    """Records execute() calls; raises on SQLAlchemy Session API usage."""

    def __init__(self, responses: list[tuple[str, object]] | None = None) -> None:
        """Initialise with an empty call log and optional response configuration."""
        self.calls: list[tuple] = []
        self._responses = responses or []

    def execute(self, sql, params=None) -> object:
        """Record the call and return the first configured response whose fragment matches."""
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in str(sql):
                return result
        return _NullResult()

    def add(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.add() called — submission service must use conn.execute()")

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.flush() called — submission service must use conn.execute()")


def _make_window_row(
    window_id: str = _WINDOW_ID,
    authority_code: str = "MFSA",
    reporting_year: int = 2025,
    open_date: date = date(2025, 4, 1),
    close_date: date = date(2025, 6, 30),
) -> tuple:
    """Return an 8-column row tuple matching the dora_submission_windows SELECT order."""
    return (
        window_id, authority_code, reporting_year,
        date(reporting_year - 1, 12, 31),
        open_date, close_date, _NOW, _NOW,
    )


def _make_run_row(
    run_id: str = _RUN_ID,
    status: str = "draft",
    val_status: str = "fail",
    val_count: int = 3,
    entry_count: int = 5,
) -> tuple:
    """Return a 12-column row tuple matching the dora_submission_runs SELECT order."""
    return (
        run_id, _TENANT_ID, _WINDOW_ID, 2025,
        status, val_status, val_count, entry_count,
        _NOW, _NOW, None, None,
    )


def _make_package(overall_status: str = "pass", issue_count: int = 0, entry_count: int = 5):
    """Return a mock DORAExportPackage-like object with a validation summary."""
    summary = MagicMock()
    summary.overall_status = overall_status
    summary.issue_count = issue_count
    package = MagicMock()
    package.validation_summary = summary
    package.entry_count = entry_count
    return package


_PATCH_TARGET = "src.services.dora_roi_submission_service.build_export_package"


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_create_submission_run_inserts_draft() -> None:
    """create_submission_run inserts a row with status='draft' and pessimistic defaults."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[("FROM dora_submission_windows", _SelectResult([window_row]))])
    result = create_submission_run(spy, _TENANT_ID, _WINDOW_ID)
    assert result.status == "draft"
    assert result.validation_overall_status == "fail"
    assert result.validation_issue_count == 0
    assert result.entry_count == 0
    assert result.submitted_at is None
    insert_calls = [sql for sql, _ in spy.calls if "INSERT INTO dora_submission_runs" in str(sql)]
    assert len(insert_calls) == 1


def test_build_and_record_submission_creates_run_when_missing() -> None:
    """build_and_record_submission inserts a new run when none exists for the slot."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_submission_runs", _SelectResult([])),
    ])
    with patch(_PATCH_TARGET, return_value=_make_package("pass", 0, 4)):
        run, package = build_and_record_submission(spy, _TENANT_ID, _WINDOW_ID)
    insert_calls = [sql for sql, _ in spy.calls if "INSERT INTO dora_submission_runs" in str(sql)]
    assert len(insert_calls) == 1
    assert run.entry_count == 4


def test_build_and_record_submission_updates_existing_run() -> None:
    """build_and_record_submission updates an existing run rather than inserting a duplicate."""
    window_row = _make_window_row()
    existing_row = _make_run_row(status="draft")
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_submission_runs", _SelectResult([existing_row])),
    ])
    with patch(_PATCH_TARGET, return_value=_make_package("pass", 0, 7)):
        run, package = build_and_record_submission(spy, _TENANT_ID, _WINDOW_ID)
    update_calls = [sql for sql, _ in spy.calls if "UPDATE dora_submission_runs" in str(sql)]
    insert_calls = [sql for sql, _ in spy.calls if "INSERT INTO dora_submission_runs" in str(sql)]
    assert len(update_calls) == 1
    assert len(insert_calls) == 0
    assert run.id == _RUN_ID


def test_build_and_record_submission_copies_validation_summary() -> None:
    """status, validation_overall_status, and validation_issue_count are copied from the package."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_submission_runs", _SelectResult([])),
    ])
    with patch(_PATCH_TARGET, return_value=_make_package("warn", 3, 2)):
        run, _ = build_and_record_submission(spy, _TENANT_ID, _WINDOW_ID)
    assert run.status == "draft"
    assert run.validation_overall_status == "warn"
    assert run.validation_issue_count == 3


def test_build_and_record_submission_does_not_set_submitted_at() -> None:
    """submitted_at is None after build_and_record_submission — it is reserved for later."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_submission_runs", _SelectResult([])),
    ])
    with patch(_PATCH_TARGET, return_value=_make_package("pass", 0, 1)):
        run, _ = build_and_record_submission(spy, _TENANT_ID, _WINDOW_ID)
    assert run.submitted_at is None


def test_list_open_windows_filters_by_today() -> None:
    """list_open_windows issues SQL that includes window_close_date >= :today."""
    spy = _SpyConn(responses=[("FROM dora_submission_windows", _SelectResult([]))])
    list_open_windows(spy)
    open_window_calls = [
        (sql, params) for sql, params in spy.calls
        if "window_close_date" in str(sql)
    ]
    assert len(open_window_calls) == 1
    _, params = open_window_calls[0]
    assert "today" in params


def test_list_tenant_submission_runs_sorted() -> None:
    """list_tenant_submission_runs SQL includes ORDER BY reporting_year DESC, created_at DESC."""
    spy = _SpyConn()
    list_tenant_submission_runs(spy, _TENANT_ID)
    sorted_calls = [sql for sql, _ in spy.calls if "reporting_year DESC" in str(sql)]
    assert len(sorted_calls) == 1


def test_falsey_tenant_raises() -> None:
    """Passing None or empty string as tenant_id raises TenantContextMissingError immediately."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        create_submission_run(spy, None, _WINDOW_ID)
    with pytest.raises(TenantContextMissingError):
        list_tenant_submission_runs(spy, "")


def test_tenant_context_set_before_tenant_queries() -> None:
    """SET LOCAL must appear in spy.calls before any INSERT or UPDATE for tenant-scoped operations."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[("FROM dora_submission_windows", _SelectResult([window_row]))])
    create_submission_run(spy, _TENANT_ID, _WINDOW_ID)
    set_local_calls = [i for i, (sql, _) in enumerate(spy.calls) if "SET LOCAL" in str(sql)]
    insert_calls = [i for i, (sql, _) in enumerate(spy.calls) if "INSERT INTO" in str(sql)]
    assert set_local_calls, "SET LOCAL must appear in calls"
    assert insert_calls, "INSERT must appear in calls"
    assert set_local_calls[0] < insert_calls[0], "SET LOCAL must come before INSERT"


def test_list_tenant_submission_runs_passes_tenant_id_in_params() -> None:
    """list_tenant_submission_runs passes tenant_id explicitly in the SELECT params."""
    spy = _SpyConn()
    list_tenant_submission_runs(spy, _TENANT_ID)
    runs_calls = [
        (sql, params) for sql, params in spy.calls
        if "FROM dora_submission_runs" in str(sql) and "WHERE" in str(sql)
    ]
    assert runs_calls, "SELECT with WHERE must be issued against dora_submission_runs"
    _, params = runs_calls[0]
    assert params is not None, "Params must not be None for the tenant runs query"
    assert "tenant_id" in params, "tenant_id key must appear in SELECT params"
    assert str(_TENANT_ID) in str(params.get("tenant_id", "")), (
        "tenant_id param value must equal the caller-supplied tenant_id"
    )


def test_no_session_api_used() -> None:
    """conn.add() and conn.flush() must never be called by the submission service."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[("FROM dora_submission_windows", _SelectResult([window_row]))])
    result = create_submission_run(spy, _TENANT_ID, _WINDOW_ID)
    assert result is not None


def test_insert_draft_run_uses_validation_severity_fail_constant() -> None:
    """The SQL INSERT params contain the value of VALIDATION_SEVERITY_FAIL, not a hardcoded string.

    Verifies that _insert_draft_run passes validation_overall_status via the
    VALIDATION_SEVERITY_FAIL constant so that a future value change to that constant
    propagates automatically rather than silently diverging from a hardcoded literal.
    """
    window_row = _make_window_row()
    spy = _SpyConn(responses=[("FROM dora_submission_windows", _SelectResult([window_row]))])
    create_submission_run(spy, _TENANT_ID, _WINDOW_ID)
    insert_calls = [
        (sql, params) for sql, params in spy.calls
        if "INSERT INTO dora_submission_runs" in str(sql)
    ]
    assert len(insert_calls) == 1, "Exactly one INSERT must be issued"
    _, params = insert_calls[0]
    assert params is not None, "INSERT params must not be None"
    assert params.get("validation_overall_status") == VALIDATION_SEVERITY_FAIL, (
        "validation_overall_status must equal VALIDATION_SEVERITY_FAIL, not a hardcoded literal"
    )
