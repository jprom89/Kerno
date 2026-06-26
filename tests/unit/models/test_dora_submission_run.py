"""Unit tests for src/models/dora_submission_run.py.

Plain-English summary
---------------------
Three tests verify the DORASubmissionRun ORM model without a live database.
Tests check that all expected columns from §3.2 are present, that the default
status and timestamp fields match the spec, and that the tenant_id column is
present for Row-Level Security enforcement.

How to run
----------
    pytest tests/unit/models/test_dora_submission_run.py -v
"""

from __future__ import annotations

from src.models.dora_submission_run import (
    SUBMISSION_STATUS_DRAFT,
    SUBMISSION_STATUS_FAILED,
    SUBMISSION_STATUS_READY,
    SUBMISSION_STATUS_SUBMITTED,
    DORASubmissionRun,
)


def test_run_model_has_expected_columns() -> None:
    """DORASubmissionRun contains all twelve columns specified in §3.2."""
    columns = {col.name for col in DORASubmissionRun.__table__.columns}
    expected = {
        "id",
        "tenant_id",
        "submission_window_id",
        "reporting_year",
        "status",
        "validation_overall_status",
        "validation_issue_count",
        "entry_count",
        "created_at",
        "updated_at",
        "submitted_at",
        "submission_reference",
    }
    assert expected.issubset(columns), f"Missing columns: {expected - columns}"


def test_run_defaults_are_set_safely() -> None:
    """Status constants have the correct string values and submitted_at column is nullable."""
    assert SUBMISSION_STATUS_DRAFT == "draft"
    assert SUBMISSION_STATUS_READY == "ready"
    assert SUBMISSION_STATUS_SUBMITTED == "submitted"
    assert SUBMISSION_STATUS_FAILED == "failed"

    submitted_at_col = DORASubmissionRun.__table__.columns["submitted_at"]
    assert submitted_at_col.nullable is True, "submitted_at must be nullable"

    submission_ref_col = DORASubmissionRun.__table__.columns["submission_reference"]
    assert submission_ref_col.nullable is True, "submission_reference must be nullable"


def test_run_is_tenant_scoped() -> None:
    """DORASubmissionRun has a tenant_id column for Row-Level Security isolation."""
    columns = {col.name for col in DORASubmissionRun.__table__.columns}
    assert "tenant_id" in columns, "tenant_id column is required for RLS"
    tenant_col = DORASubmissionRun.__table__.columns["tenant_id"]
    assert not tenant_col.nullable, "tenant_id must not be nullable"
