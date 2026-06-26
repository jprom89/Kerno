"""The Tenant record — one row per customer organisation on the platform.

Plain-English summary
---------------------
A "tenant" is a single customer company using Kerno. Every other piece of data
in the system — embeddings, overrides, audit entries, bias vectors — is owned by
exactly one tenant and is walled off from all the others. The ``tenant_id`` on
this record is the identity that wall is built around.

Two rules about ``tenant_id`` matter for security (CLAUDE.md Section 3):

  * It is **immutable**. The database generates it once at registration
    (KER-101) and it never changes for the life of the company. This model
    actively refuses any attempt to reassign it after creation.
  * It must **never be set from HTTP request input**. The value a user could put
    in a request body is untrusted. The real tenant identity is always resolved
    from the authenticated session, never bound straight from the request. No
    API handler may populate this field from raw input.

How to run or test
------------------
Model files have no executable logic of their own; they are tested through
the services that use them. Syntax-check with:

    python -c "from src.models.tenant import Tenant; print('Tenant model OK')"

Integration tests that exercise tenant creation live in
tests/integration/test_rag_pipeline_end_to_end.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from src.exceptions import TenantContextMissingError
from src.models import Base


class Tenant(Base):
    """A single customer organisation and the root of its data-isolation domain.

    One row is created per company when it registers. The primary key is a
    server-generated UUIDv4 that becomes the immutable anchor for every
    tenant-scoped query elsewhere in the system.
    """

    __tablename__ = "tenants"

    # The immutable UUIDv4 identity of the company. PostgreSQL generates it on
    # insert (gen_random_uuid), so the application never supplies it and it must
    # never be bound from HTTP input. Reassignment is blocked below. (KER-101)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # When the company registered. Set by the database clock, not the app.
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    # Human-readable company name shown in the UI. Required.
    display_name: Mapped[str] = mapped_column(String, nullable=False)

    # Whether the company's account is currently active. New tenants start active.
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    @validates("tenant_id")
    def _enforce_tenant_id_is_immutable(self, key: str, value: uuid.UUID) -> uuid.UUID:
        """Refuse to change ``tenant_id`` once it has a value — it is immutable.

        Allows the very first assignment (when the record is created) but raises
        if anything later tries to point this row at a different company.
        """
        current = getattr(self, key, None)
        if current is not None and value != current:
            raise ValueError(
                "tenant_id is immutable and cannot be reassigned after creation."
            )
        return value

    def __repr__(self) -> str:
        """Short, safe summary for logs and test output (no sensitive data)."""
        return f"<Tenant tenant_id={self.tenant_id!s} is_active={self.is_active}>"
