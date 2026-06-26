"""recommendation.py — ORM model for the Recommendation table.

What:  Defines the SQLAlchemy ORM model for the recommendations table,
       which stores one explainable compliance recommendation per
       (tenant_id, control_id) pair at any given time.

Why:   Document 13 (KER-105) requires a persisted, auditable record of
       each recommendation — including the evidence it relied on, its
       confidence score, a plain-language rationale, and a full snapshot
       of the inputs so the recommendation can be reproduced without
       querying other tables (AC-4).

How to run or test:
    pytest tests/unit/services/test_recommendation_service.py -v
    pytest tests/unit/services/test_scoring.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base

# ---------------------------------------------------------------------------
# Status constants — what a recommendation concludes about control coverage
# ---------------------------------------------------------------------------

STATUS_MET: str = "met"
STATUS_PARTIAL: str = "partial"
STATUS_GAP: str = "gap"

# ---------------------------------------------------------------------------
# Confidence level constants — how certain the scoring engine is
# ---------------------------------------------------------------------------

CONFIDENCE_HIGH: str = "high"
CONFIDENCE_MEDIUM: str = "medium"
CONFIDENCE_LOW: str = "low"


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class Recommendation(Base):
    """Persisted recommendation record for a single (tenant, control) pair.

    At most one row per (tenant_id, control_id) has is_superseded=False at any
    time. Older rows are marked is_superseded=True when a new recommendation is
    generated for the same pair (§4.2 supersede pattern).
    """

    __tablename__ = "recommendations"

    recommendation_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    control_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence_level: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    gaps: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    requires_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_superseded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
