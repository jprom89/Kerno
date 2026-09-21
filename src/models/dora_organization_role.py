"""dora_organization_role.py — ORM model for the roles an organisation plays.

What:  Defines the SQLAlchemy model for the dora_organization_roles table and
       the exact initial role vocabulary. One row says that one organisation
       acts as a financial entity, or as an ICT provider, within one tenant's
       DORA graph.
Why:   DORA_MODEL_V2.md §5.3. A group bank can be both a regulated financial
       entity and an intra-group ICT provider; modelling that as two
       organisation rows would duplicate its identity. A role is an
       organisation-level fact. Direct provider, subcontractor, rank, signatory
       and consumer are relationship-level facts and are deliberately not roles
       (§9, §16, §20) — the database CHECK admits exactly the two strings below.
How:   pytest tests/unit/models/test_dora_organization_models.py -v
       Live behaviour: tests/security/test_dora_organization_isolation.py
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
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models import Base

# ---------------------------------------------------------------------------
# role_type constants (DORA_MODEL_V2.md §5.3) — exactly these two, no more
# ---------------------------------------------------------------------------

ROLE_TYPE_FINANCIAL_ENTITY: str = "financial_entity"
ROLE_TYPE_ICT_PROVIDER: str = "ict_provider"
ALLOWED_ROLE_TYPES: frozenset[str] = frozenset(
    {ROLE_TYPE_FINANCIAL_ENTITY, ROLE_TYPE_ICT_PROVIDER}
)


class DORAOrganizationRole(Base):
    """One role assignment on one organisation, owned by one tenant.

    Composite foreign key to the organisation's (tenant_id, organization_id)
    candidate key, so the role cannot belong to a different tenant than the
    organisation. An organisation holds each role at most once — the UNIQUE on
    (tenant_id, organization_id, role_type) — and may hold both at once.
    """

    __tablename__ = "dora_organization_roles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "organization_id"],
            ["dora_organizations.tenant_id", "dora_organizations.organization_id"],
            name="fk_dora_organization_roles_organization",
        ),
        UniqueConstraint(
            "tenant_id",
            "organization_id",
            "role_type",
            name="uq_dora_organization_roles_tenant_org_role",
        ),
        CheckConstraint(
            "role_type IN ('financial_entity', 'ict_provider')",
            name="ck_dora_organization_roles_role_type",
        ),
    )

    organization_role_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", name="dora_organization_roles_tenant_id_fkey"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    role_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # No database trigger maintains updated_at (migration 025 creates none);
    # the writer sets it explicitly. An ORM-side onupdate hook would document
    # behaviour the database does not provide, so none is declared.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
