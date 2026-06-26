"""dora_roi_submission_service.py — DORA RoI submission run lifecycle service.

What:  Manages the lifecycle of DORA Register of Information submission runs —
       creating draft runs, building and recording validated export packages,
       listing open submission windows, and listing a tenant's submission history.

Why:   Document 16 (KER-106 part 3) wraps the validated export package from Doc 15
       into an auditable submission run record. The service is the single write path
       for dora_submission_runs rows. A future authority-portal integration consumes
       the run record (reading status and submission_reference) without needing to
       know how the export was built.

How to run or test:
    pytest tests/unit/services/test_dora_roi_submission_service.py -v
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone

from sqlalchemy.exc import IntegrityError

from config.constants import VALIDATION_SEVERITY_FAIL, VALIDATION_SEVERITY_PASS
from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError
from src.models.dora_submission_run import (
    SUBMISSION_STATUS_DRAFT,
    SUBMISSION_STATUS_READY,
)
from src.services.dora_roi_export_service import DORAExportPackage, build_export_package

# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SubmissionWindowOutput:
    """Return type for submission window queries.

    Mirrors the dora_submission_windows table columns. Dates are Python date
    objects (not ISO strings) because they are stored and returned as SQL DATE.
    """

    id: str
    authority_code: str
    reporting_year: int
    register_reference_date: date
    window_open_date: date
    window_close_date: date
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass(frozen=True)
class SubmissionRunOutput:
    """Return type for submission run queries and mutations.

    Mirrors the dora_submission_runs table columns. submitted_at and
    submission_reference are None until an authority-portal integration sets them.
    """

    id: str
    tenant_id: str
    submission_window_id: str
    reporting_year: int
    status: str
    validation_overall_status: str
    validation_issue_count: int
    entry_count: int
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    submission_reference: str | None


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SELECT_WINDOW_BY_ID = """
SELECT id, authority_code, reporting_year, register_reference_date,
       window_open_date, window_close_date, created_at, updated_at
FROM dora_submission_windows
WHERE id = :window_id
"""

_SELECT_OPEN_WINDOWS = """
SELECT id, authority_code, reporting_year, register_reference_date,
       window_open_date, window_close_date, created_at, updated_at
FROM dora_submission_windows
WHERE window_open_date <= :today
  AND window_close_date >= :today
ORDER BY window_open_date ASC, authority_code ASC
"""

_SELECT_EXISTING_RUN = """
SELECT id, tenant_id, submission_window_id, reporting_year, status,
       validation_overall_status, validation_issue_count, entry_count,
       created_at, updated_at, submitted_at, submission_reference
FROM dora_submission_runs
WHERE tenant_id = :tenant_id
  AND submission_window_id = :window_id
  AND reporting_year = :reporting_year
ORDER BY created_at DESC
LIMIT 1
"""

_SELECT_TENANT_RUNS = """
SELECT id, tenant_id, submission_window_id, reporting_year, status,
       validation_overall_status, validation_issue_count, entry_count,
       created_at, updated_at, submitted_at, submission_reference
FROM dora_submission_runs
WHERE tenant_id = :tenant_id
ORDER BY reporting_year DESC, created_at DESC
"""

_INSERT_RUN = """
INSERT INTO dora_submission_runs (
    id, tenant_id, submission_window_id, reporting_year, status,
    validation_overall_status, validation_issue_count, entry_count,
    created_at, updated_at, submitted_at, submission_reference
) VALUES (
    :id, :tenant_id, :submission_window_id, :reporting_year, :status,
    :validation_overall_status, :validation_issue_count, :entry_count,
    :created_at, :updated_at, NULL, NULL
)
"""

_UPDATE_RUN = """
UPDATE dora_submission_runs
SET status = :status,
    validation_overall_status = :validation_overall_status,
    validation_issue_count = :validation_issue_count,
    entry_count = :entry_count,
    updated_at = :updated_at
WHERE id = :id
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_submission_run(
    conn, tenant_id: str, submission_window_id: str
) -> SubmissionRunOutput:
    """Create a new draft submission run for the tenant and window.

    Validates tenant_id, verifies the window exists, derives the reporting_year
    from the window, then inserts a new dora_submission_runs row with pessimistic
    defaults (status=draft, validation_overall_status=fail, counts=0).
    Raises TenantContextMissingError if tenant_id is falsey.
    Raises ValueError if the submission window does not exist.
    """
    _guard_tenant(tenant_id)
    window = _fetch_window_by_id(conn, submission_window_id)
    if window is None:
        raise ValueError(f"Submission window {submission_window_id!r} not found")
    set_tenant_context(conn, tenant_id)
    now = datetime.now(timezone.utc)
    return _insert_draft_run(conn, tenant_id, submission_window_id, window.reporting_year, now)


