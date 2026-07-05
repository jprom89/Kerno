"""FastAPI router for the human override capture endpoint mounted at /api/v1/overrides.
Thin translation layer only — all business logic lives in override_service."""

from __future__ import annotations

import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request

# _oauth2_scheme and _jwt_secret are imported rather than reimplemented so the
# reviewer token is read with the same security scheme as get_tenant_id; the
# no-modify constraint forbids adding a public reviewer dependency to dependencies.py.
from src.api.dependencies import _jwt_secret, _oauth2_scheme, get_conn, get_tenant_id
from src.api.rate_limit import limiter
from src.api.schemas.overrides import OverrideRequest, OverrideResponse
from src.services.override_service import OverrideInput, capture_override

router = APIRouter()


class _SessionContext:
    """Adapts the authenticated tenant_id to the session interface capture_override expects.
    Exposes resolve_tenant_id() so the service reads the tenant from the session, never the request body."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> str:
        return self._tenant_id


def get_reviewer_id(token: str | None = Depends(_oauth2_scheme)) -> str:
    """Return the reviewer identity from the authenticated token's sub claim, never from the request body.
    Raises HTTP 401 if the token is missing, invalid, or carries a non-UUID sub claim."""
    if token is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    reviewer_id = payload.get("sub")
    if not reviewer_id:
        raise HTTPException(status_code=401, detail="sub claim missing")
    try:
        uuid.UUID(reviewer_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="sub is not a valid UUID")
    # LIMITATION (SEC-01): the JWT 'sub' claim currently equals tenant_id (KER-108) —
    # this is the authenticated tenant principal, not a verified per-user identity.
    # reviewer_id is intentionally left on this pattern; per-user JWT claims (which
    # would make this a real person) are deferred post-Sprint 1. The audit ledger
    # records after_state.actor_attribution to make this limitation explicit.
    return reviewer_id


@router.post("/overrides", status_code=201)
@limiter.limit("60/minute")
def create_override(
    request: Request,
    body: OverrideRequest,
    tenant_id: str = Depends(get_tenant_id),
    reviewer_id: str = Depends(get_reviewer_id),
    conn=Depends(get_conn),
) -> OverrideResponse:
    """Capture a human override for the authenticated tenant and reviewer; return the stored record with 201.
    A ValueError from the service becomes HTTP 422; TenantContextMissingError becomes HTTP 403 via the app handler."""
    override_input = OverrideInput(
        reviewer_id=reviewer_id,
        reviewer_role=body.reviewer_role,
        action_type=body.action_type,
        original_control_id=body.original_control_id,
        corrected_control_id=body.corrected_control_id,
        justification_text=body.justification_text,
    )
    try:
        override = capture_override(_SessionContext(tenant_id), conn, override_input)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return OverrideResponse(
        override_id=str(override.override_id),
        action_type=override.action_type,
        original_control_id=override.original_control_id,
        corrected_control_id=override.corrected_control_id,
        created_at=override.created_at,
    )
