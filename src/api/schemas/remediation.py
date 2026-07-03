"""Pydantic request and response models for the remediation endpoints (KER-110).
tenant_id is never accepted from the request — it always comes from the authenticated session."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class TriggerRemediationRequest(BaseModel):
    control_id: str


class TriggerRemediationResponse(BaseModel):
    control_id: str
    jira_issue_key: str
    due_date: date


class CloseCallbackRequest(BaseModel):
    jira_issue_key: str
    control_id: str


class CloseCallbackResponse(BaseModel):
    control_id: str
    flagged_for_rereview: bool
