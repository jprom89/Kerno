"""FastAPI router for the monthly meeting pack mounted at /api/v1/meetings.

What:  GET /pack returns the exception-review agenda for the authenticated
       tenant. Read-only; tenant_id comes from the JWT, never the request.
Why:  HTTP stays here so meeting_pack_service remains framework-free.
How:  pytest tests/unit/api/test_meetings.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.meetings import MeetingPackResponse
from src.services.meeting_pack_service import build_meeting_pack

router = APIRouter()


@router.get("/pack")
def meeting_pack(
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> MeetingPackResponse:
    """Return the NIS2 exception-review agenda built from live tenant data."""
    return MeetingPackResponse.model_validate(build_meeting_pack(conn, tenant_id))
