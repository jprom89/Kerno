"""Unit tests for src/services/remediation_service.py — gap remediation routing (KER-110).

Eleven tests cover gap-only enforcement, routing-rule lookup (category match with tenant
default fallback), Jira call parameters, task-row persistence, audit-ledger entries on both
trigger and closure, and tenant isolation. The Jira client is mocked at the module level
and spy connections serve every query; no database or network is touched.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.exceptions import TenantContextMissingError
from src.services.remediation_service import flag_for_rereview, trigger_remediation

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_CONTROL_ID = "e1000000-0000-4000-a000-000000000001"
_TASK_ID = "f1000000-0000-4000-a000-000000000001"
_ISSUE_KEY = "KERNO-123"


# ── Test infrastructure ───────────────────────────────────────────────────────


class _RowsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _RemediationSpyConn:
    """Serves canned rows for each query the trigger and closure flows issue."""

    def __init__(
        self,
        coverage_rows: list | None = None,
        category_rule: tuple | None = None,
        default_rule: tuple | None = None,
        recommendation_row: tuple | None = None,
        open_task_row: tuple | None = None,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self._coverage_rows = coverage_rows or []
        self._category_rule = category_rule
        self._default_rule = default_rule
        self._recommendation_row = recommendation_row
        self._open_task_row = open_task_row

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "FROM compliance_controls" in sql:
            return _RowsResult(self._coverage_rows)
        if "FROM remediation_routing_rules" in sql:
            if "control_category = :control_category" in sql:
                return _RowsResult([self._category_rule] if self._category_rule else [])
            return _RowsResult([self._default_rule] if self._default_rule else [])
        if "FROM recommendations" in sql:
            return _RowsResult([self._recommendation_row] if self._recommendation_row else [])
        if "FROM remediation_tasks" in sql:
            return _RowsResult([self._open_task_row] if self._open_task_row else [])
        return _RowsResult([])

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _FakeSession:
    def __init__(self, tenant_id=_TENANT_ID) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self):
        return self._tenant_id


def _coverage_row(status: str | None = "gap", override_action: str | None = None) -> tuple:
    # Column order mirrors coverage_service._COVERAGE_QUERY.
    return (
        _CONTROL_ID, "NIS2-1.1", "Governance policy", "governance", "nis2",
        status, "low" if status else None, 0.2 if status else None, override_action, 1,
    )


def _recommendation_row() -> tuple:
    # Column order mirrors recommendation_service._SELECT_CURRENT.
    return (
        "r0000000-0000-4000-a000-000000000001", str(_TENANT_ID), _CONTROL_ID,
        "gap", "low", 0.2, "No active evidence records were found.", "Coverage missing.",
        [], True, {}, datetime(2025, 6, 1, tzinfo=timezone.utc), False,
    )


def _gap_spy(**kwargs) -> _RemediationSpyConn:
    defaults = {
        "coverage_rows": [_coverage_row("gap")],
        "default_rule": ("a0000000-0000-4000-a000-00000000000a", "default-assignee", 14),
        "recommendation_row": _recommendation_row(),
    }
    defaults.update(kwargs)
    return _RemediationSpyConn(**defaults)


def _trigger(spy, session=None):
    with patch("src.services.remediation_service.JiraClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.project_key = "KERNO"
        mock_client.create_issue.return_value = _ISSUE_KEY
        result = trigger_remediation(spy, session or _FakeSession(), _CONTROL_ID)
    return result, mock_client


def _find_params(spy, fragment: str) -> dict:
    return next(p for s, p in spy.calls if fragment in s)


# ── trigger_remediation ───────────────────────────────────────────────────────


def test_non_gap_control_rejected_before_jira() -> None:
    spy = _gap_spy(coverage_rows=[_coverage_row("met")])
    with patch("src.services.remediation_service.JiraClient") as mock_client_cls:
        with pytest.raises(ValueError, match="confirmed gaps"):
            trigger_remediation(spy, _FakeSession(), _CONTROL_ID)
    mock_client_cls.assert_not_called()
    assert not any("INSERT INTO remediation_tasks" in s for s, _ in spy.calls)


def test_unknown_control_rejected() -> None:
    spy = _gap_spy(coverage_rows=[])
    with pytest.raises(ValueError, match="not found"):
        _trigger(spy)


def test_category_rule_preferred_over_default() -> None:
    spy = _gap_spy(
        category_rule=("b0000000-0000-4000-a000-00000000000b", "governance-assignee", 5),
    )
    result, mock_client = _trigger(spy)
    assert mock_client.create_issue.call_args.kwargs["assignee_account_id"] == "governance-assignee"
    assert result.due_date == datetime.now(timezone.utc).date() + timedelta(days=5)


def test_falls_back_to_tenant_default_rule() -> None:
    spy = _gap_spy(category_rule=None)
    result, mock_client = _trigger(spy)
    assert mock_client.create_issue.call_args.kwargs["assignee_account_id"] == "default-assignee"
    rule_queries = [s for s, _ in spy.calls if "FROM remediation_routing_rules" in s]
    assert "control_category = :control_category" in rule_queries[0]
    assert "IS NULL" in rule_queries[1]


def test_no_routing_rule_raises_value_error() -> None:
    spy = _gap_spy(category_rule=None, default_rule=None)
    with pytest.raises(ValueError, match="routing rule"):
        _trigger(spy)


def test_jira_called_with_control_reference_sla_and_rationale() -> None:
    spy = _gap_spy()
    result, mock_client = _trigger(spy)
    kwargs = mock_client.create_issue.call_args.kwargs
    assert kwargs["project_key"] == "KERNO"
    assert kwargs["summary"] == "Remediation: NIS2-1.1 — Governance policy"
    assert kwargs["due_date"] == datetime.now(timezone.utc).date() + timedelta(days=14)
    assert "NIS2-1.1" in kwargs["description"]
    assert "No active evidence records were found." in kwargs["description"]
    assert result.jira_issue_key == _ISSUE_KEY


def test_task_row_inserted_with_snapshot() -> None:
    spy = _gap_spy()
    _trigger(spy)
    params = _find_params(spy, "INSERT INTO remediation_tasks")
    assert params["control_id"] == _CONTROL_ID
    assert params["jira_issue_key"] == _ISSUE_KEY
    assert params["assignee_jira_account_id"] == "default-assignee"
    assert params["tenant_id"] == str(_TENANT_ID)


def test_audit_entry_written_on_trigger() -> None:
    spy = _gap_spy()
    result, _ = _trigger(spy)
    params = _find_params(spy, "INSERT INTO audit_log")
    assert params["action_type"] == "remediation_triggered"
    assert params["object_type"] == "control"
    assert params["object_id"] == _CONTROL_ID
    after_state = json.loads(params["after_state"])
    assert after_state["jira_issue_key"] == _ISSUE_KEY
    assert after_state["due_date"] == result.due_date.isoformat()


def test_none_session_raises_before_sql() -> None:
    spy = _gap_spy()
    with pytest.raises(TenantContextMissingError):
        trigger_remediation(spy, None, _CONTROL_ID)
    assert len(spy.calls) == 0


# ── flag_for_rereview ─────────────────────────────────────────────────────────


def test_closure_flags_rereview_and_writes_audit_entry() -> None:
    spy = _RemediationSpyConn(open_task_row=(_TASK_ID,))
    result = flag_for_rereview(spy, _FakeSession(), _CONTROL_ID, _ISSUE_KEY)
    assert result.flagged_for_rereview is True
    update_params = _find_params(spy, "UPDATE remediation_tasks")
    assert update_params["task_id"] == _TASK_ID
    assert update_params["re_review_flagged_at"] is not None
    audit_params = _find_params(spy, "INSERT INTO audit_log")
    assert audit_params["action_type"] == "remediation_closed"
    assert audit_params["object_id"] == _CONTROL_ID
    assert json.loads(audit_params["after_state"])["flagged_for_rereview"] is True


def test_closure_with_unknown_task_raises_and_writes_nothing() -> None:
    spy = _RemediationSpyConn(open_task_row=None)
    with pytest.raises(ValueError, match="No open remediation task"):
        flag_for_rereview(spy, _FakeSession(), _CONTROL_ID, _ISSUE_KEY)
    assert not any("UPDATE remediation_tasks" in s for s, _ in spy.calls)
    assert not any("INSERT INTO audit_log" in s for s, _ in spy.calls)