def build_and_record_submission(
    conn, tenant_id: str, submission_window_id: str
) -> tuple[SubmissionRunOutput, DORAExportPackage]:
    """Build an export package, validate it, and record the outcome as a submission run.

    Validates tenant_id, fetches the window for reporting_year, calls Doc 15's
    build_export_package (which sets tenant context and runs validation), then
    creates or updates the submission run for the (tenant, window, year) slot.
    Status is 'ready' if validation passes, 'draft' if warn or fail.
    Does not set submitted_at (that is reserved for authority-portal integration).
    Raises TenantContextMissingError if tenant_id is falsey.
    Raises ValueError if the submission window does not exist.
    """
    _guard_tenant(tenant_id)
    window = _fetch_window_by_id(conn, submission_window_id)
    if window is None:
        raise ValueError(f"Submission window {submission_window_id!r} not found")
    package = build_export_package(conn, tenant_id, window.reporting_year)
    set_tenant_context(conn, tenant_id)
    run = _upsert_submission_run(conn, tenant_id, submission_window_id, window.reporting_year, package)
    return run, package


def list_open_windows(conn) -> list[SubmissionWindowOutput]:
    """Return all submission windows that are currently open as of today.

    A window is open when window_open_date <= today <= window_close_date.
    This is global reference data; no tenant context is set.
    Results are ordered by window_open_date ASC, then authority_code ASC.
    """
    today = date.today()
    rows = conn.execute(_SELECT_OPEN_WINDOWS, {"today": today}).fetchall()
    return [_window_row_to_output(row) for row in rows]


def list_tenant_submission_runs(conn, tenant_id: str) -> list[SubmissionRunOutput]:
    """Return all submission runs for the tenant, ordered by reporting_year DESC, created_at DESC.

    Sets tenant context before querying so RLS returns only this tenant's rows.
    Raises TenantContextMissingError if tenant_id is falsey.
    """
    _guard_tenant(tenant_id)
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(_SELECT_TENANT_RUNS, {"tenant_id": str(tenant_id)}).fetchall()
    return [_run_row_to_output(row) for row in rows]


# ---------------------------------------------------------------------------
# Private helpers — tenant guard and window lookup
# ---------------------------------------------------------------------------


def _guard_tenant(tenant_id: str) -> None:
    """Raise TenantContextMissingError immediately if tenant_id is falsey.

    Provides a fast, clear failure before any DB call is attempted.
    set_tenant_context provides additional UUID-format validation for non-empty values.
    """
    if not tenant_id:
        raise TenantContextMissingError("tenant_id is required for submission workflow operations")


def _fetch_window_by_id(conn, window_id: str) -> SubmissionWindowOutput | None:
    """Return the SubmissionWindowOutput for a given window ID, or None if not found.

    Queries dora_submission_windows (global data — no tenant context required).
    """
    row = conn.execute(_SELECT_WINDOW_BY_ID, {"window_id": str(window_id)}).fetchone()
    return _window_row_to_output(row) if row is not None else None


# ---------------------------------------------------------------------------
# Private helpers — run upsert logic
# ---------------------------------------------------------------------------


def _upsert_submission_run(
    conn, tenant_id: str, window_id: str, reporting_year: int, package: DORAExportPackage
) -> SubmissionRunOutput:
    """Race guard for concurrent scheduler processes: if the UNIQUE constraint fires on INSERT,
    re-fetch the row the winning process created and update it rather than propagating the error."""
    existing = _find_existing_run(conn, tenant_id, window_id, reporting_year)
    run_status = _status_from_validation(package.validation_summary.overall_status)
    now = datetime.now(timezone.utc)
    if existing is None:
        try:
            return _insert_draft_run(conn, tenant_id, window_id, reporting_year, now,
                                     run_status=run_status, package=package)
        except IntegrityError:
            existing = _find_existing_run(conn, tenant_id, window_id, reporting_year)
            if existing is None:
                raise RuntimeError(
                    f"IntegrityError recovery failed: could not re-fetch run for "
                    f"tenant={tenant_id!r} window={window_id!r} year={reporting_year}. "
                    "The constraint violation may not have been caused by the expected "
                    "concurrent-INSERT race."
                ) from None
    return _update_existing_run(conn, existing, run_status, package, now)


def _find_existing_run(
    conn, tenant_id: str, window_id: str, reporting_year: int
) -> SubmissionRunOutput | None:
    """Return the most recent run for this (tenant, window, year) slot, or None.

    Results are ordered by created_at DESC so the most recent attempt is returned
    when multiple runs exist for the same slot.
    """
    row = conn.execute(
        _SELECT_EXISTING_RUN,
        {"tenant_id": str(tenant_id), "window_id": str(window_id), "reporting_year": reporting_year},
    ).fetchone()
    return _run_row_to_output(row) if row is not None else None


