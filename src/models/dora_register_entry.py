"""dora_register_entry.py — ORM model for the DORA Register of Information entries.

What:  Defines the SQLAlchemy ORM model for the dora_register_entries table and
       the module-level constants for criticality_level and provider_type allowed
       values. Each row represents one live ICT third-party or ICT service
       relationship record belonging to a tenant.

Why:   DORA Article 28 requires financial entities to maintain a Register of
       Information (RoI) for ICT third-party service providers. Document 14
       (KER-106 part 1) establishes this as a continuously-maintained live
       register — not an annual export artifact — so it can be queried, filtered,
       and later exported into ESA xBRL-CSV format (Document 15).

How to run or test:
    pytest tests/unit/models/test_dora_register_entry.py -v
    pytest tests/unit/services/test_dora_roi_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base

# ---------------------------------------------------------------------------
# criticality_level constants (§3.2)
# ---------------------------------------------------------------------------

CRITICALITY_CRITICAL: str = "critical"
CRITICALITY_HIGH: str = "high"
CRITICALITY_STANDARD: str = "standard"

# ---------------------------------------------------------------------------
# provider_type constants (§3.3)
# ---------------------------------------------------------------------------

PROVIDER_TYPE_CLOUD: str = "cloud"
PROVIDER_TYPE_SOFTWARE: str = "software"
PROVIDER_TYPE_MANAGED_SERVICE: str = "managed_service"
PROVIDER_TYPE_TELECOM: str = "telecom"
PROVIDER_TYPE_OTHER: str = "other"


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class DORARegisterEntry(Base):
    """Live RoI record for one ICT third-party or ICT service relationship.

    Tenant-scoped via tenant_id. Row-Level Security is enforced by migration
    011; only rows whose tenant_id matches the active session variable are
    visible. At most one row should represent each distinct (tenant, provider,
    service) combination, though uniqueness is not enforced at the DB level
    to allow historical corrections.
    """

    __tablename__ = "dora_register_entries"

    register_entry_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    provider_name: Mapped[str] = mapped_column(Text, nullable=False)
    service_name: Mapped[str] = mapped_column(Text, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    criticality_level: Mapped[str] = mapped_column(String(16), nullable=False)
    business_function: Mapped[str] = mapped_column(Text, nullable=False)
    data_types: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    countries_supported: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    contract_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_strategy_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_record_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
