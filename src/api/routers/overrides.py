"""FastAPI router for the human override capture endpoint mounted at /api/v1/overrides.
Thin translation layer only — all business logic lives in override_service."""

from __future__ import annotations

import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request

# _oauth2_scheme and _jwt_secret are imported rather than reimplemented so the
# reviewer token is read with the same security scheme as get_tenant_id; the
# no-modify constraint forbids adding a public reviewer dependency to dependencies.py.
from src.api.dependencies import (
    _jwt_secret,
    _oauth2_scheme,
    get_conn,
    get_tenant_id,
    require_role,
)
from src.api.rate_limit import limiter
from src.api.schemas.overrides import OverrideRequest, OverrideResponse
from src.services.override_service import (
    OVERRIDE_CAPABLE_ROLES,
    OverrideInput,
    capture_override,
    resolve_reviewer_role,
)

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
    # sub is the verified per-user user_id (KER-202), validated as a UUID — the real
    # actor recorded on the override and in the KER-107 audit ledger.
    return reviewer_id


@router.post("/overrides", status_code=201)
@limiter.limit("60/minute")
def create_override(
    request: Request,
    body: OverrideRequest,
    tenant_id: str = Depends(get_tenant_id),
    reviewer_id: str = Depends(get_reviewer_id),
    rbac_role: str = Depends(require_role(*OVERRIDE_CAPABLE_ROLES)),
    conn=Depends(get_conn),
) -> OverrideResponse:
    """Capture a human override for the authenticated tenant and reviewer; return the stored record with 201.
    Auditors and any role that may not override are rejected with 403 by require_role. The reviewer_role is
    derived from the verified JWT role, never the body. ValueError -> 422; TenantContextMissingError -> 403."""
    reviewer_role = resolve_reviewer_role(rbac_role)
    if reviewer_role is None:
        # Defensive: require_role already excludes auditor and unknown roles.
        raise HTTPException(
            status_code=403, detail="your role is not permitted to submit overrides"
        )
    override_input = OverrideInput(
        reviewer_id=reviewer_id,
        reviewer_role=reviewer_role,
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