def _insert_draft_run(
    conn, tenant_id: str, window_id: str, reporting_year: int, now: datetime,
    run_status: str = SUBMISSION_STATUS_DRAFT,
    package: DORAExportPackage | None = None,
) -> SubmissionRunOutput:
    """Insert a new dora_submission_runs row and return the created SubmissionRunOutput.

    When called from create_submission_run, package is None and pessimistic defaults
    are used. When called from _upsert_submission_run, package is provided and its
    validation fields are stored.
    """
    run_id = str(uuid.uuid4())
    val_status = package.validation_summary.overall_status if package else VALIDATION_SEVERITY_FAIL
    val_count = package.validation_summary.issue_count if package else 0
    entries = package.entry_count if package else 0
    conn.execute(_INSERT_RUN, {
        "id": run_id,
        "tenant_id": str(tenant_id),
        "submission_window_id": str(window_id),
        "reporting_year": reporting_year,
        "status": run_status,
        "validation_overall_status": val_status,
        "validation_issue_count": val_count,
        "entry_count": entries,
        "created_at": now,
        "updated_at": now,
    })
    return SubmissionRunOutput(
        id=run_id, tenant_id=str(tenant_id), submission_window_id=str(window_id),
        reporting_year=reporting_year, status=run_status,
        validation_overall_status=val_status, validation_issue_count=val_count,
        entry_count=entries, created_at=now, updated_at=now,
        submitted_at=None, submission_reference=None,
    )


def _update_existing_run(
    conn, existing: SubmissionRunOutput, run_status: str,
    package: DORAExportPackage, now: datetime,
) -> SubmissionRunOutput:
    """Update the status, validation fields, and entry_count on an existing run.

    Preserves created_at, submitted_at, and submission_reference from the existing
    row. Does not set submitted_at — that is reserved for authority-portal integration.
    """
    conn.execute(_UPDATE_RUN, {
        "id": existing.id,
        "status": run_status,
        "validation_overall_status": package.validation_summary.overall_status,
        "validation_issue_count": package.validation_summary.issue_count,
        "entry_count": package.entry_count,
        "updated_at": now,
    })
    return SubmissionRunOutput(
        id=existing.id, tenant_id=existing.tenant_id,
        submission_window_id=existing.submission_window_id,
        reporting_year=existing.reporting_year, status=run_status,
        validation_overall_status=package.validation_summary.overall_status,
        validation_issue_count=package.validation_summary.issue_count,
        entry_count=package.entry_count, created_at=existing.created_at,
        updated_at=now, submitted_at=existing.submitted_at,
        submission_reference=existing.submission_reference,
    )


def _status_from_validation(validation_overall_status: str) -> str:
    """Map a validation overall_status to a submission run status.

    Only a 'pass' validation result promotes the run to 'ready'.
    Both 'warn' and 'fail' keep the run in 'draft' because the package still has
    unresolved issues that a compliance engineer should review before filing.
    """
    if validation_overall_status == VALIDATION_SEVERITY_PASS:
        return SUBMISSION_STATUS_READY
    return SUBMISSION_STATUS_DRAFT


# ---------------------------------------------------------------------------
# Private helpers — row-to-dataclass converters
# ---------------------------------------------------------------------------


def _window_row_to_output(row) -> SubmissionWindowOutput:
    """Map a dora_submission_windows SELECT result row (by position) to SubmissionWindowOutput.

    Column order: 0=id, 1=authority_code, 2=reporting_year, 3=register_reference_date,
    4=window_open_date, 5=window_close_date, 6=created_at, 7=updated_at.
    """
    return SubmissionWindowOutput(
        id=str(row[0]),
        authority_code=row[1],
        reporting_year=row[2],
        register_reference_date=row[3],
        window_open_date=row[4],
        window_close_date=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


def _run_row_to_output(row) -> SubmissionRunOutput:
    """Map a dora_submission_runs SELECT result row (by position) to SubmissionRunOutput.

    Column order: 0=id, 1=tenant_id, 2=submission_window_id, 3=reporting_year,
    4=status, 5=validation_overall_status, 6=validation_issue_count, 7=entry_count,
    8=created_at, 9=updated_at, 10=submitted_at, 11=submission_reference.
    """
    return SubmissionRunOutput(
        id=str(row[0]),
        tenant_id=str(row[1]),
        submission_window_id=str(row[2]),
        reporting_year=row[3],
        status=row[4],
        validation_overall_status=row[5],
        validation_issue_count=row[6],
        entry_count=row[7],
        created_at=row[8],
        updated_at=row[9],
        submitted_at=row[10],
        submission_reference=row[11],
    )
