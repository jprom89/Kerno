"""The immutable override audit log — one row per human override event (KER-107).

Plain-English summary
---------------------
Every time a compliance engineer overrides an AI recommendation, a permanent,
unmodifiable record is written here. Unlike the override record (which tracks
what happened), the audit log exists for accountability and regulatory evidence:
who made a change, when, and exactly what they changed.

"Immutable" means two things in practice:
  1. The application never issues UPDATE or DELETE on this table.
  2. The database table should be granted INSERT-only permissions in production
     (see migrations/004_create_audit_log_table.py).

Compliance auditors and investors may ask to see this log. Every field must be
self-explanatory in isolation — no joined lookups required to understand an entry.
(KER-107, LEARNING_PIPELINE_SPEC.md Section 4.1.)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class AuditLog(Base):
    """A single immutable record of a human override event.

    Written once by ``override_service.capture_override()`` immediately after the
    override record is persisted. Must never be updated or deleted. Every field
    is nullable=False except ``corrected_control_id``, which is genuinely absent
    when the reviewer approves the AI's mapping unchanged.
    """

    __tablename__ = "audit_log"

    # Surrogate primary key for this log entry.
    id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # The override event this entry records. Denormalised intentionally: the
    # audit log must be readable without joining to the overrides table.
    override_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Which company's data was involved.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Who made the override decision.
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
    )

    # Their role at the time of the decision (denormalised for audit stability —
    # role changes after the fact must not silently alter the historical record).
    reviewer_role: Mapped[str] = mapped_column(String, nullable=False)

    # What they did: "approve", "edit", or "reject".
    action_type: Mapped[str] = mapped_column(String, nullable=False)

    # The control the AI had recommended before the human intervened.
    original_control_id: Mapped[str] = mapped_column(String, nullable=False)

    # The control the human chose instead. Only present for edit/reject.
    corrected_control_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # The reviewer's anonymised justification text, copied from the override record.
    justification_text: Mapped[str | None] = mapped_column(String, nullable=True)

    # Exact moment the override was recorded. Set by the database clock.
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    def __repr__(self) -> str:
        """Short summary for logs — contains IDs and action but no sensitive data."""
        return (
            f"<AuditLog id={self.id!s} "
            f"override_id={self.override_id!s} "
            f"action_type={self.action_type}>"
        )
