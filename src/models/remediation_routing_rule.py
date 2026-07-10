"""ORM model for remediation_routing_rules — who fixes gaps in each control category (KER-110).

One row per (tenant, category) routing decision: which Jira account remediation tasks are
assigned to and how many days the SLA allows. A NULL control_category row is the tenant's
default rule, used when no category-specific rule exists.

Why:   gap remediation must land with the right owner and deadline automatically,
       or gaps sit unassigned.
How:   pytest tests/unit/services/test_remediation_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class RemediationRoutingRule(Base):
    """Routing decision for one tenant and control category (NULL = tenant default).

    Lookup order in remediation_service: exact category match first, then the
    tenant's default rule; no rule at all means remediation cannot be triggered.
    """

    __tablename__ = "remediation_routing_rules"

    rule_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # Which company this rule belongs to. Never accepted from HTTP input.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Control category this rule applies to; NULL marks the tenant's default rule.
    control_category: Mapped[str | None] = mapped_column(String, nullable=True)

    # Jira account the remediation task is assigned to.
    assignee_jira_account_id: Mapped[str] = mapped_column(String, nullable=False)

    # Due date = task creation date + this many days.
    sla_days: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    def __repr__(self) -> str:
        """Short summary for logs — no tenant-sensitive content beyond IDs."""
        return (
            f"<RemediationRoutingRule rule_id={self.rule_id!s} "
            f"category={self.control_category or 'default'}>"
        )
