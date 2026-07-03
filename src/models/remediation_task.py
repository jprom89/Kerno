"""ORM model for remediation_tasks — one row per Jira remediation task created for a gap (KER-110).

Tracks the link between a control, the Jira issue remediating it, and the re-review flag set
when Jira reports closure. The close-callback validates the (tenant, control, issue key)
triple against this table so an attacker cannot flag arbitrary controls for re-review.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class RemediationTask(Base):
    """One remediation task: Jira issue, assignee, SLA due date, and closure state.

    re_review_flagged_at is set (together with closed_at) when Jira reports the
    issue closed — the signal the next sync uses to re-review the control.
    """

    __tablename__ = "remediation_tasks"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # Which company this task belongs to. Never accepted from HTTP input.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # The control being remediated (catalogue UUID as text, matching the
    # recommendations and overrides tables).
    control_id: Mapped[str] = mapped_column(String, nullable=False)

    # The Jira issue created for this remediation.
    jira_issue_key: Mapped[str] = mapped_column(String, nullable=False)

    # Snapshot of the routing decision at trigger time, for audit stability.
    assignee_jira_account_id: Mapped[str] = mapped_column(String, nullable=False)

    # SLA-based due date: creation date + the routing rule's sla_days.
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    # When Jira reported the issue closed. NULL while remediation is open.
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When the control was flagged for re-review. Set together with closed_at.
    re_review_flagged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        """Short summary for logs — identifiers only."""
        return (
            f"<RemediationTask task_id={self.task_id!s} "
            f"jira_issue_key={self.jira_issue_key}>"
        )
