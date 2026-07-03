"""Pydantic response models for the control-coverage dashboard endpoints (KER-109).
Read-only shapes mirroring the coverage_service dataclasses — no write models here."""

from __future__ import annotations

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
