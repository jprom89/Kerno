"""dora_contract.py — ORM model for canonical DORA contracts.

What:  Defines the SQLAlchemy model for the dora_contracts table and the one
       whitespace policy that governs contract text. One row is one
       contractual arrangement as known to one tenant, identified by the
       reference the tenant uses for it.
Why:   DORA_MODEL_V2.md §7.1. A contract is reusable: several organisations
       sign it (dora_contract_parties) and later slices will hang services
       and costs off it. Its identity is its tenant-scoped reference — never a
       provider name or a display name. Hierarchy, costs, services and
       template fields are deliberately absent; a contract with no hierarchy
       link is uncollected, not "standalone".
How:   pytest tests/unit/models/test_dora_contract_models.py -v
       Live behaviour: tests/integration/test_dora_v2_002a_contracts.py
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from config.constants import CONTRACT_REFERENCE_MAX_CHARACTERS
from src.models import Base

# ---------------------------------------------------------------------------
# Whitespace policy for contract text (migration 026)
# ---------------------------------------------------------------------------

# Trimmed from both ends of contract_reference and display_name — by the
# service with str.strip(), by the database with btrim(). One explicit set on
# both sides, so what the service accepts and what the CHECK accepts cannot
# disagree. It is the set Python's str.isspace() accepts: tab, LF, VT, FF,
# CR, the four information separators, space, NEL, NBSP, OGHAM SPACE MARK,
# U+2000–U+200A, LINE and PARAGRAPH SEPARATOR, NARROW NBSP, MEDIUM
# MATHEMATICAL SPACE and IDEOGRAPHIC SPACE. Written as escapes because most
# of them are invisible in source. Interior characters are never touched.
CONTRACT_TEXT_TRIM_CHARACTERS: str = (
    "\u0009\u000a\u000b\u000c\u000d\u001c\u001d\u001e\u001f\u0020\u0085\u00a0"
    "\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
    "\u2028\u2029\u202f\u205f\u3000"
)

# The same set as the PostgreSQL escape-string literal migration 026 wrote
# into its CHECKs. Metadata only: this text is never executed by the service.
_TRIM_CHARACTERS_SQL = (
    "E'\\u0009\\u000a\\u000b\\u000c\\u000d\\u001c\\u001d\\u001e\\u001f"
    "\\u0020\\u0085\\u00a0\\u1680\\u2000\\u2001\\u2002\\u2003\\u2004\\u2005"
    "\\u2006\\u2007\\u2008\\u2009\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000'"
)


class DORAContract(Base):
    """One contractual arrangement, owned by one tenant.

    Tenant-scoped via tenant_id under ENABLE + FORCE Row-Level Security
    (migration 026). contract_reference is unique within the tenant and
    canonical at both ends; display_name is optional, canonical when present,
    and not unique. The (tenant_id, contract_id) unique constraint is the
    candidate key dora_contract_parties references with a composite foreign
    key, so a party can never belong to another tenant's contract.
    """

    __tablename__ = "dora_contracts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "contract_id", name="uq_dora_contracts_tenant_contract"),
        UniqueConstraint(
            "tenant_id", "contract_reference", name="uq_dora_contracts_tenant_reference"
        ),
        CheckConstraint(
            f"contract_reference = btrim(contract_reference, {_TRIM_CHARACTERS_SQL}) "
            "AND contract_reference <> ''",
            name="ck_dora_contracts_reference_canonical",
        ),
        CheckConstraint(
            f"char_length(contract_reference) <= {CONTRACT_REFERENCE_MAX_CHARACTERS}",
            name="ck_dora_contracts_reference_length",
        ),
        CheckConstraint(
            f"display_name IS NULL OR (display_name = btrim(display_name, {_TRIM_CHARACTERS_SQL}) "
            "AND display_name <> '')",
            name="ck_dora_contracts_display_name_canonical",
        ),
        CheckConstraint(
            "contract_start_date IS NULL OR contract_end_date IS NULL "
            "OR contract_end_date >= contract_start_date",
            name="ck_dora_contracts_date_order",
        ),
    )

    contract_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", name="dora_contracts_tenant_id_fkey"),
        nullable=False,
    )
    contract_reference: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    contract_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    contract_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
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
