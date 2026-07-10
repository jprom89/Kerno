"""Webhook service — registration, HMAC verification, dedup, and event normalisation (KER-205).

Plain-English summary
---------------------
Five things happen to a webhook over its life, and all five live here:

  1. ``register_webhook`` — a platform engineer connects a source system; a
     random signing secret is generated and returned to the caller exactly
     once (the service returns it; only the 201 response ever shows it).
  2. ``rotate_secret`` — replaces a registration's secret with a fresh one,
     returned once.
  3. ``verify_and_resolve_tenant`` — the security gate for every inbound
     delivery: loads the one registration named by X-Kerno-Webhook-Id,
     recomputes HMAC-SHA256 over the raw body with that registration's
     secret, compares in constant time, and returns the registration's
     tenant. The tenant is NEVER taken from the request; tenant_id_hint is
     diagnostics only. Unknown id, inactive registration, malformed header,
     and wrong signature all raise the same error, so a caller cannot tell
     which part failed.
  4. ``is_duplicate`` / ``record_dedup`` — the idempotency memory: a
     (source_system, external_ref) pair seen inside
     WEBHOOK_DEDUP_WINDOW_HOURS is acknowledged without re-processing;
     recording is an upsert that refreshes received_at, so re-delivery after
     the window works despite the unique constraint.
  5. ``normalise_event`` / ``WebhookNormaliser`` — the thin translation from
     the four supported event types to a context_records-compatible dict.
     No new ingest framework: the output feeds the existing evidence table.

Tenant isolation: ``verify_and_resolve_tenant`` deliberately reads
webhook_registrations WITHOUT tenant context — the auth-bootstrap exception
(§13 KER-205 decision 2; the table is RLS-without-FORCE for exactly this
read). Every other function sets tenant context first, and the dedup table
is FORCE row-level secured.

The ``conn`` parameter throughout must be a raw database connection
supporting ``conn.execute(sql, params_dict)`` — not a SQLAlchemy Session.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/services/test_webhook_service.py -v
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from config.constants import WEBHOOK_DEDUP_WINDOW_HOURS
from src.db.rls import set_tenant_context
from src.exceptions import UnsupportedEventTypeError, WebhookAuthenticationError

# Length in bytes of a newly generated signing secret; 32 bytes = 64 hex
# characters = a 256-bit HMAC key.
_SIGNING_SECRET_BYTES = 32

# The scheme prefix every X-Kerno-Signature value must carry.
_SIGNATURE_PREFIX = "sha256="


_INSERT_REGISTRATION = """
INSERT INTO webhook_registrations (id, tenant_id, source_system, signing_secret)
VALUES (:id, :tenant_id, :source_system, :signing_secret)
"""

_SELECT_REGISTRATION_FOR_VERIFY = """
SELECT tenant_id, signing_secret, is_active
FROM webhook_registrations
WHERE id = :id
"""

_UPDATE_SECRET = """
UPDATE webhook_registrations
SET signing_secret = :signing_secret
WHERE id = :id AND tenant_id = :tenant_id
RETURNING id
"""

_SELECT_RECENT_DEDUP = """
SELECT 1
FROM webhook_ingest_dedup
WHERE tenant_id = :tenant_id
  AND source_system = :source_system
  AND external_ref = :external_ref
  AND received_at > :window_start
