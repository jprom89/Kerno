"""Pydantic schemas for the Trust Center endpoints (KER-204).

The public status response deliberately carries summary counts ONLY — no
control-level detail, no evidence references, no audit entries, and never a
tenant_id (the slug is the only identifier a public caller sees). The
visibility schemas serve the authenticated opt-in toggle.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class TrustCenterCategoryStatus(BaseModel):
    """Met/partial/gap counts for one NIS2 control category."""

    category: str
    met: int
    partial: int
    gap: int
    total: int


class TrustCenterStatusResponse(BaseModel):
    """The public NIS2 coverage summary for one tenant's Trust Center page.

    Counts derive from the KER-109 system-of-record statuses (human overrides
    win over AI recommendations). generated_at is when the snapshot was
    computed — cached responses keep the original timestamp so the page's age
    is honest.
    """

    tenant_slug: str
    total_controls: int
    met: int
    partial: int
    gap: int
    categories: list[TrustCenterCategoryStatus]
    generated_at: datetime


class TrustCenterVisibilityRequest(BaseModel):
    """The authenticated toggle body: make the tenant's page public or private."""

    public: bool


class TrustCenterVisibilityResponse(BaseModel):
    """Confirms the new visibility state and the slug the public page lives at."""

    tenant_slug: str
    trust_center_public: bool
