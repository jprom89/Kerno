"""dora_reporting_window.py — ORM model for DORA authority reporting windows.

What:  Defines the SQLAlchemy ORM model for the dora_reporting_windows table.
       Each row stores the open/close dates and metadata for one competent
       authority's annual submission window.

Why:   Financial entities must submit their DORA Register of Information to the
       relevant competent authority within a defined annual window. Kerno stores
       these windows as global reference data (no tenant_id) so the service layer
       can present upcoming deadlines to any tenant without duplication.
       Document 14 (KER-106 part 1) creates the foundation; Document 16 will
       wire these windows into the submission workflow.

How to run or test:
    pytest tests/unit/services/test_dora_roi_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class DORAReportingWindow(Base):
    """Reporting window for one competent authority in one reporting year.

    Global reference data — not tenant-scoped, no RLS. Any tenant can read
    all rows. Rows are created by platform administrators, not by tenant actions.
    """

    __tablename__ = "dora_reporting_windows"

    reporting_window_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True
    )
    authority_code: Mapped[str] = mapped_column(String(32), nullable=False)
    authority_name: Mapped[str] = mapped_column(Text, nullable=False)
    member_state: Mapped[str] = mapped_column(Text, nullable=False)
    reporting_year: Mapped[int] = mapped_column(Integer, nullable=False)
    submission_open_date: Mapped[date] = mapped_column(Date, nullable=False)
    submission_close_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
