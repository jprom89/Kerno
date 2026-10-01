"""dora_contract_relationship.py — ORM model for the recorded hierarchy between two contracts.

What:  Defines the SQLAlchemy model for the dora_contract_relationships table
       and its one relationship type. One row says that, within one tenant,
       one contract (the child) is recorded as having another contract (the
       parent) as its overarching contractual arrangement — "Hosting Order
       2026 -> Master Services Agreement".
Why:   DORA_MODEL_V2.md §7.3: the single canonical representation of contract
       hierarchy, kept out of dora_contracts so there is never both a parent
       column and a link graph. A contract with no recorded parent has no
       parent RECORDED; that does not prove it is standalone, and nothing here
       derives a regulatory field (B_02.01.0020 or B_02.01.0030) from the
       graph. Rows are history: the hierarchy service never changes
       endpoints or type, never reactivates an inactive row and never
       deletes one. No database trigger enforces that (migration 027), so
       the table owner can still do all three with raw SQL.
How:   pytest tests/unit/models/test_dora_contract_relationship_model.py -v
       Live behaviour: tests/integration/test_dora_v2_002b_hierarchy.py
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models import Base

# The child's overarching contractual arrangement is the parent. The only
# type this slice supports; another needs its own use case first (§7.3).
RELATIONSHIP_TYPE_OVERARCHING: str = "overarching"
ALLOWED_RELATIONSHIP_TYPES: frozenset[str] = frozenset({RELATIONSHIP_TYPE_OVERARCHING})


class DORAContractRelationship(Base):
    """One recorded child -> parent link between two contracts of one tenant.

    Tenant-scoped via tenant_id under ENABLE + FORCE Row-Level Security
    (migration 027). Both endpoints carry a composite (tenant_id, contract)
    foreign key to the contracts' candidate key, so a link can never join two
    tenants' contracts. The partial unique index allows at most one ACTIVE
    link per child and type — Kerno's modelling constraint that a child has
    at most one active parent — while inactive history accumulates freely.
    The CHECK forbids a direct self-link only; longer cycles are refused by
    the hierarchy service, which no database constraint backs.
    """

    __tablename__ = "dora_contract_relationships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "child_contract_id"],
            ["dora_contracts.tenant_id", "dora_contracts.contract_id"],
            name="fk_dora_contract_relationships_child",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "parent_contract_id"],
            ["dora_contracts.tenant_id", "dora_contracts.contract_id"],
            name="fk_dora_contract_relationships_parent",
        ),
        CheckConstraint(
            "child_contract_id <> parent_contract_id",
            name="ck_dora_contract_relationships_not_self",
        ),
        CheckConstraint(
            "relationship_type = 'overarching'",
            name="ck_dora_contract_relationships_type",
        ),
        Index(
            "uq_dora_contract_relationships_one_active_parent",
            "tenant_id",
            "child_contract_id",
            "relationship_type",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        # A child's full history, inactive rows included — the partial index
        # above cannot serve it. Also serves the child foreign key.
        Index(
            "ix_dora_contract_relationships_tenant_child",
            "tenant_id",
            "child_contract_id",
        ),
        # A parent's children; also serves the parent foreign key.
        Index(
            "ix_dora_contract_relationships_tenant_parent",
            "tenant_id",
            "parent_contract_id",
        ),
    )

    contract_relationship_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", name="dora_contract_relationships_tenant_id_fkey"),
        nullable=False,
    )
    child_contract_id: Mapped[uuid.UUID] = mapped_column(PostgresUUID(as_uuid=True), nullable=False)
    parent_contract_id: Mapped[uuid.UUID] = mapped_column(PostgresUUID(as_uuid=True), nullable=False)
    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # No database trigger maintains updated_at (migration 027 creates none);
    # the writer sets it explicitly, so no ORM-side onupdate hook is declared.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
