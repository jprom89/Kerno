"""Unit tests for the /api/v1/remediation endpoints (KER-110).
Service functions are mocked at the router level; no database or Jira is touched."""

from __future__ import annotations

import os
from datetime import date
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id
from src.exceptions import JiraClientError
from src.services.remediation_service import RemediationResult, ReReviewResult

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_CONTROL_ID = "e1000000-0000-4000-a000-000000000001"
_ISSUE_KEY = "KERNO-123"

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _override_get_conn():
    yield MagicMock()


def _fake_result() -> RemediationResult:
    return RemediationResult(
        control_id=_CONTROL_ID,
        jira_issue_key=_ISSUE_KEY,
        due_date=date(2026, 7, 17),
        assignee_jira_account_id="acct-42",
    )


def _app_with_overrides():
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_trigger_returns_201_for_gap_control():
    with patch("src.api.routers.remediation.trigger_remediation", return_value=_fake_result()):
        client = TestClient(_app_with_overrides())
        response = client.post("/api/v1/remediation/trigger", json={"control_id": _CONTROL_ID})
    assert response.status_code == 201
    body = response.json()
    assert body["jira_issue_key"] == _ISSUE_KEY
    assert body["due_date"] == "2026-07-17"
    assert body["control_id"] == _CONTROL_ID


def test_trigger_returns_422_for_non_gap_control():
    error = ValueError("Control has status 'met' — remediation can only be triggered for confirmed gaps.")
    with patch("src.api.routers.remediation.trigger_remediation", side_effect=error):
        client = TestClient(_app_with_overrides())
        response = client.post("/api/v1/remediation/trigger", json={"control_id": _CONTROL_ID})
    assert response.status_code == 422
    assert "confirmed gaps" in response.json()["detail"]


def test_trigger_returns_503_when_jira_unreachable():
    with patch(
        "src.api.routers.remediation.trigger_remediation",
        side_effect=JiraClientError("Jira request failed: connection refused"),
    ):
        client = TestClient(_app_with_overrides())
        response = client.post("/api/v1/remediation/trigger", json={"control_id": _CONTROL_ID})
    assert response.status_code == 503


def test_remediation_requires_authentication():
    _app = create_app()
    _app.dependency_overrides[get_conn] = _override_get_conn
    client = TestClient(_app)
    trigger = client.post("/api/v1/remediation/trigger", json={"control_id": _CONTROL_ID})
    callback = client.post(
        "/api/v1/remediation/close-callback",
        json={"control_id": _CONTROL_ID, "jira_issue_key": _ISSUE_KEY},
    )
    assert trigger.status_code == 401
    assert callback.status_code == 401


def test_close_callback_flags_rereview():
    result = ReReviewResult(control_id=_CONTROL_ID, jira_issue_key=_ISSUE_KEY, flagged_for_rereview=True)
    with patch("src.api.routers.remediation.flag_for_rereview", return_value=result) as mock_flag:
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/remediation/close-callback",
            json={"control_id": _CONTROL_ID, "jira_issue_key": _ISSUE_KEY},
        )
    assert response.status_code == 200
    assert response.json() == {"control_id": _CONTROL_ID, "flagged_for_rereview": True}
    # The service receives the session tenant, never caller-supplied identity.
    session_arg = mock_flag.call_args[0][1]
    assert session_arg.resolve_tenant_id() == _TENANT_ID


def test_close_callback_returns_422_for_unknown_task():
    with patch(
        "src.api.routers.remediation.flag_for_rereview",
        side_effect=ValueError("No open remediation task found"),
    ):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/remediation/close-callback",
            json={"control_id": _CONTROL_ID, "jira_issue_key": _ISSUE_KEY},
        )
    assert response.status_code == 422
