"""FastAPI router for DORA register entry endpoints mounted at /api/v1/register.
Thin translation layer only — all business logic lives in dora_roi_service."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.register import (
    RegisterEntryRequest,
    RegisterEntryResponse,
    ReportingWindowResponse,
)
from src.exceptions import EntryNotFoundError
from src.services.dora_roi_service import (
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
    conn=Depends(get_conn),
) -> RegisterEntryResponse:
    """Create a new register entry for the authenticated tenant."""
    entry_input = RegisterEntryInput(**body.model_dump())
    result = create_register_entry(conn, tenant_id, entry_input)
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
    """Return a single register entry by ID, or 404 if not found."""
    result = get_register_entry(conn, tenant_id, entry_id)
    if result is None:
        raise EntryNotFoundError(entry_id)
    return RegisterEntryResponse.model_validate(result)


@router.patch("/entries/{entry_id}")
def update_entry(
    entry_id: str,
    body: RegisterEntryRequest,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> RegisterEntryResponse:
    """Update an existing register entry by ID, or 404 if not found."""
    entry_input = RegisterEntryInput(**body.model_dump())
    result = update_register_entry(conn, tenant_id, entry_id, entry_input)
    if result is None:
        raise EntryNotFoundError(entry_id)
    return RegisterEntryResponse.model_validate(result)


@router.get("/windows")
def list_windows(conn=Depends(get_conn)) -> list[ReportingWindowResponse]:
    """List DORA reporting windows. Global reference data — no auth required."""
    results = list_reporting_windows(conn)
    return [ReportingWindowResponse.model_validate(r) for r in results]