"""

# Upsert: re-delivery after the dedup window refreshes received_at on the
# existing row instead of violating the unique triple constraint.
_UPSERT_DEDUP = """
INSERT INTO webhook_ingest_dedup (tenant_id, source_system, external_ref)
VALUES (:tenant_id, :source_system, :external_ref)
ON CONFLICT ON CONSTRAINT uq_webhook_dedup_tenant_source_ref
DO UPDATE SET received_at = now()
"""


@dataclasses.dataclass(frozen=True)
class WebhookRegistrationRecord:
    """A registration as the service hands it to the API layer — no secret field."""

    id: str
    tenant_id: str
    source_system: str
    created_at: datetime
    is_active: bool


def register_webhook(conn, tenant_id, source_system: str) -> tuple[WebhookRegistrationRecord, str]:
    """Create a registration for the tenant and return it with its one-time secret.

    Generates the signing secret with the standard-library CSPRNG and the id
    in Python so no RETURNING round-trip is needed. Sets tenant context first;
    raises TenantContextMissingError on a missing or invalid tenant. The
    plaintext secret is returned ONLY here — persist it caller-side or lose it.
    """
    set_tenant_context(conn, tenant_id)
    registration_id = str(uuid.uuid4())
    plaintext_secret = secrets.token_hex(_SIGNING_SECRET_BYTES)
    conn.execute(
        _INSERT_REGISTRATION,
        {
            "id": registration_id,
            "tenant_id": str(tenant_id),
            "source_system": source_system,
            "signing_secret": plaintext_secret,
        },
    )
    record = WebhookRegistrationRecord(
        id=registration_id,
        tenant_id=str(tenant_id),
        source_system=source_system,
        created_at=datetime.now(timezone.utc),
        is_active=True,
    )
    return record, plaintext_secret


def rotate_secret(conn, registration_id, tenant_id) -> str:
    """Replace a registration's signing secret and return the new one (once).

    Scoped to the caller's tenant — rotating another tenant's registration is
    impossible because the UPDATE filters on both id and tenant_id. Raises
    LookupError when no matching registration exists (the router maps it to
    404). Sets tenant context first.
    """
    set_tenant_context(conn, tenant_id)
    new_secret = secrets.token_hex(_SIGNING_SECRET_BYTES)
    row = conn.execute(
        _UPDATE_SECRET,
        {
            "signing_secret": new_secret,
            "id": str(registration_id),
            "tenant_id": str(tenant_id),
        },
    ).fetchone()
    if row is None:
        raise LookupError("webhook registration not found for this tenant")
    return new_secret


def verify_and_resolve_tenant(conn, webhook_id: str, signature: str | None, body_bytes: bytes) -> str:
    """Authenticate a delivery and return the tenant it belongs to.

    Loads the registration named by the X-Kerno-Webhook-Id header — a
    deliberate PRE-CONTEXT read (the signature IS the authentication, so no
    tenant context exists yet; the registrations table is RLS-without-FORCE
    for exactly this). Recomputes HMAC-SHA256 over the raw request body with
    the stored secret and compares constant-time. Raises
    WebhookAuthenticationError for unknown/malformed id, inactive
    registration, missing/malformed signature header, or HMAC mismatch —
    one indistinguishable error for all causes.
    """
    provided_hex = _extract_signature_hex(signature)
    row = _load_registration_for_verify(conn, webhook_id)
    tenant_id, signing_secret, is_active = str(row[0]), row[1], row[2]
    if not is_active:
        raise WebhookAuthenticationError("registration is inactive")
    expected_hex = hmac.new(
        signing_secret.encode("utf-8"), body_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hex, provided_hex):
        raise WebhookAuthenticationError("signature mismatch")
    return tenant_id


def is_duplicate(conn, tenant_id, source_system: str, external_ref: str) -> bool:
    """Return True if this (source, ref) pair was received inside the dedup window.

    Sets tenant context first (the dedup table is FORCE row-level secured).
    The window is WEBHOOK_DEDUP_WINDOW_HOURS; the cutoff is computed here so
    the SQL stays a plain bound-parameter comparison.
    """
    set_tenant_context(conn, tenant_id)
    window_start = datetime.now(timezone.utc) - timedelta(hours=WEBHOOK_DEDUP_WINDOW_HOURS)
    row = conn.execute(
        _SELECT_RECENT_DEDUP,
        {
            "tenant_id": str(tenant_id),
            "source_system": source_system,
            "external_ref": external_ref,
            "window_start": window_start,
        },
    ).fetchone()
    return row is not None


def record_dedup(conn, tenant_id, source_system: str, external_ref: str) -> None:
    """Remember that this (source, ref) pair was received now.

    An upsert: a pre-existing row from before the window gets its received_at
    refreshed instead of violating the unique constraint. Sets tenant context
    first; runs on the caller's transaction alongside the context_records
    insert so the memory and the record commit together.
    """
    set_tenant_context(conn, tenant_id)
    conn.execute(
        _UPSERT_DEDUP,
        {
            "tenant_id": str(tenant_id),
            "source_system": source_system,
            "external_ref": external_ref,
        },
    )


class WebhookNormaliser:
    """Translates the four supported event types into context_records fields.

    Thin by design (§13 KER-205 AC-6): each event type maps to a record_type
    and a title/body extraction; the payload's SHA-256 becomes content_hash so
    identical payloads are detectable downstream. No new ingest framework —
    the output dict feeds the existing evidence table from migration 007.
    """

    # event_type -> the context_records.record_type it lands as.
    SUPPORTED_EVENT_TYPES: dict[str, str] = {
        "jira.issue.updated": "ticket",
        "jira.issue.closed": "ticket",
        "cmdb.asset.updated": "asset",
        "generic.evidence.submitted": "evidence",
    }

    def normalise(self, event_type: str, external_ref: str, payload: dict) -> dict:
        """Return a context_records-compatible dict for one accepted event.

        Raises UnsupportedEventTypeError for any event type outside the
        Sprint 2b four. Title falls back to the external ref so a record is
        always findable; the body is the payload's human-readable core.
        """
        record_type = self.SUPPORTED_EVENT_TYPES.get(event_type)
        if record_type is None:
            raise UnsupportedEventTypeError(
                f"unsupported event_type '{event_type}'; supported: "
                f"{sorted(self.SUPPORTED_EVENT_TYPES)}"
            )
        return {
            "record_type": record_type,
            "external_id": external_ref,
            "title": self._extract_title(event_type, external_ref, payload),
            "body": self._extract_body(event_type, payload),
            "content_hash": _hash_payload(payload),
        }

    def _extract_title(self, event_type: str, external_ref: str, payload: dict) -> str:
        """Return the record's display title, falling back to the external ref."""
        title = (
            payload.get("summary")
            or payload.get("title")
            or payload.get("asset_name")
            or payload.get("name")
        )
        if event_type == "jira.issue.closed" and title:
            return f"{title} (closed)"
        return title or external_ref

    def _extract_body(self, event_type: str, payload: dict) -> str:
        """Return the record's text body — the payload's descriptive fields,
        or the whole payload as compact JSON when none are present."""
        body = payload.get("description") or payload.get("body")
        if body:
            return str(body)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def normalise_event(event_type: str, external_ref: str, payload: dict) -> dict:
    """Module-level convenience wrapper over WebhookNormaliser.normalise."""
    return WebhookNormaliser().normalise(event_type, external_ref, payload)


def _extract_signature_hex(signature: str | None) -> str:
    """Return the hex digest from an 'sha256=<hex>' header value, or fail closed.

    A missing header or any other scheme raises WebhookAuthenticationError —
    the same error as a wrong signature, so the header format is not probeable.
    """
    if not signature or not signature.startswith(_SIGNATURE_PREFIX):
        raise WebhookAuthenticationError("missing or malformed signature header")
    return signature[len(_SIGNATURE_PREFIX):]


def _load_registration_for_verify(conn, webhook_id: str):
    """Return the registration row for the ingest gate, or fail closed.

    A non-UUID id and an unknown id raise the same WebhookAuthenticationError,
    keeping unknown-id responses indistinguishable from bad signatures.
    """
    try:
        canonical_id = str(uuid.UUID(str(webhook_id)))
    except (ValueError, AttributeError, TypeError):
        raise WebhookAuthenticationError("unknown webhook id")
    row = conn.execute(_SELECT_REGISTRATION_FOR_VERIFY, {"id": canonical_id}).fetchone()
    if row is None:
        raise WebhookAuthenticationError("unknown webhook id")
    return row


def _hash_payload(payload: dict) -> str:
    """Return the SHA-256 hex digest of the payload's canonical JSON form."""
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
