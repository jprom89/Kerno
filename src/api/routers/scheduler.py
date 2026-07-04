"""FastAPI router for the manual scheduler trigger mounted at /api/v1/scheduler.
Internal/admin surface (KER-114) — no scheduling infrastructure exists in the app yet,
so the nightly recalculation stub is triggered manually through this endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.scheduler import RecalculationRunResponse
from src.scheduler.nightly_bias_recalculation import run_recalculation_stub

router = APIRouter()


class _SessionContext:
    """Adapts the authenticated tenant_id to the session interface the scheduler expects.
    Mirrors the KER-106 overrides router adapter (duplicated because that file must not change)."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> str:
        return self._tenant_id


@router.post("/run-recalculation")
def run_recalculation(
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> RecalculationRunResponse:
    """Run the KER-114 recalculation stub for the authenticated tenant.

    Logs the run, records it in the KER-107 audit ledger, and reports the
    pending override count — without modifying any bias vector (stub).
    """
    result = run_recalculation_stub(conn, _SessionContext(tenant_id))
    return RecalculationRunResponse(
        tenant_id=result.tenant_id,
        override_count=result.override_count,
        duration_ms=result.duration_ms,
        status=result.status,
    )
