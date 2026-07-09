"""The User record — one login identity per person within a tenant (KER-202).

Plain-English summary
---------------------
Sprint 1 authenticated at the tenant level: one email/password per company. This
model introduces real per-user identity: each row is one human who can log in,
belonging to exactly one tenant, carrying a single RBAC role that governs what
they may do. The ``user_id`` becomes the verified actor recorded on overrides and
in the audit ledger, replacing the tenant-principal placeholder.

Two security rules mirror the tenant model (CLAUDE.md Section 3):
  * ``tenant_id`` is never set from HTTP request input — it is resolved from the
    authenticated session, and every users query runs under tenant context (RLS).
  * ``role`` holds an RbacRole value (config/constants.py); it is set at
    provisioning time and read from the verified JWT, never from a request body.

How to run or test
------------------
Model files have no executable logic of their own; they are tested through the
services that use them. Syntax-check with:

    python -c "from src.models.user import User; print('User model OK')"

Unit tests for the auth path live in tests/unit/services/test_user_auth.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class User(Base):
    """A single login identity belonging to one tenant.

    Email is unique per tenant (two tenants may each have an admin@… address).
    The password is stored only as a scrypt hash. role is one of the
    config.constants.RbacRole values and drives RBAC enforcement.
    """

    __tablename__ = "users"

    # Server-generated immutable identity for this user. Never bound from HTTP input.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # The tenant this user belongs to. Resolved from the session, never the request.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Login email, unique within a tenant (see the UNIQUE (tenant_id, email)
    # constraint in migration 019).
    email: Mapped[str] = mapped_column(String, nullable=False)

    # scrypt hash produced by auth_service.hash_password; never a plaintext password.
    password_hash: Mapped[str] = mapped_column(String, nullable=False)

    # The user's RBAC role — a config.constants.RbacRole value. Stored as text so
    # the six-role vocabulary can evolve without a database enum migration.
    role: Mapped[str] = mapped_column(String, nullable=False)

    # Whether the account may log in. New users start active.
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    # When the user was provisioned. Set by the database clock.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    def __repr__(self) -> str:
        """Short, safe summary for logs — identifiers and role only, no credentials."""
        return f"<User user_id={self.user_id!s} role={self.role}>"
