"""Webhook models — a tenant's registered source systems and the ingest dedup memory (KER-205).

Plain-English summary
---------------------
``WebhookRegistration`` is one row per (tenant, source system) a customer has
connected: it holds the HMAC signing secret that authenticates that source's
deliveries. The secret is stored as plaintext because HMAC verification needs
the raw value (§13 KER-205 decision 1); the compensating controls live in the
API layer — the secret is returned exactly once at creation, never again by
any read endpoint, and is replaceable via the rotate endpoint.

``WebhookIngestDedup`` is one row per (tenant, source system, external ref)
recently received: a repeat delivery inside WEBHOOK_DEDUP_WINDOW_HOURS is
acknowledged without re-processing. The unique constraint makes the check
race-safe; re-ingestion after the window refreshes ``received_at`` instead of
inserting a second row.

Isolation differs by table (§13 KER-205 decision 2): registrations carry RLS
WITHOUT FORCE (the unauthenticated ingest lookup runs before tenant context
exists — the migration-019 auth-bootstrap pattern), while the dedup table is
fully FORCE row-level secured.

How to run or test
------------------
Model files have no executable logic of their own; they are tested through the
services that use them. Syntax-check with:

    python -c "from src.models.webhook_registration import WebhookRegistration; print('OK')"

Unit tests live in tests/unit/services/test_webhook_service.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID as PostgresUUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class WebhookRegistration(Base):
    """One registered webhook source for one tenant, holding its signing secret.

    Rows are created by platform_engineer users through POST /api/v1/webhooks;
    the ingest endpoint loads a row by id (pre-context) and verifies each
    delivery's HMAC against signing_secret. is_active False disables the
    registration without deleting its history.
    """

    __tablename__ = "webhook_registrations"

    # Non-secret public handle for this registration — carried by ingest
    # requests in the X-Kerno-Webhook-Id header.
    id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # The tenant every accepted delivery is routed to. Resolved from the
    # verified signature's registration row — never from request input.
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
        index=True,
    )

    # Which upstream system this registration is for (e.g. "jira", "cmdb").
    source_system: Mapped[str] = mapped_column(Text, nullable=False)

    # Raw HMAC-SHA256 signing key. Plaintext by design (verification needs it);
    # returned to the caller exactly once, at creation or rotation.
    signing_secret: Mapped[str] = mapped_column(Text, nullable=False)

    # When the registration was created. Set by the database clock.
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    # Whether deliveries against this registration are accepted.
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    def __repr__(self) -> str:
        """Short, safe summary for logs — never includes the signing secret."""
        return (
            f"<WebhookRegistration id={self.id!s} "
            f"source_system={self.source_system} is_active={self.is_active}>"
        )


class WebhookIngestDedup(Base):
    """The recent-delivery memory: one row per (tenant, source, external ref).

    A delivery whose triple already exists with received_at inside the dedup
    window is acknowledged without re-processing. Re-ingestion after the
    window refreshes received_at on the same row (upsert), so the unique
    constraint below never blocks a legitimate re-delivery.
    """

    __tablename__ = "webhook_ingest_dedup"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_system", "external_ref",
            name="uq_webhook_dedup_tenant_source_ref",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        nullable=False,
    )

    # The tenant the delivery belonged to (resolved from the signature).
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PostgresUUID(as_uuid=True),
        nullable=False,
    )

    # The claimed upstream system and its reference for the delivered event.
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    external_ref: Mapped[str] = mapped_column(Text, nullable=False)

    # When this triple was last received — the dedup window measures from here.
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("now()"),
        nullable=False,
    )

    def __repr__(self) -> str:
        """Short, safe summary for logs."""
        return (
            f"<WebhookIngestDedup source_system={self.source_system} "
            f"external_ref={self.external_ref}>"
        )
