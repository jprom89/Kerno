"""dora_roi_scheduler_service.py — Service-layer logic for the DORA RoI submission scheduler.

What:  Provides business logic for the DORA RoI nightly scheduler tick — querying
       open submission windows so the scheduler can drive per-tenant submission run
       upserts without duplicating the open-window filter SQL.

Why:   Doc 17B item 8 found that the SQL constant _SELECT_OPEN_WINDOWS was duplicated
       between dora_roi_submission_service and this module. This file now imports
       _SELECT_OPEN_WINDOWS from the authoritative source (the submission service)
       and contains no local copy of that SQL. Any future change to the window filter
       propagates to both callers automatically.

How to run or test:
    pytest tests/unit/services/test_dora_roi_scheduler_service.py -v
"""

from datetime import date

from src.services.dora_roi_submission_service import (
    SubmissionWindowOutput,
    _SELECT_OPEN_WINDOWS,
    _window_row_to_output,
)


def list_open_submission_windows(conn) -> list[SubmissionWindowOutput]:
    """Return all submission windows currently open for filing.

    Uses the shared _SELECT_OPEN_WINDOWS constant imported from
    dora_roi_submission_service rather than defining a local copy,
    ensuring the scheduler and submission service always apply the same
    open-window filter (Doc 17B item 8).
    """
    rows = conn.execute(_SELECT_OPEN_WINDOWS, {"today": date.today()}).fetchall()
    return [_window_row_to_output(row) for row in rows]
