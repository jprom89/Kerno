"""Pydantic schemas for webhook registration, rotation, and ingestion (KER-205).

The one security-loaded rule here: ``signing_secret`` appears ONLY in the two
returned-exactly-once shapes (WebhookRegistrationCreatedResponse and
WebhookRotateResponse). The ordinary read shape, WebhookRegistrationResponse,
has no secret field at all — it cannot leak what it cannot represent.

How:   pytest tests/unit/api/test_webhooks.py -v
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class WebhookRegistrationCreate(BaseModel):
    """The registration request: which upstream system the tenant is connecting."""

    source_system: str


class WebhookRegistrationResponse(BaseModel):
    """A registration as returned by every read endpoint — never the secret."""

    id: str
    tenant_id: str
    source_system: str
    created_at: datetime
    is_active: bool


class WebhookRegistrationCreatedResponse(WebhookRegistrationResponse):
    """The 201 creation response — the only ordinary response carrying the secret.

    The caller must store the secret now: no endpoint returns it again, and
    recovery is only possible by rotating to a new one.
    """

    signing_secret: str


class WebhookRotateResponse(BaseModel):
    """The rotation response: the replacement secret, returned exactly once."""

    signing_secret: str


class WebhookIngestRequest(BaseModel):
    """One inbound webhook delivery.

    tenant_id_hint is diagnostic only — the real tenant is resolved from the
    registration whose secret verified the signature, never from this field.

    control_ref is the human-readable control reference (e.g. "NIS2-21.2b")
    this evidence supports. Optional, but supplying it is what stops the
    delivery becoming an orphan record: with it the ingest links the evidence
    to that control on arrival. An unknown ref is a 422 — a sender that names
    a control we do not have is misconfigured, and silently storing an
    unlinkable record is the bug this field exists to close.
    """

    source_system: str
    event_type: str
    external_ref: str
    payload: dict
    tenant_id_hint: str | None = None
    control_ref: str | None = None


class WebhookIngestResponse(BaseModel):
    """The ingest outcome: 'ingested' with the new record's id, or 'duplicate'."""

    status: str
    correlation_id: str | None = None
