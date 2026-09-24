"""dora_organization.py — ORM model for canonical DORA legal organisations.

What:  Defines the SQLAlchemy model for the dora_organizations table. One row
       is one legal organisation as known to one tenant — a bank, a subsidiary,
       an intra-group IT provider, or an external ICT provider such as AWS.
Why:   DORA_MODEL_V2.md §5.1. A legal organisation must exist once per tenant
       and be reusable across many contracts and arrangements, and it must be a
       different object from the Kerno tenant (the customer and security
       boundary) and from any role it happens to play. The roles live in
       dora_organization_roles; the identifiers in
       dora_organization_identifiers. Nothing regulatory-template-shaped lives
       here.
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
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models import Base


class DORAOrganization(Base):
    """One legal organisation, owned by one tenant.

    Tenant-scoped via tenant_id under ENABLE + FORCE Row-Level Security
    (migration 025). legal_name is deliberately not unique: near-duplicate
    names are distinct rows until a human says otherwise. The
    (tenant_id, organization_id) unique constraint is the candidate key the
    identifier and role tables reference with a composite foreign key, so a
    child row can never point at another tenant's organisation.
    """

    __tablename__ = "dora_organizations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "organization_id", name="uq_dora_organizations_tenant_org"
        ),
        CheckConstraint(
            "legal_name = btrim(legal_name) AND legal_name <> ''",
            name="ck_dora_organizations_legal_name_canonical",
        ),
        CheckConstraint(
            "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
            name="ck_dora_organizations_country_code_iso2",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", name="dora_organizations_tenant_id_fkey"),
        nullable=False,
    )
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
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
