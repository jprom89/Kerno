"""FastAPI router for DORA submission run endpoints mounted at /api/v1/submissions.
Thin translation layer only — all business logic lives in dora_roi_submission_service.

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_submissions.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.api.dependencies import get_conn, get_tenant_id, require_lookup_id, require_role

# get_reviewer_id is the existing verified-JWT user identity (KER-202); reused
# here so filing a register attributes to the same actor an override does.
from src.api.routers.overrides import get_reviewer_id
from src.api.schemas.submissions import (
    SubmissionRunRequest,
    SubmissionRunResponse,
    SubmissionWindowResponse,
)
from src.exceptions import EntryNotFoundError
from src.services.dora_roi_submission_service import (
    SUBMISSION_CAPABLE_ROLES,
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
    user_id: str = Depends(get_reviewer_id),
    rbac_role: str = Depends(require_role(*SUBMISSION_CAPABLE_ROLES)),
    conn=Depends(get_conn),
) -> SubmissionRunResponse:
    """Trigger a submission run for the authenticated tenant, attributed to the caller.

    A submission_window_id that does not exist is a 404, and so is one that is
    not a well-formed id — both mean the same thing to the caller, and neither
    should reach the generic 500 handler.
    """
    require_lookup_id(body.submission_window_id, resource="submission window")
    run, _ = build_and_record_submission(
        conn, tenant_id, body.submission_window_id, actor_id=user_id, actor_role=rbac_role
    )
    return SubmissionRunResponse.model_validate(run)


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> SubmissionRunResponse:
    """Return one submission run for the authenticated tenant, or 404 if not found or not a valid ID."""
    require_lookup_id(run_id, resource="submission run")
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
