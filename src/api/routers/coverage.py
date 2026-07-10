"""FastAPI router for the control-coverage dashboard endpoints mounted at /api/v1/coverage.
Thin read-only translation layer — status resolution lives in coverage_service (KER-109).

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_coverage.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.coverage import CoverageControlItem, CoverageSummaryResponse
from src.services.coverage_service import get_coverage_controls, get_coverage_summary

router = APIRouter()


@router.get("/summary")
def coverage_summary(
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> CoverageSummaryResponse:
    """Return tenant-wide met/partial/gap counts and the per-category breakdown.

    Figures are derived from the same resolution pass as /coverage/controls, so
    the summary always reconciles exactly with the drill-down rows.
    """
    return CoverageSummaryResponse.model_validate(get_coverage_summary(conn, tenant_id))


@router.get("/controls")
def coverage_controls(
    category: str | None = None,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> list[CoverageControlItem]:
    """Return active controls with their system-of-record status, optionally filtered by category.

    A human override (KER-106) wins over the AI recommendation; status_source
    and human_confirmed let the caller distinguish confirmed from machine-only figures.
    """
    controls = get_coverage_controls(conn, tenant_id, category=category)
    return [CoverageControlItem.model_validate(c) for c in controls]
