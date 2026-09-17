"""Minimal Jira REST client — creates remediation issues for KER-110.

Synchronous (httpx.Client) to match the rest of the codebase; the constructor accepts an
injected client so tests use httpx.MockTransport and never touch a real Jira instance.
Raises JiraClientError for missing configuration and for every API failure — callers map
it to HTTP 503. Run tests with: pytest tests/unit/services/test_jira_client.py -v
"""

from __future__ import annotations

import os
from datetime import date

import httpx

from src.exceptions import JiraClientError

__all__ = ["JiraClient"]

_CREATE_ISSUE_PATH = "/rest/api/2/issue"


class JiraClient:
    """Thin wrapper around the Jira issue-creation endpoint.

    Reads JIRA_BASE_URL, JIRA_API_TOKEN, and JIRA_PROJECT_KEY from the
    environment at construction time and fails loudly (JiraClientError) when
    any are missing, so a misconfigured deployment surfaces before a request
    is attempted. Pass http_client to inject a test transport.
    """

    def __init__(self, http_client: httpx.Client | None = None) -> None:
        """Read connection settings from the environment; raise JiraClientError if absent."""
        self._base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        self._api_token = os.environ.get("JIRA_API_TOKEN", "")
        self.project_key = os.environ.get("JIRA_PROJECT_KEY", "")
        if not self._base_url or not self._api_token or not self.project_key:
            raise JiraClientError(
                "Jira is not configured: JIRA_BASE_URL, JIRA_API_TOKEN, and "
                "JIRA_PROJECT_KEY must all be set."
            )
        self._http = http_client if http_client is not None else httpx.Client()

    def create_issue(
        self,
        project_key: str,
        summary: str,
        assignee_account_id: str,
        due_date: date,
        description: str,
    ) -> str:
        """Create a Jira issue and return its key (e.g. 'KERNO-123').

        Uses the v2 REST endpoint so description is plain text. Wraps every
        transport and HTTP failure in JiraClientError so callers never handle
        httpx exceptions directly.
        """
        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": description,
                "assignee": {"accountId": assignee_account_id},
                "duedate": due_date.isoformat(),
                "issuetype": {"name": "Task"},
            }
        }
        try:
            response = self._http.post(
                f"{self._base_url}{_CREATE_ISSUE_PATH}",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_token}"},
            )
        except httpx.HTTPError as exc:
            raise JiraClientError(f"Jira request failed: {exc}") from exc
        if response.status_code != httpx.codes.CREATED:
            raise JiraClientError(
                f"Jira issue creation returned HTTP {response.status_code}: {response.text}"
            )
        issue_key = response.json().get("key")
        if not issue_key:
            raise JiraClientError("Jira response did not contain an issue key.")
        return issue_key
