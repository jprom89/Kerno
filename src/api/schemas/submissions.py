"""Pydantic request and response models for the DORA submission run endpoints.
Field names match the service-layer dataclasses exactly — no renaming.

Why:   request/response contracts live apart from routing so the API surface
       is reviewable in one place.
How:   pytest tests/unit/api/test_submissions.py -v
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class SubmissionRunRequest(BaseModel):
    submission_window_id: str


class SubmissionRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class SubmissionWindowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    authority_code: str
    reporting_year: int
    register_reference_date: date
    window_open_date: date
    window_close_date: date
    created_at: datetime
    updated_at: datetime
