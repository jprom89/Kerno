"""FastAPI router for the embedded side-panel read endpoint mounted at /api/v1/panel.
Thin read-only translation layer — recommendation and evidence data come from their existing services.

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_panel.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.panel import (
    PanelContextResponse,
    PanelEvidenceItem,
    PanelRecommendation,
)
from src.services.evidence_service import get_evidence_for_control
from src.services.recommendation_service import get_recommendation

router = APIRouter()


@router.get("/controls/{control_id}")
def get_panel_context(
    control_id: str,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> PanelContextResponse:
    """Return the current recommendation and linked evidence for one control, tenant-scoped.

    recommendation is null when none has been generated for this control yet;
    evidence includes broken links (link_status='broken') so the panel can flag
    them instead of silently hiding missing sources.
    """
    recommendation = get_recommendation(conn, tenant_id, control_id)
    evidence = get_evidence_for_control(conn, tenant_id, control_id)
    return PanelContextResponse(
        control_id=control_id,
        recommendation=(
            PanelRecommendation.model_validate(recommendation)
            if recommendation is not None
            else None
        ),
        evidence=[PanelEvidenceItem.model_validate(item) for item in evidence],
    )
