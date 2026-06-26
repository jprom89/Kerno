"""Identifies tenants at risk of missing a DORA filing deadline by finding those with no
ready or submitted run for any currently-open authority submission window. Requires an
admin-level database connection that bypasses PostgreSQL RLS; a tenant-scoped connection
raises SchedulerAdminConnectionRequiredError instead of silently returning an empty list."""

from __future__ import annotations

import datetime

from src.models.dora_submission_run import (
    SUBMISSION_STATUS_READY,
    SUBMISSION_STATUS_SUBMITTED,
)
from src.services.dora_roi_submission_service import (
    SubmissionWindowOutput,
    _SELECT_OPEN_WINDOWS,
    _window_row_to_output,
)

_SELECT_ALL_ACTIVE_TENANTS = """
SELECT DISTINCT tenant_id::text
FROM dora_register_entries
"""

# IN clause values come from Python constants, not user input — safe to interpolate at import time.
_SELECT_TENANTS_WITH_COMPLETED_RUNS = f"""
SELECT DISTINCT tenant_id::text
FROM dora_submission_runs
WHERE submission_window_id = :window_id
  AND reporting_year = :reporting_year
  AND status IN ('{SUBMISSION_STATUS_READY}', '{SUBMISSION_STATUS_SUBMITTED}')
"""


# ---------------------------------------------------------------------------
# Module-local exception
# ---------------------------------------------------------------------------


class SchedulerAdminConnectionRequiredError(RuntimeError):
    """Raised when find_tenants_missing_submission cannot establish cross-tenant visibility.

    This error fires when open submission windows exist but tenant enumeration
    returns zero results — a pattern that indicates the database connection is
    restricted by Row-Level Security and is hiding all rows.

    The scheduler must not silently return an empty list in this case, because an
    empty result would be incorrectly interpreted as 'all tenants are compliant'.
    For a compliance scheduling workflow, a loud failure is always preferable to
    a misleading silent success.

    Resolution: pass an admin-level (privileged) database connection that is
    configured to bypass PostgreSQL RLS, not a standard tenant-scoped connection.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_tenants_missing_submission(
    conn, today: datetime.date | None = None
) -> list[tuple[str, SubmissionWindowOutput]]:
    """Return (tenant_id, window) pairs for tenants missing a ready/submitted run.

    For each submission window that is currently open on `today`, identifies
    which tenants (enumerated from dora_register_entries) have no submission run
    in 'ready' or 'submitted' status for that window.

    Uses `today` if provided; otherwise falls back to datetime.date.today().

    PRIVILEGED CONNECTION REQUIRED: conn must be an admin-level database connection
    that bypasses Row-Level Security. A tenant-scoped RLS connection will cause
    tenant enumeration to return zero rows silently, which this function detects
    and rejects by raising SchedulerAdminConnectionRequiredError.

    Returns [] when no windows are currently open — this is normal, not an error.
    Raises SchedulerAdminConnectionRequiredError when open windows exist but
    zero tenants are visible (indicating an RLS-restricted connection).
    """
    reference_date = today if today is not None else datetime.date.today()
    open_windows = _fetch_open_windows(conn, reference_date)
    if not open_windows:
        return []
    all_tenants = _fetch_all_active_tenants(conn)
    _guard_cross_tenant_visibility(open_windows, all_tenants)
    result: list[tuple[str, SubmissionWindowOutput]] = []
    for window in open_windows:
        missing = _find_missing_tenants_for_window(conn, window, all_tenants)
        for tenant_id in sorted(missing):
            result.append((tenant_id, window))
    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _guard_cross_tenant_visibility(
    open_windows: list[SubmissionWindowOutput], all_tenants: set[str]
) -> None:
    """Raise SchedulerAdminConnectionRequiredError if tenant enumeration looks RLS-restricted.

    If at least one submission window is open but zero tenants are visible from
    dora_register_entries, the connection is likely restricted by Row-Level Security
    and silently hiding all rows. Returning an empty result in that case would be
    unsafe because callers would interpret it as 'all tenants are compliant'.

    This is a conservative heuristic: it may also fire in a genuinely empty
    environment (no tenants have ever created register entries). That is an
    acceptable trade-off for a compliance scheduling workflow.
    """
    if open_windows and not all_tenants:
        raise SchedulerAdminConnectionRequiredError(
            "find_tenants_missing_submission requires an admin-level (privileged) database "
            "connection with cross-tenant visibility. Open submission windows exist, but "
            "tenant enumeration from dora_register_entries returned zero results. A "
            "tenant-scoped RLS connection may be hiding all rows. Returning an empty list "
            "in this case would be unsafe to interpret as 'all tenants compliant'. "
            "Pass a connection that bypasses Row-Level Security."
        )


def _fetch_open_windows(conn, reference_date: datetime.date) -> list[SubmissionWindowOutput]:
    rows = conn.execute(_SELECT_OPEN_WINDOWS, {"today": reference_date}).fetchall()
    return [_window_row_to_output(row) for row in rows]


def _fetch_all_active_tenants(conn) -> set[str]:
    """Return a set of all tenant_ids that have at least one register entry.

    Requires an admin-level connection to bypass RLS on dora_register_entries.
    If the connection is RLS-restricted, this will return an empty set, which
    _guard_cross_tenant_visibility will detect and convert to a hard failure.
    """
    rows = conn.execute(_SELECT_ALL_ACTIVE_TENANTS, {}).fetchall()
    return {str(row[0]) for row in rows}


def _find_missing_tenants_for_window(
    conn, window: SubmissionWindowOutput, all_tenants: set[str]
) -> set[str]:
    """Return the set of tenant_ids that have no completed run for this window.

    Fetches tenant_ids with a ready/submitted run for (window.id, window.reporting_year),
    then returns the set difference from all_tenants.
    """
    rows = conn.execute(
        _SELECT_TENANTS_WITH_COMPLETED_RUNS,
        {"window_id": window.id, "reporting_year": window.reporting_year},
    ).fetchall()
    completed_tenants = {str(row[0]) for row in rows}
    return all_tenants - completed_tenants
