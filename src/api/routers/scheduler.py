"""FastAPI router for the manual scheduler trigger mounted at /api/v1/scheduler.
Internal/admin surface (KER-201) — the nightly batch runs via the cron entrypoint in
src/scheduler/nightly_bias_recalculation.py; this endpoint recalculates one tenant on demand.

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_scheduler.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.api.dependencies import get_conn, get_tenant_id
from src.api.rate_limit import limiter
from src.api.schemas.scheduler import RecalculationRunResponse
from src.scheduler.nightly_bias_recalculation import run_tenant_recalculation

router = APIRouter()


class _SessionContext:
    """Adapts the authenticated tenant_id to the session interface the scheduler expects.
    Mirrors the KER-106 overrides router adapter (duplicated because that file must not change)."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> str:
        return self._tenant_id


@router.post("/run-recalculation")
@limiter.limit("10/minute")
def run_recalculation(
    request: Request,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> RecalculationRunResponse:
    """Run a real bias recalculation for the authenticated tenant (KER-201).

    Processes every override captured since the tenant's last recalculation,
    updates the retrieval_bias row, and records the run in the KER-107 audit
    ledger — all in one transaction. Reports status "no_new_overrides" (and
    writes nothing) when there is nothing new to process.
    """
    result = run_tenant_recalculation(conn, _SessionContext(tenant_id))
    return RecalculationRunResponse(
        tenant_id=result.tenant_id,
        override_count=result.override_count,
        dimensions=result.dimensions,
        duration_ms=result.duration_ms,
        status=result.status,
    )
