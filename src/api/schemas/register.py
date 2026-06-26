"""Pydantic request and response models for the DORA register entry endpoints.
Field names match the service-layer dataclasses exactly — no renaming."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class RegisterEntryRequest(BaseModel):
    provider_name: str
    service_name: str
    provider_type: str
    criticality_level: str
    business_function: str
    data_types: list[str]
    countries_supported: list[str]
    contract_start_date: date | None = None
    contract_end_date: date | None = None
    exit_strategy_summary: str | None = None
    is_active: bool = True
    source_record_id: str | None = None


class RegisterEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    register_entry_id: str
    tenant_id: str
    provider_name: str
    service_name: str
    provider_type: str
    criticality_level: str
    business_function: str
    data_types: list[str]
    countries_supported: list[str]
    contract_start_date: date | None
    contract_end_date: date | None
    exit_strategy_summary: str | None
    is_active: bool
    source_record_id: str | None
    created_at: datetime
    updated_at: datetime


class ReportingWindowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reporting_window_id: str
    authority_code: str
    authority_name: str
    member_state: str
    reporting_year: int
    submission_open_date: date
    submission_close_date: date
    notes: str | None
    created_at: datetime
