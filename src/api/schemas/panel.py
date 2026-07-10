"""Pydantic response models for the embedded side-panel read endpoint (KER-108).
Read-only shapes narrowly scoped to what the panel renders; the write path stays on the overrides schemas.

Why:   request/response contracts live apart from routing so the API surface
       is reviewable in one place.
How:   pytest tests/unit/api/test_panel.py -v
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PanelRecommendation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    recommendation_id: str
    control_id: str
    status: str
    confidence_level: str
    confidence_score: float
    rationale: str
    gaps: str | None
    evidence_ids: list[str]
    requires_review: bool
    generated_at: datetime


class PanelEvidenceItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    link_id: str
    record_id: str
    title: str | None
    source_system: str | None
    external_id: str | None
    record_type: str | None
    link_status: str
    relevance_score: float | None
    linked_by: str
    linked_at: datetime


class PanelContextResponse(BaseModel):
    control_id: str
    recommendation: PanelRecommendation | None
    evidence: list[PanelEvidenceItem]
