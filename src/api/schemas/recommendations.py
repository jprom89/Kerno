"""Pydantic response models for the recommendation review list (KER-303).

What:  one RecommendationListItem per open recommendation (with catalogue
       metadata for display and client-side filtering) wrapped in a paginated
       RecommendationListResponse.
Why:   the review UI is the EU AI Act Article 14 human-in-the-loop surface;
       its read contract lives here, apart from the routing.
How:   pytest tests/unit/api/test_recommendations.py -v
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RecommendationListItem(BaseModel):
    """One open recommendation awaiting human review."""

    model_config = ConfigDict(from_attributes=True)

    recommendation_id: str
    control_id: str
    control_ref: str | None
    control_title: str | None
    category: str | None
    status: str
    confidence_level: str
    confidence_score: float
    rationale: str
    evidence_count: int
    generated_at: datetime


class RecommendationListResponse(BaseModel):
    """One page of the review queue with pagination bookkeeping."""

    items: list[RecommendationListItem]
    total: int
    page: int
    page_size: int


class GenerateRecommendationRequest(BaseModel):
    """The on-demand generation trigger body (KER-401): which control to analyse."""

    control_id: str


class GeneratedRecommendationResponse(BaseModel):
    """The freshly generated recommendation as returned by the trigger endpoint.

    Status, confidence, and evidence_ids come from the deterministic scorer
    only; rationale_source discloses whether the prose came from the LLM or
    the template fallback — the demo surface never hides a degraded run.
    """

    recommendation_id: str
    control_id: str
    status: str
    confidence_level: str
    confidence_score: float
    rationale: str
    rationale_source: str
    evidence_ids: list[str]
    requires_review: bool
    generated_at: datetime
