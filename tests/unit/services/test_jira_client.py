"""Unit tests for src/services/jira_client.py — the minimal Jira REST connector (KER-110).

Four tests cover successful issue creation (asserting the request payload and auth header),
HTTP failure mapping, transport failure mapping, and missing-configuration failure.
All HTTP traffic goes through httpx.MockTransport; no real Jira is contacted.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from src.exceptions import JiraClientError
from src.services.jira_client import JiraClient

_DUE_DATE = date(2026, 7, 17)


@pytest.fixture
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_API_TOKEN", "test-token")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "KERNO")


def _client_with(handler) -> JiraClient:
    return JiraClient(http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_create_issue_returns_key_and_sends_correct_payload(jira_env):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(201, json={"key": "KERNO-7"})

    client = _client_with(handler)
    issue_key = client.create_issue(
        project_key=client.project_key,
        summary="Remediation: NIS2-1.1 — Governance policy",
        assignee_account_id="acct-42",
        due_date=_DUE_DATE,
        description="Control gap details",
    )
    assert issue_key == "KERNO-7"
    request = captured["request"]
    assert request.url.path == "/rest/api/2/issue"
    assert request.headers["Authorization"] == "Bearer test-token"
    fields = json.loads(request.content)["fields"]
    assert fields["project"]["key"] == "KERNO"
    assert fields["assignee"]["accountId"] == "acct-42"
    assert fields["duedate"] == "2026-07-17"
    assert fields["summary"].startswith("Remediation:")


def test_non_created_response_raises_jira_client_error(jira_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errorMessages": ["bad request"]})

    client = _client_with(handler)
    with pytest.raises(JiraClientError, match="HTTP 400"):
        client.create_issue(
            project_key="KERNO", summary="s", assignee_account_id="a",
            due_date=_DUE_DATE, description="d",
        )


def test_transport_failure_raises_jira_client_error(jira_env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client_with(handler)
    with pytest.raises(JiraClientError, match="request failed"):
        client.create_issue(
            project_key="KERNO", summary="s", assignee_account_id="a",
            due_date=_DUE_DATE, description="d",
        )


def test_missing_configuration_raises_jira_client_error(monkeypatch):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)
    with pytest.raises(JiraClientError, match="not configured"):
        JiraClient()
