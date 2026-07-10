"""FastAPI router for DORA submission run endpoints mounted at /api/v1/submissions.
Thin translation layer only — all business logic lives in dora_roi_submission_service.

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_submissions.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.submissions import (
    SubmissionRunRequest,
    SubmissionRunResponse,
    SubmissionWindowResponse,
)
from src.exceptions import EntryNotFoundError
from src.services.dora_roi_submission_service import (
    build_and_record_submission,
    get_submission_run,
    list_open_windows,
    list_tenant_submission_runs,
)

router = APIRouter()


@router.post("/runs")
def create_run(
    body: SubmissionRunRequest,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> SubmissionRunResponse:
    """Trigger a submission run for the authenticated tenant and return its record."""
    run, _ = build_and_record_submission(conn, tenant_id, body.submission_window_id)
    return SubmissionRunResponse.model_validate(run)


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> SubmissionRunResponse:
    """Return a single submission run by ID for the authenticated tenant. Raises 404 if not found."""
    run = get_submission_run(conn, tenant_id, run_id)
    if run is None:
        raise EntryNotFoundError(f"submission run {run_id!r} not found")
    return SubmissionRunResponse.model_validate(run)


@router.get("/runs")
def list_runs(
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> list[SubmissionRunResponse]:
    """List all submission runs for the authenticated tenant."""
    results = list_tenant_submission_runs(conn, tenant_id)
    return [SubmissionRunResponse.model_validate(r) for r in results]


@router.get("/windows")
def list_windows(conn=Depends(get_conn)) -> list[SubmissionWindowResponse]:
    """List open submission windows. Global reference data — no auth required."""
    results = list_open_windows(conn)
    return [SubmissionWindowResponse.model_validate(r) for r in results]
