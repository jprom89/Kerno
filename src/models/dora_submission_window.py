"""ORM model for dora_submission_windows: global reference data for competent-authority
filing windows; no tenant scope, no RLS."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models import Base


class DORASubmissionWindow(Base):
    """Competent-authority submission window for one reporting year.

    Global reference data — no tenant_id column, no Row-Level Security.
    Any authenticated session may read all rows. Rows are created by platform
    administrators via the migration seed process, not by tenant actions.

    The composite unique constraint on (authority_code, reporting_year,
    register_reference_date) prevents duplicate window definitions for the
    same authority, year, and reference snapshot date.
    """

    __tablename__ = "dora_submission_windows"

    __table_args__ = (
        UniqueConstraint(
            "authority_code",
            "reporting_year",
            "register_reference_date",
            name="uq_submission_window_authority_year_ref",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True
    )
    authority_code: Mapped[str] = mapped_column(String(32), nullable=False)
    reporting_year: Mapped[int] = mapped_column(Integer, nullable=False)
    register_reference_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_open_date: Mapped[date] = mapped_column(Date, nullable=False)
    window_close_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
