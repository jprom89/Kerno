"""dora_organization_identifier.py — ORM model for organisation identifiers.

What:  Defines the SQLAlchemy model for the dora_organization_identifiers
       table. One row is one external identifier — an LEI, an EUID, a national
       registration number — attached to one organisation.
Why:   DORA_MODEL_V2.md §5.2. Identifiers are kept apart from the organisation
       master so an organisation can carry several, so an identifier can be
       added without touching the master row, and so "the same identifier must
       not name two organisations in one tenant" can be a database rule rather
       than a habit. The rule is deliberately per tenant: AWS's LEI may
       legitimately appear as a separate record in every tenant that deals with
       AWS. This is not the regulatory reference-data ticket, so identifier_type
       is free text (canonicalised to upper case), not a controlled list.
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
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models import Base


class DORAOrganizationIdentifier(Base):
    """One external identifier on one organisation, owned by one tenant.

    The foreign key is composite — (tenant_id, organization_id) references the
    organisation's (tenant_id, organization_id) candidate key — so the row's
    tenant and its organisation's tenant cannot disagree. The uniqueness grain
    is (tenant_id, identifier_type, identifier_value) over the values as
    persisted; the CHECK constraints keep those values canonical so the UNIQUE
    is meaningful.
    """

    __tablename__ = "dora_organization_identifiers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "organization_id"],
            ["dora_organizations.tenant_id", "dora_organizations.organization_id"],
            name="fk_dora_organization_identifiers_organization",
        ),
        UniqueConstraint(
            "tenant_id",
            "identifier_type",
            "identifier_value",
            name="uq_dora_organization_identifiers_tenant_type_value",
        ),
        CheckConstraint(
            "identifier_type = upper(btrim(identifier_type)) AND identifier_type <> ''",
            name="ck_dora_organization_identifiers_type_canonical",
        ),
        CheckConstraint(
            "identifier_value = btrim(identifier_value) AND identifier_value <> ''",
            name="ck_dora_organization_identifiers_value_canonical",
        ),
        # The UNIQUE above does not contain organization_id, so it cannot serve
        # the (tenant_id, organization_id) pair that the composite FK check and
        # "list one organisation's identifiers" both need (migration 025).
        Index("ix_dora_organization_identifiers_tenant_org", "tenant_id", "organization_id"),
    )

    organization_identifier_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", name="dora_organization_identifiers_tenant_id_fkey"),
        nullable=False,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    identifier_type: Mapped[str] = mapped_column(String(64), nullable=False)
    identifier_value: Mapped[str] = mapped_column(Text, nullable=False)
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
