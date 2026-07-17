"""Pydantic response models for the control-coverage dashboard endpoints (KER-109, KER-302).
Read-only shapes mirroring the coverage_service dataclasses — no write models here.

Why:   request/response contracts live apart from routing so the API surface
       is reviewable in one place.
How:   pytest tests/unit/api/test_coverage.py -v
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CategoryCoverageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category: str
    met: int
    partial: int
    gap: int
    total: int


class CoverageSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_controls: int
    met: int
    partial: int
    gap: int
    categories: list[CategoryCoverageOut]
    # When this tenant's bias vector was last recalculated (KER-302 AC-3);
    # null = never calibrated. Source: retrieval_bias.last_recalculated_at.
    last_recalculated_at: datetime | None = None


class CoverageControlItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    control_id: str
    control_ref: str
    title: str
    category: str
    framework: str
    status: str
    status_source: str
    human_confirmed: bool
    confidence_level: str | None
    confidence_score: float | None
    evidence_count: int
