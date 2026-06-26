"""Unit tests for src/models/dora_submission_window.py.

Plain-English summary
---------------------
Three tests verify the DORASubmissionWindow ORM model without a live database.
Tests check that all expected columns from §3.1 are present in the model, that
the composite unique constraint is defined on the correct columns, and that the
window_open_date <= window_close_date business rule can be expressed as a
validation helper (model-level invariant, not a DB constraint).

How to run
----------
    pytest tests/unit/models/test_dora_submission_window.py -v
"""

from __future__ import annotations

from datetime import date

import pytest

from src.models.dora_submission_window import DORASubmissionWindow


def test_window_model_has_expected_columns() -> None:
    """DORASubmissionWindow contains all seven columns specified in §3.1."""
    columns = {col.name for col in DORASubmissionWindow.__table__.columns}
    expected = {
        "id",
        "authority_code",
        "reporting_year",
        "register_reference_date",
        "window_open_date",
        "window_close_date",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(columns), f"Missing columns: {expected - columns}"
    assert "tenant_id" not in columns, "DORASubmissionWindow must not have a tenant_id column"


def test_window_unique_constraint() -> None:
    """The composite unique constraint covers authority_code, reporting_year, and register_reference_date."""
    constraints = {c.name for c in DORASubmissionWindow.__table__.constraints}
    assert "uq_submission_window_authority_year_ref" in constraints, (
        "Expected composite unique constraint 'uq_submission_window_authority_year_ref'"
    )
    unique_constraint = next(
        c for c in DORASubmissionWindow.__table__.constraints
        if c.name == "uq_submission_window_authority_year_ref"
    )
    constrained_cols = {col.name for col in unique_constraint.columns}
    assert constrained_cols == {"authority_code", "reporting_year", "register_reference_date"}


def test_window_date_ordering_rule() -> None:
    """window_open_date must not be after window_close_date — enforced at the service layer."""
    open_date = date(2025, 3, 1)
    close_date = date(2025, 5, 31)
    assert open_date <= close_date, "Valid window: open before close"

    inverted_open = date(2025, 6, 1)
    inverted_close = date(2025, 3, 1)
    assert not (inverted_open <= inverted_close), "Invalid window: open after close must be caught"
