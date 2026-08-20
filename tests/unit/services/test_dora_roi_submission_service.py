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

import uuid
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from config.constants import VALIDATION_SEVERITY_FAIL
from src.exceptions import TenantContextMissingError
from src.services.dora_roi_export_service import DORAExportPackage
from src.services.dora_roi_validation_service import ValidationSummary
from src.services.dora_roi_submission_service import (
    FrozenFilingPackage,
    SubmissionRunOutput,
    SubmissionWindowOutput,
    build_and_record_submission,
    create_submission_run,
    get_frozen_filing_package,
    list_open_windows,
    list_tenant_submission_runs,
)

# KER-409: register and submission writes now attribute to a verified JWT
# identity, so every service call has to supply one.
_LEDGER_ACTOR_ID = uuid.UUID("d0000000-0000-4000-d000-000000000004")

_TENANT_ID = "c0000000-0000-4000-a000-000000000066"
_WINDOW_ID = "b0000000-0000-4000-b000-000000000001"
_RUN_ID = "f0000000-0000-4000-f000-000000000001"
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
    """Return a real DORAExportPackage so the freeze path can serialise it."""
    warn_count = issue_count if overall_status == "warn" else 0
    fail_count = issue_count if overall_status == "fail" else 0
    return DORAExportPackage(
        tenant_id=_TENANT_ID,
        generated_at=datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        reporting_year=2025,
        entry_count=entry_count,
        rows=[],
        validation_summary=ValidationSummary(
            overall_status=overall_status,
            issue_count=issue_count,
            pass_count=0,
            warn_count=warn_count,
            fail_count=fail_count,
            issues=[],
        ),
    )


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
        run, package = build_and_record_submission(spy, _TENANT_ID, _WINDOW_ID, actor_id=_LEDGER_ACTOR_ID, actor_role="vciso")
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
        run, package = build_and_record_submission(spy, _TENANT_ID, _WINDOW_ID, actor_id=_LEDGER_ACTOR_ID, actor_role="vciso")
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
        run, _ = build_and_record_submission(spy, _TENANT_ID, _WINDOW_ID, actor_id=_LEDGER_ACTOR_ID, actor_role="vciso")
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
        run, _ = build_and_record_submission(spy, _TENANT_ID, _WINDOW_ID, actor_id=_LEDGER_ACTOR_ID, actor_role="vciso")
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


def test_draft_insert_stores_null_frozen_package() -> None:
    """create_submission_run writes NULL for frozen_package_json — there is no package yet."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[("FROM dora_submission_windows", _SelectResult([window_row]))])
    create_submission_run(spy, _TENANT_ID, _WINDOW_ID)
    insert_calls = [
        (sql, params) for sql, params in spy.calls
        if "INSERT INTO dora_submission_runs" in str(sql)
    ]
    _, params = insert_calls[0]
    assert params["frozen_package_json"] is None


def test_build_and_record_stores_serialised_package() -> None:
    """INSERT params include canonical JSON of the package, not a live rebuild token."""
    window_row = _make_window_row()
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_submission_runs", _SelectResult([])),
    ])
    with patch(_PATCH_TARGET, return_value=_make_package("pass", 0, 4)):
        build_and_record_submission(
            spy, _TENANT_ID, _WINDOW_ID, actor_id=_LEDGER_ACTOR_ID, actor_role="vciso"
        )
    insert_calls = [
        (sql, params) for sql, params in spy.calls
        if "INSERT INTO dora_submission_runs" in str(sql)
    ]
    _, params = insert_calls[0]
    frozen = params["frozen_package_json"]
    assert frozen is not None
    assert '"entry_count":4' in frozen
    assert '"reporting_year":2025' in frozen


def test_update_replaces_frozen_package() -> None:
    """A second Start-run overwrites frozen_package_json, matching the counts on the page."""
    window_row = _make_window_row()
    existing_row = _make_run_row(status="draft")
    spy = _SpyConn(responses=[
        ("FROM dora_submission_windows", _SelectResult([window_row])),
        ("FROM dora_submission_runs", _SelectResult([existing_row])),
    ])
    with patch(_PATCH_TARGET, return_value=_make_package("pass", 0, 7)):
        build_and_record_submission(
            spy, _TENANT_ID, _WINDOW_ID, actor_id=_LEDGER_ACTOR_ID, actor_role="vciso"
        )
    update_calls = [
        (sql, params) for sql, params in spy.calls
        if "UPDATE dora_submission_runs" in str(sql)
    ]
    _, params = update_calls[0]
    assert '"entry_count":7' in params["frozen_package_json"]


def test_list_and_get_run_sql_does_not_select_frozen_package() -> None:
    """Metadata list/GET queries must not pull the filing blob."""
    spy = _SpyConn()
    list_tenant_submission_runs(spy, _TENANT_ID)
    sqls = [str(sql) for sql, _ in spy.calls if "FROM dora_submission_runs" in str(sql)]
    assert sqls
    for sql in sqls:
        assert "frozen_package_json" not in sql


def test_get_frozen_filing_package_returns_stored_text() -> None:
    """The download path returns the TEXT column unchanged."""
    spy = _SpyConn(responses=[
        ("frozen_package_json", _SelectResult([(_RUN_ID, 2025, 3, '{"x":1}')])),
    ])
    filing = get_frozen_filing_package(spy, _TENANT_ID, _RUN_ID)
    assert filing == FrozenFilingPackage(
        run_id=_RUN_ID, reporting_year=2025, entry_count=3, package_json='{"x":1}'
    )


def test_get_frozen_filing_package_none_when_column_null() -> None:
    """A draft with NULL frozen_package_json is indistinguishable from a missing run."""
    spy = _SpyConn(responses=[
        ("frozen_package_json", _SelectResult([(_RUN_ID, 2025, 0, None)])),
    ])
    assert get_frozen_filing_package(spy, _TENANT_ID, _RUN_ID) is None


def test_get_frozen_filing_package_none_when_run_missing() -> None:
    """A well-formed id that names no row returns None."""
    spy = _SpyConn(responses=[("frozen_package_json", _SelectResult([]))])
    assert get_frozen_filing_package(spy, _TENANT_ID, _RUN_ID) is None


def test_falsey_tenant_on_frozen_package_raises() -> None:
    """The download lookup refuses to query without a tenant context."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        get_frozen_filing_package(spy, "", _RUN_ID)
