"""ORM model for dora_submission_runs: one row per tenant submission attempt for a given
authority window, tracking status from draft through ready to submitted."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models import Base

# ---------------------------------------------------------------------------
# Status constants — used by the service layer and tests
# ---------------------------------------------------------------------------

SUBMISSION_STATUS_DRAFT: str = "draft"
SUBMISSION_STATUS_READY: str = "ready"
SUBMISSION_STATUS_SUBMITTED: str = "submitted"
SUBMISSION_STATUS_FAILED: str = "failed"


class DORASubmissionRun(Base):
    """Tenant-scoped DORA submission run (RLS enforced via tenant_id column).

    Migration 013 adds UNIQUE (submission_window_id, tenant_id) — one run per
    (tenant, window) slot. submitted_at remains NULL until an authority portal
    marks the run as submitted; build_and_record_submission deliberately leaves it as NULL.
    """

    __tablename__ = "dora_submission_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    submission_window_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    reporting_year: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    validation_overall_status: Mapped[str] = mapped_column(String(8), nullable=False)
    validation_issue_count: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submission_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
