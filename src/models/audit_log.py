"""ORM model for the audit_log table — the tamper-evident, append-only audit ledger (KER-107).

Each row is one immutable audit event whose entry_hash chains to the previous entry's hash,
so edits, deletions, and reordering are detectable by src/services/audit_log.verify_audit_chain();
the append-only trigger from migration 016 blocks UPDATE and DELETE at the database level.
Rows are written exclusively through src/services/audit_log.append_audit_entry().

Why:   compliance auditors must trust that history cannot be silently rewritten;
       the hash chain plus database trigger make tampering detectable and blocked.
How:   pytest tests/unit/services/test_audit_log.py tests/integration/test_ker107_audit_ledger.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class AuditLog(Base):
    """A single immutable, hash-chained audit ledger entry.

    Deliberately denormalised: every entry is readable in isolation without joins.
    actor_id is NULL for system-generated events (e.g. AI recommendation writes);
    control_id is NULL when the event does not target a specific control.
    previous_hash and entry_hash are computed by the audit service — never set
    these fields by hand.
    """

    __tablename__ = "audit_log"

    # A valid chain is linear: each previous_hash appears once per tenant.
    # This constraint turns any concurrent chain fork into a database error.
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "previous_hash", name="uq_audit_log_tenant_previous_hash"
        ),
    )

    # Surrogate primary key. Included in the hashed payload, so changing it
    # counts as tampering.
    id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # Which company's ledger this entry belongs to. Chains are per tenant.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Who performed the action. NULL means the event was system-generated,
    # not a human decision.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=True,
    )

    # The actor's role at the time of the event, snapshotted for audit stability
    # ("system" for machine-generated events).
    actor_role: Mapped[str] = mapped_column(String, nullable=False)

    # What happened: "approve", "edit", "reject", "recommendation_generated", ...
    action_type: Mapped[str] = mapped_column(String, nullable=False)

    # The kind of object the event acted on: "override", "system_event", ...
    object_type: Mapped[str] = mapped_column(String, nullable=False)

    # The identifier of that object (e.g. the override_id), stored as text so
    # the ledger can reference objects whose keys are not UUIDs.
    object_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # The compliance control the event relates to. NULL when the event does not
    # target a control directly.
    control_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Object state before the event. For overrides this is the AI's recommended
    # control; NULL when no meaningful prior state exists.
    before_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Object state after the event. For overrides this is the reviewer's decided
    # control plus their anonymised justification.
    after_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # When the event was recorded. Generated in Python (not by the database
    # clock) because the timestamp is part of the hashed payload and must be
    # known before the row is written.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # entry_hash of the previous entry in this tenant's chain, or the genesis
    # constant for the first entry (config.constants.AUDIT_GENESIS_HASH).
    previous_hash: Mapped[str] = mapped_column(String, nullable=False)

    # SHA-256 of previous_hash + this entry's canonical payload.
    entry_hash: Mapped[str] = mapped_column(String, nullable=False)

    # Database-assigned insertion order used to walk the chain. Not part of the
    # hash (assigned after hashing); chain links protect ordering integrity.
    # The counter is global across tenants, so gaps in one tenant's values
    # reveal other tenants' activity volume — never expose it raw to tenants.
    sequence_number: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        server_default=text("nextval('audit_log_sequence_number_seq')"),
    )

    def __repr__(self) -> str:
        """Short summary for logs — identifiers and action only, no sensitive content."""
        return (
            f"<AuditLog id={self.id!s} "
            f"object_type={self.object_type} "
            f"action_type={self.action_type}>"
        )
