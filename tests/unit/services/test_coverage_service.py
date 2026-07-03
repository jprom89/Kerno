"""Unit tests for src/services/coverage_service.py — system-of-record status resolution.

Eleven tests cover the override-wins resolution matrix (approve confirms, edit/reject
invalidate, recommendation is the unconfirmed fallback, nothing resolves to gap),
summary aggregation and its exact reconciliation with the control rows, the category
filter, and tenant isolation. Spy connections only; no database required.
"""

from __future__ import annotations

import uuid

import pytest

from src.exceptions import TenantContextMissingError
from src.services.coverage_service import (
    SOURCE_NONE,
    SOURCE_OVERRIDE,
    SOURCE_RECOMMENDATION,
    get_coverage_controls,
    get_coverage_summary,
    resolve_system_of_record_status,
    summarise_coverage,
)

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")


# ── Test infrastructure ───────────────────────────────────────────────────────


class _RowsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _CoverageSpyConn:
    def __init__(self, rows: list | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._rows = rows or []

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "compliance_controls" in sql:
            return _RowsResult(self._rows)
        return _RowsResult([])

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _row(
    ref: str = "NIS2-1.1",
    category: str = "governance",
    rec_status: str | None = None,
    override_action: str | None = None,
    evidence_count: int = 0,
) -> tuple:
    # Column order mirrors _COVERAGE_QUERY: control_id, control_ref, title,
    # category, framework, status, confidence_level, confidence_score,
    # action_type, evidence_count.
    confidence = ("high", 0.9) if rec_status is not None else (None, None)
    return (
        str(uuid.uuid4()), ref, f"Title for {ref}", category, "nis2",
        rec_status, confidence[0], confidence[1], override_action, evidence_count,
    )


# ── Resolution matrix: the human decision is the system of record ─────────────


def test_approve_override_confirms_recommendation_status() -> None:
    status, source, confirmed = resolve_system_of_record_status("approve", "met")
    assert (status, source, confirmed) == ("met", SOURCE_OVERRIDE, True)


def test_edit_override_invalidates_recommendation_to_gap() -> None:
    status, source, confirmed = resolve_system_of_record_status("edit", "met")
    assert (status, source, confirmed) == ("gap", SOURCE_OVERRIDE, True)


def test_reject_override_invalidates_recommendation_to_gap() -> None:
    status, source, confirmed = resolve_system_of_record_status("reject", "partial")
    assert (status, source, confirmed) == ("gap", SOURCE_OVERRIDE, True)


def test_no_override_falls_back_to_recommendation_status() -> None:
    status, source, confirmed = resolve_system_of_record_status(None, "partial")
    assert (status, source, confirmed) == ("partial", SOURCE_RECOMMENDATION, False)


def test_no_override_and_no_recommendation_is_gap() -> None:
    status, source, confirmed = resolve_system_of_record_status(None, None)
    assert (status, source, confirmed) == ("gap", SOURCE_NONE, False)


def test_approve_without_recommendation_is_conservative_gap() -> None:
    status, source, confirmed = resolve_system_of_record_status("approve", None)
    assert (status, source, confirmed) == ("gap", SOURCE_OVERRIDE, True)


# ── Service queries ───────────────────────────────────────────────────────────


def test_controls_resolution_applied_per_row() -> None:
    spy = _CoverageSpyConn(rows=[
        _row(ref="A-1", rec_status="met", override_action="approve"),
        _row(ref="A-2", rec_status="met", override_action="reject"),
        _row(ref="A-3", rec_status="partial"),
        _row(ref="A-4"),
    ])
    controls = get_coverage_controls(spy, _TENANT_ID)
    assert [c.status for c in controls] == ["met", "gap", "partial", "gap"]
    assert [c.human_confirmed for c in controls] == [True, True, False, False]
    assert controls[0].status_source == SOURCE_OVERRIDE
    assert controls[2].status_source == SOURCE_RECOMMENDATION
    assert controls[3].status_source == SOURCE_NONE


def test_summary_reconciles_exactly_with_control_rows() -> None:
    rows = [
        _row(ref="G-1", category="governance", rec_status="met", override_action="approve"),
        _row(ref="G-2", category="governance", rec_status="gap"),
        _row(ref="R-1", category="risk_management", rec_status="met", override_action="edit"),
        _row(ref="R-2", category="risk_management", rec_status="partial"),
    ]
    summary = get_coverage_summary(_CoverageSpyConn(rows=rows), _TENANT_ID)
    controls = get_coverage_controls(_CoverageSpyConn(rows=rows), _TENANT_ID)
    assert summary.total_controls == len(controls) == 4
    assert summary.met == sum(1 for c in controls if c.status == "met") == 1
    assert summary.partial == sum(1 for c in controls if c.status == "partial") == 1
    assert summary.gap == sum(1 for c in controls if c.status == "gap") == 2
    governance = next(c for c in summary.categories if c.category == "governance")
    assert (governance.met, governance.partial, governance.gap, governance.total) == (1, 0, 1, 2)


def test_category_filter_adds_bound_parameter() -> None:
    spy = _CoverageSpyConn()
    get_coverage_controls(spy, _TENANT_ID, category="governance")
    sql, params = next((s, p) for s, p in spy.calls if "compliance_controls" in s)
    assert "cc.category = :category" in sql
    assert params["category"] == "governance"
    assert params["tenant_id"] == str(_TENANT_ID)


def test_set_tenant_context_fires_before_query() -> None:
    spy = _CoverageSpyConn()
    get_coverage_controls(spy, _TENANT_ID)
    assert "SET LOCAL" in spy.calls[0][0]


def test_none_tenant_raises_before_sql() -> None:
    spy = _CoverageSpyConn()
    with pytest.raises(TenantContextMissingError):
        get_coverage_summary(spy, None)
    assert len(spy.calls) == 0


def test_summarise_empty_catalogue() -> None:
    summary = summarise_coverage([])
    assert summary.total_controls == 0
    assert (summary.met, summary.partial, summary.gap) == (0, 0, 0)
    assert summary.categories == []
