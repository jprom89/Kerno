"""dora_contract_party.py — ORM model for the organisations that sign a contract.

What:  Defines the SQLAlchemy model for the dora_contract_parties table and the
       exact initial party-role vocabulary. One row says that one organisation
       signs one contract in one capacity, within one tenant.
Why:   DORA_MODEL_V2.md §7.2 and §20. A contract may have several signing
       organisations and an organisation may sign several contracts, so the
       link is its own table. Signing is not consuming: a group company may
       sign for a subsidiary that actually uses the service, so nothing here
       records, implies or creates a service consumer. Removal and
       reassignment workflows are out of scope; rows are retained.
How:   pytest tests/unit/models/test_dora_contract_models.py -v
       Live behaviour: tests/security/test_dora_contract_isolation.py
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models import Base

# ---------------------------------------------------------------------------
# party_role constants (DORA_MODEL_V2.md §7.2) — exactly these three, no more
# ---------------------------------------------------------------------------

PARTY_ROLE_RECIPIENT_SIGNATORY: str = "recipient_signatory"
PARTY_ROLE_PROVIDER_SIGNATORY: str = "provider_signatory"
PARTY_ROLE_INTRAGROUP_PROVIDER_SIGNATORY: str = "intragroup_provider_signatory"
ALLOWED_PARTY_ROLES: frozenset[str] = frozenset(
    {
        PARTY_ROLE_RECIPIENT_SIGNATORY,
        PARTY_ROLE_PROVIDER_SIGNATORY,
        PARTY_ROLE_INTRAGROUP_PROVIDER_SIGNATORY,
    }
)

# The party roles that require the organisation to already hold the
# ict_provider organisation role. The requirement is checked, never
# satisfied as a side effect. recipient_signatory has no role requirement: a
# group company signing on behalf of another entity is a valid case.
PROVIDER_PARTY_ROLES: frozenset[str] = frozenset(
    {PARTY_ROLE_PROVIDER_SIGNATORY, PARTY_ROLE_INTRAGROUP_PROVIDER_SIGNATORY}
)


class DORAContractParty(Base):
    """One signing organisation on one contract, owned by one tenant.

    Two composite foreign keys — to the contract's and to the organisation's
    (tenant_id, …) candidate keys — so the party row, its contract and its
    organisation cannot belong to different tenants. The UNIQUE on
    (tenant_id, contract_id, organization_id, party_role) records each
    capacity at most once while letting one organisation hold different
    capacities on the same contract when those are explicitly recorded.
    """

    __tablename__ = "dora_contract_parties"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "contract_id"],
            ["dora_contracts.tenant_id", "dora_contracts.contract_id"],
            name="fk_dora_contract_parties_contract",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "organization_id"],
            ["dora_organizations.tenant_id", "dora_organizations.organization_id"],
            name="fk_dora_contract_parties_organization",
        ),
        UniqueConstraint(
            "tenant_id",
            "contract_id",
            "organization_id",
            "party_role",
            name="uq_dora_contract_parties_tenant_tuple",
        ),
        CheckConstraint(
            "party_role IN ('recipient_signatory', 'provider_signatory', "
            "'intragroup_provider_signatory')",
            name="ck_dora_contract_parties_party_role",
        ),
        # Contracts signed by one organisation; the UNIQUE above leads with
        # (tenant_id, contract_id) and cannot serve it (migration 026).
        Index(
            "ix_dora_contract_parties_tenant_org_contract",
            "tenant_id",
            "organization_id",
            "contract_id",
        ),
    )

    contract_party_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", name="dora_contract_parties_tenant_id_fkey"),
        nullable=False,
    )
    contract_id: Mapped[uuid.UUID] = mapped_column(PostgresUUID(as_uuid=True), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True), nullable=False
    )
    party_role: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # No database trigger maintains updated_at (migration 026 creates none);
    # the writer sets it explicitly, so no ORM-side onupdate hook is declared.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
