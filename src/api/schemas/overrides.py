"""Pydantic request and response models for the override capture endpoint.
Field names match the override_service dataclass; reviewer_id and tenant_id are never accepted from the request."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OverrideRequest(BaseModel):
    # reviewer_role is NOT accepted from the request (KER-202): the reviewer's role
    # is derived from the verified JWT and mapped to a ReviewerRole in
    # override_service.REVIEWER_ROLE_MAP. reviewer_id likewise comes from the JWT.
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
