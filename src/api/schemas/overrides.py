"""Pydantic request and response models for the override capture endpoint.
Field names match the override_service dataclass; reviewer_id and tenant_id are never accepted from the request."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from config.constants import ReviewerRole


class OverrideRequest(BaseModel):
    # reviewer_role is bounded to the ReviewerRole enum (SEC-01): an out-of-set
    # value is rejected with 422 instead of reaching the DB enum as a 500.
    reviewer_role: ReviewerRole
    action_type: str
    original_control_id: str
    corrected_control_id: str | None = None
    justification_text: str | None = None


class OverrideResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    override_id: str
    action_type: str
    original_control_id: str
    corrected_control_id: str | None
    created_at: datetime
