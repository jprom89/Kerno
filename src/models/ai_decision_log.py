"""The AiDecisionLog record — one retained row per AI mapping decision (KER-203).

Plain-English summary
---------------------
Every time the AI produces a compliance recommendation, one row lands here in
the same transaction: which control was mapped, which evidence records the
model cited, a SHA-256 fingerprint of the exact inputs, what the model decided
(status + confidence), a short extract of its reasoning, and which model
version produced it. Regulators (EU AI Act Articles 12/19/26) and enterprise
buyers can then reconstruct what the machine decided and why, for at least
AI_DECISION_LOG_RETENTION_DAYS (180 days).

This is NOT the KER-107 human-decision ledger: it is append-only in practice
but not hash-chained, and it is pruned past the retention window. GDPR
alignment: only input_snapshot_hash is stored — never the raw snapshot — so no
personal data enters this table.

control_id and evidence_ids are TEXT refs, matching the recommendations table
this log describes (column-type correction recorded in migration 020's
docstring — the §13 UUID types would have broken the same-transaction insert).

How to run or test
------------------
Model files have no executable logic of their own; they are tested through the
services that use them. Syntax-check with:

    python -c "from src.models.ai_decision_log import AiDecisionLog; print('OK')"

Unit tests live in tests/unit/services/test_ai_decision_log.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Float, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, TIMESTAMP, UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class AiDecisionLog(Base):
    """One retained record of a single AI mapping decision.

    Rows are written by ai_decision_log_service.emit_decision_log inside the
    same transaction as the recommendation they describe, queried through
    GET /api/v1/ai-decisions, and pruned past the retention window by
    src/scheduler/prune_ai_decision_log.py. All access runs under tenant
    context — the table is FORCE row-level secured (migration 020).
    """

    __tablename__ = "ai_decision_log"

    # Kerno-generated identity for this decision record. Never bound from HTTP input.
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # The tenant the decision belongs to. Resolved from the session, never the request.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
    )

    # TEXT control ref matching recommendations.control_id (e.g. "ctrl-001").
    control_id: Mapped[str] = mapped_column(Text, nullable=False)

    # The evidence record refs the model cited, matching recommendations.evidence_ids.
    evidence_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)

    # SHA-256 hex digest of the canonical JSON of the mapping inputs — the
    # verifiable fingerprint of what the model saw, never the inputs themselves.
    input_snapshot_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # The model's mapping outcome: met, partial, or gap.
    output_status: Mapped[str] = mapped_column(Text, nullable=False)

    # The model's self-reported confidence, 0.0–1.0.
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Short extract of the model's reasoning (the recommendation stores it in full).
    rationale_extract: Mapped[str] = mapped_column(Text, nullable=False)

    # Which model produced the decision (KERNO_LLM_MODEL at generation time).
    model_version: Mapped[str] = mapped_column(String, nullable=False)

    # When the decision was recorded. Set by the database clock; the prune job
    # compares this against the retention window.
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    def __repr__(self) -> str:
        """Short, safe summary for logs — identifiers and outcome only."""
        return (
            f"<AiDecisionLog correlation_id={self.correlation_id!s} "
            f"control_id={self.control_id} status={self.output_status}>"
        )
