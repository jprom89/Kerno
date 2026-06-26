"""Unit tests for src/scheduler/dora_roi_submission_scheduler.py.

Plain-English summary
---------------------
Five tests verify the submission scheduler hook without a live database. A spy
connection returns configurable rows for each SQL query. Tests confirm that tenants
without a ready/submitted run are returned, that closed windows produce no results,
that the `today` parameter changes the window-lookup behaviour deterministically,
that a restricted RLS connection raises SchedulerAdminConnectionRequiredError
rather than returning a misleading empty result, and that the error message
mentions the admin connection requirement.

How to run
----------
    pytest tests/unit/scheduler/test_dora_roi_submission_scheduler.py -v
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from src.scheduler.dora_roi_submission_scheduler import (
    SchedulerAdminConnectionRequiredError,
    find_tenants_missing_submission,
)

_WINDOW_ID = "w0000000-0000-4000-w000-000000000001"
_TENANT_A = "a0000000-0000-4000-a000-000000000001"
_TENANT_B = "b0000000-0000-4000-b000-000000000002"
_TODAY = date(2025, 5, 15)
_NOW = datetime(2025, 5, 15, 0, 0, 0)


# ── Infrastructure ─────────────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT result — fetchall returns an empty list."""

    def fetchall(self) -> list:
        """Return an empty list."""
        return []


class _SelectResult:
    """Simulates a SELECT result returning a fixed list of row tuples."""

    def __init__(self, rows: list) -> None:
        """Store rows to return from fetchall."""
        self._rows = rows

    def fetchall(self) -> list:
        """Return all configured rows."""
        return self._rows


class _SpyConn:
    """Records execute() calls and returns configurable responses by SQL fragment."""

    def __init__(self, responses: list[tuple[str, object]] | None = None) -> None:
        """Initialise with an empty call log and optional response configuration."""
        self.calls: list[tuple] = []
        self._responses = responses or []

    def execute(self, sql, params=None) -> object:
        """Record the call and return the first matching configured response."""
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in str(sql):
                return result
        return _NullResult()


def _make_window_row(
    window_id: str = _WINDOW_ID,
    open_date: date = date(2025, 4, 1),
    close_date: date = date(2025, 6, 30),
    reporting_year: int = 2025,
) -> tuple:
    """Return an 8-column window row matching the _SELECT_OPEN_WINDOWS column order."""
    return (
        window_id, "MFSA", reporting_year,
        date(reporting_year - 1, 12, 31),
        open_date, close_date, _NOW, _NOW,
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_find_tenants_missing_submission_returns_expected_pairs() -> None:
    """Tenants with no ready/submitted run for an open window are returned as (tenant_id, window) pairs."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_register_entries", _SelectResult([(_TENANT_A,), (_TENANT_B,)])),
        ("FROM dora_submission_runs", _SelectResult([(_TENANT_B,)])),
    ])
    result = find_tenants_missing_submission(spy, today=_TODAY)
    tenant_ids = [t for t, _ in result]
    assert _TENANT_A in tenant_ids, "Tenant A has no completed run and should be returned"
    assert _TENANT_B not in tenant_ids, "Tenant B has a completed run and must be excluded"
    assert len(result) == 1


def test_find_tenants_missing_submission_ignores_closed_windows() -> None:
    """When no windows are open on today, the function returns an empty list."""
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([])),
    ])
    result = find_tenants_missing_submission(spy, today=_TODAY)
    assert result == [], "No open windows means no missing submissions to report"
    register_calls = [sql for sql, _ in spy.calls if "FROM dora_register_entries" in str(sql)]
    assert len(register_calls) == 0, "Tenant enumeration should be skipped when no windows are open"


def test_find_tenants_missing_submission_respects_today_parameter() -> None:
    """Passing a custom today date changes which windows the query targets."""
    custom_today = date(2025, 3, 1)
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([])),
    ])
    find_tenants_missing_submission(spy, today=custom_today)
    window_calls = [(sql, params) for sql, params in spy.calls if "FROM dora_submission_windows" in str(sql)]
    assert len(window_calls) == 1
    _, params = window_calls[0]
    assert params.get("today") == custom_today, (
        f"Expected today={custom_today!r} in params, got {params!r}"
    )


def test_find_tenants_missing_submission_raises_when_open_window_but_no_visible_tenants() -> None:
    """When open windows exist but tenant enumeration returns zero results, the function raises.

    This is the fail-loud guard: a restricted RLS connection hides all rows from
    dora_register_entries, making the result look like 'no tenants exist'. Returning
    an empty list in that case would be incorrectly interpreted as 'all tenants compliant'.
    The guard raises SchedulerAdminConnectionRequiredError instead.
    """
    window_row = _make_window_row()
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_register_entries", _SelectResult([])),
    ])
    with pytest.raises(SchedulerAdminConnectionRequiredError):
        find_tenants_missing_submission(spy, today=_TODAY)


def test_scheduler_error_message_mentions_admin_connection() -> None:
    """The SchedulerAdminConnectionRequiredError message explicitly mentions the admin connection requirement.

    The error must be actionable: whoever receives it needs to know what to fix.
    Mentioning 'admin' in the message points the caller to the correct resolution
    without requiring them to read the source code or module docstring.
    """
    window_row = _make_window_row()
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_register_entries", _SelectResult([])),
    ])
    with pytest.raises(SchedulerAdminConnectionRequiredError) as exc_info:
        find_tenants_missing_submission(spy, today=_TODAY)
    assert "admin" in str(exc_info.value).lower(), (
        "Error message must mention 'admin' to direct callers to the correct resolution"
    )


def test_scheduler_open_windows_sql_is_shared_with_submission_service() -> None:
    from src.scheduler.dora_roi_submission_scheduler import _SELECT_OPEN_WINDOWS as sched_sql
    from src.services.dora_roi_submission_service import _SELECT_OPEN_WINDOWS as svc_sql
    assert sched_sql is svc_sql
