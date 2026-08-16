"""FastAPI router for DORA register entry endpoints mounted at /api/v1/register.
Thin translation layer only — all business logic lives in dora_roi_service.

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_register.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_conn, get_tenant_id, require_lookup_id, require_role

# get_reviewer_id is the existing verified-JWT user identity (KER-202); reused
# here so a register write attributes to the same actor an override does.
from src.api.routers.overrides import get_reviewer_id
from src.api.schemas.register import (
    RegisterEntryRequest,
    RegisterEntryResponse,
    ReportingWindowResponse,
)
from src.exceptions import EntryNotFoundError
from src.services.dora_roi_service import (
    REGISTER_CAPABLE_ROLES,
    RegisterEntryInput,
    create_register_entry,
    get_register_entry,
    list_active_register_entries,
    list_reporting_windows,
    update_register_entry,
)

router = APIRouter()


@router.post("/entries", status_code=201)
def create_entry(
    body: RegisterEntryRequest,
    tenant_id: str = Depends(get_tenant_id),
    user_id: str = Depends(get_reviewer_id),
    rbac_role: str = Depends(require_role(*REGISTER_CAPABLE_ROLES)),
    conn=Depends(get_conn),
) -> RegisterEntryResponse:
    """Create a new register entry for the authenticated tenant, attributed to the caller.

    Field validation that the service rejects — an unknown provider type, an end
    date before the start date — is a 422 carrying the reason, not a 500. The
    person filling in this form is the one who can fix it, so they get told what
    is wrong rather than a correlation ID.
    """
    entry_input = RegisterEntryInput(**body.model_dump())
    try:
        result = create_register_entry(
            conn, tenant_id, entry_input, actor_id=user_id, actor_role=rbac_role
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RegisterEntryResponse.model_validate(result)


@router.get("/entries")
def list_entries(
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> list[RegisterEntryResponse]:
    """List all active register entries for the authenticated tenant."""
    results = list_active_register_entries(conn, tenant_id)
    return [RegisterEntryResponse.model_validate(r) for r in results]


@router.get("/entries/{entry_id}")
def get_entry(
    entry_id: str,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> RegisterEntryResponse:
    """Return a single register entry by ID, or 404 if not found or not a valid ID."""
    require_lookup_id(entry_id, resource="register entry")
    result = get_register_entry(conn, tenant_id, entry_id)
    if result is None:
        raise EntryNotFoundError(entry_id)
    return RegisterEntryResponse.model_validate(result)


@router.patch("/entries/{entry_id}")
def update_entry(
    entry_id: str,
    body: RegisterEntryRequest,
    tenant_id: str = Depends(get_tenant_id),
    user_id: str = Depends(get_reviewer_id),
    rbac_role: str = Depends(require_role(*REGISTER_CAPABLE_ROLES)),
    conn=Depends(get_conn),
) -> RegisterEntryResponse:
    """Update an existing register entry by ID, attributed to the caller, or 404 if not found.

    Same 422-with-a-reason contract as create: an amendment rejected on its
    field values reports why. The id is checked first, so a bad id is a 404
    rather than a validation error about the body.
    """
    require_lookup_id(entry_id, resource="register entry")
    entry_input = RegisterEntryInput(**body.model_dump())
    try:
        result = update_register_entry(
            conn, tenant_id, entry_id, entry_input, actor_id=user_id, actor_role=rbac_role
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise EntryNotFoundError(entry_id)
    return RegisterEntryResponse.model_validate(result)


@router.get("/windows")
def list_windows(conn=Depends(get_conn)) -> list[ReportingWindowResponse]:
    """List DORA reporting windows. Global reference data — no auth required."""
    results = list_reporting_windows(conn)
    return [ReportingWindowResponse.model_validate(r) for r in results]
