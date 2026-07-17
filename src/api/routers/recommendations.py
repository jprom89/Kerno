"""FastAPI router for the recommendation review list mounted at /api/v1/recommendations.
Read-only and thin (KER-303) — the "open" predicate lives in recommendation_service;
the tenant always comes from the verified JWT, never the request.

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_recommendations.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from config.constants import (
    RECOMMENDATIONS_DEFAULT_PAGE_SIZE,
    RECOMMENDATIONS_MAX_PAGE_SIZE,
)
from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.recommendations import (
    RecommendationListItem,
    RecommendationListResponse,
)
from src.services.recommendation_service import list_open_recommendations

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
