"""Pydantic response model for the manual scheduler trigger endpoint (KER-114).
The stub reports what it observed — it never modifies a bias vector."""

from __future__ import annotations

from pydantic import BaseModel


class RecalculationRunResponse(BaseModel):
    tenant_id: str
    override_count: int
    duration_ms: int
    status: str
