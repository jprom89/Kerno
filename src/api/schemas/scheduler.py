"""Pydantic response model for the manual scheduler trigger endpoint (KER-201).
Reports what one real per-tenant recalculation did: how many overrides were
processed, the bias vector's dimension count, and whether anything was written."""

from __future__ import annotations

from pydantic import BaseModel


class RecalculationRunResponse(BaseModel):
    """The outcome of one real bias recalculation run for the calling tenant.

    ``status`` is "recalculated" when the bias vector was updated and the audit
    ledger entry written, or "no_new_overrides" when there was nothing new to
    process and nothing was written. ``dimensions`` is the length of the
    tenant's bias vector (0 when the tenant has never been calibrated).
    """

    tenant_id: str
    override_count: int
    dimensions: int
    duration_ms: int
    status: str
