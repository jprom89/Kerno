"""FastAPI router for the recommendation surfaces mounted at /api/v1/recommendations.
Thin (KER-303 read list + KER-401 generation trigger) — the "open" predicate and
the hybrid engine live in recommendation_service; the tenant and the triggering
user always come from the verified JWT, never the request.

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_recommendations.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from config.constants import (
    RECOMMENDATIONS_DEFAULT_PAGE_SIZE,
    RECOMMENDATIONS_MAX_PAGE_SIZE,
)
from src.api.dependencies import get_conn, get_tenant_id, require_role
from src.api.rate_limit import limiter
# get_reviewer_id is the existing verified-JWT user-identity dependency
# (KER-202); reused so generation attribution reads the sub claim the same way
# override attribution does.
from src.api.routers.overrides import get_reviewer_id
from src.api.schemas.recommendations import (
    GeneratedRecommendationResponse,
    GenerateRecommendationRequest,
    RecommendationListItem,
    RecommendationListResponse,
)
from src.services.recommendation_service import (
    GENERATE_CAPABLE_ROLES,
    generate_recommendation,
    list_open_recommendations,
)

router = APIRouter()


@router.get("")
def list_recommendations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(
        default=RECOMMENDATIONS_DEFAULT_PAGE_SIZE, ge=1, le=RECOMMENDATIONS_MAX_PAGE_SIZE
    ),
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> RecommendationListResponse:
    """Return one page of the tenant's open recommendations, newest first.

    Open = current (not superseded) with no override recorded after generation
    (the exact KER-303 predicate, corrected 15 July 2026). Read-only; the
    review actions themselves go through POST /api/v1/overrides.
    """
    items, total = list_open_recommendations(conn, tenant_id, page, page_size)
    return RecommendationListResponse(
        items=[RecommendationListItem.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/generate", status_code=201)
@limiter.limit("10/minute")
def generate(
    request: Request,
    body: GenerateRecommendationRequest,
    tenant_id: str = Depends(get_tenant_id),
    user_id: str = Depends(get_reviewer_id),
    rbac_role: str = Depends(require_role(*GENERATE_CAPABLE_ROLES)),
    conn=Depends(get_conn),
) -> GeneratedRecommendationResponse:
    """Analyse one control now: run the hybrid engine and persist the result (KER-401).

    The deterministic scorer decides status/confidence/citations; the LLM only
    writes the rationale (template fallback disclosed via rationale_source).
    Rate-limited because each call may invoke the LLM. Unknown control → 404
    (EntryNotFoundError via the app handler). The triggering user's verified
    identity lands in the KER-107 ledger entry.
    """
    output = generate_recommendation(
        conn,
        tenant_id,
        body.control_id,
        triggered_by_user_id=user_id,
        triggered_by_role=rbac_role,
    )
    return GeneratedRecommendationResponse(
        recommendation_id=output.recommendation_id,
        control_id=output.control_id,
        status=output.status,
        confidence_level=output.confidence_level,
        confidence_score=output.confidence_score,
        rationale=output.rationale,
        rationale_source=output.input_snapshot.get("rationale_source", "template"),
        evidence_ids=output.evidence_ids,
        requires_review=output.requires_review,
        generated_at=output.generated_at,
    )
