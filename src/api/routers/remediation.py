"""FastAPI router for remediation endpoints mounted at /api/v1/remediation.
Thin translation layer — routing, Jira creation, and audit writes live in remediation_service.

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_remediation.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.remediation import (
    CloseCallbackRequest,
    CloseCallbackResponse,
    TriggerRemediationRequest,
    TriggerRemediationResponse,
)
from src.exceptions import JiraClientError
from src.services.remediation_service import flag_for_rereview, trigger_remediation

router = APIRouter()


class _SessionContext:
    """Adapts the authenticated tenant_id to the session interface the service expects.
    Mirrors the KER-106 overrides router adapter (duplicated because that file must not change)."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> str:
        return self._tenant_id


@router.post("/trigger", status_code=201)
def trigger(
    body: TriggerRemediationRequest,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> TriggerRemediationResponse:
    """Create a Jira remediation task for a confirmed gap; 422 if the control is not a gap
    or has no routing rule, 503 when Jira is unreachable or unconfigured."""
    try:
        result = trigger_remediation(conn, _SessionContext(tenant_id), body.control_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except JiraClientError:
        # SEC-04: do not leak Jira's internal error text (endpoints, timeouts) to
        # the caller; the full exception is available server-side via logging.
        raise HTTPException(
            status_code=503, detail="Jira integration error — check server logs."
        )
    return TriggerRemediationResponse(
        control_id=result.control_id,
        jira_issue_key=result.jira_issue_key,
        due_date=result.due_date,
    )


@router.post("/close-callback")
def close_callback(
    body: CloseCallbackRequest,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> CloseCallbackResponse:
    """Flag a control for re-review after its Jira remediation task is closed.
    422 when no open remediation task matches the (control, issue key) pair for this tenant."""
    try:
        result = flag_for_rereview(
            conn, _SessionContext(tenant_id), body.control_id, body.jira_issue_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return CloseCallbackResponse(
        control_id=result.control_id,
        flagged_for_rereview=result.flagged_for_rereview,
    )
