"""FastAPI router for the AI-decision log query endpoint mounted at /api/v1/ai-decisions.
Thin translation layer only (KER-203) — filtering and tenant scoping live in
ai_decision_log_service; the tenant always comes from the verified JWT, never the request."""

from __future__ import annotations

import dataclasses
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.ai_decisions import DecisionLogRecord, DecisionLogResponse
from src.services.ai_decision_log_service import query_decision_logs

router = APIRouter()


@router.get("/ai-decisions")
def list_ai_decisions(
    control_id: str | None = Query(default=None),
    after: datetime | None = Query(default=None),
    confidence_gte: float | None = Query(default=None, ge=0.0, le=1.0),
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> DecisionLogResponse:
    """Return the authenticated tenant's retained AI decisions, newest first.

    Optional filters: control_id (exact TEXT control ref, matching the
    recommendations pipeline), after (ISO datetime — decisions created at or
    after it), confidence_gte (0.0–1.0 minimum model confidence). The tenant is
    resolved from the JWT via get_tenant_id and is never accepted from the
    request. TenantContextMissingError maps to 403 via the app-level handler.
    """
    entries = query_decision_logs(
        conn,
        tenant_id,
        control_id=control_id,
        after=after,
        confidence_gte=confidence_gte,
    )
    records = [DecisionLogRecord(**dataclasses.asdict(entry)) for entry in entries]
    return DecisionLogResponse(decisions=records, count=len(records))
