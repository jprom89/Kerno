"""The human override record — one row for every time a compliance engineer
corrects an AI-generated control recommendation (KER-106).

Plain-English summary
---------------------
When the AI maps a piece of evidence to a compliance control and a human decides
the mapping is wrong, they either approve it (confirming the AI was right),
reject it (the AI was completely wrong), or edit it (the AI was close but
imprecise). Each of those decisions is captured here as an override record.

Two fields drive how much each override counts: ``reviewer_role`` (who made the
decision) and ``reviewer_confidence_weight`` (how much their decision is trusted
in the nightly recalculation). A vCISO's override counts for 1.0; an internal
admin's counts for 0.5. These weights come from config/constants.py and must
never be hard-coded here. (LEARNING_PIPELINE_SPEC.md Section 5.2.)
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, String, text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base

# The three actions a reviewer can take. Stored as a database-level ENUM so
# invalid values are rejected by the database, not just the application.
ACTION_TYPES = ("approve", "edit", "reject")

# The two recognised reviewer roles. Values must stay in sync with the weight
# assignments in override_service.py and the constants in config/constants.py.
REVIEWER_ROLES = ("vciso", "fciso", "internal_admin")


class Override(Base):
    """A single human correction to an AI-generated control mapping.

    Written once when the reviewer submits their decision and never updated.
    The nightly batch reads all overrides written since the last run to
    recalculate the tenant's retrieval bias vector. (KER-106.)
    """

    __tablename__ = "overrides"

    # Unique identifier for this override decision.
    override_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # The company this override belongs to. Never accepted from HTTP input.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # The user who made the override decision.
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
    )

    # The reviewer's role, which determines their confidence weight.
    reviewer_role: Mapped[str] = mapped_column(
        Enum(*REVIEWER_ROLES, name="reviewer_role_enum"),
        nullable=False,
    )

    # What the reviewer did: confirmed the AI, changed the mapping, or rejected it.
    action_type: Mapped[str] = mapped_column(
        Enum(*ACTION_TYPES, name="action_type_enum"),
        nullable=False,
    )

    # The control the AI originally recommended.
    original_control_id: Mapped[str] = mapped_column(String, nullable=False)

    # The control the reviewer chose instead — present for edit/reject, None for approve.
    corrected_control_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # The reviewer's free-text explanation of their decision, stored anonymised.
    # override_service.py strips internal identifiers before writing this column.
    justification_text: Mapped[str | None] = mapped_column(String, nullable=True)

    # How much weight the nightly batch gives this reviewer's decision.
    # Set by override_service.py based on reviewer_role; never from user input.
    reviewer_confidence_weight: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )

    # When the reviewer submitted this decision.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    def __repr__(self) -> str:
        """Short summary for logs — contains IDs but no sensitive content."""
        return (
            f"<Override override_id={self.override_id!s} "
            f"action_type={self.action_type} "
            f"reviewer_role={self.reviewer_role}>"
        )
