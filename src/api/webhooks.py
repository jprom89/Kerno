"""Webhook endpoints (KER-205) — registration management and the public ingest surface.

Plain-English summary
---------------------
Two audiences use these endpoints. Platform engineers manage a tenant's
webhook registrations over the authenticated API: create one (the signing
secret is shown exactly once in the 201 response), read it back (never with
the secret), or rotate the secret (the replacement is shown once). Upstream
systems — Jira, CMDBs, anything — deliver events to the public ingest
endpoint, authenticated not by JWT but by an HMAC-SHA256 signature over the
raw request body.

Ingest order of operations is security-critical and fixed:
  1. Verify the signature FIRST, against the registration named by
     X-Kerno-Webhook-Id. Any failure — unknown id, inactive registration,
     missing/malformed header, wrong signature — is the same 401. The raw
     body is read before any parsing so a signature failure can never
     surface as a 422.
  2. Only then parse and validate the body (bad JSON/shape -> 422).
  3. Check the event type (unsupported -> 422).
  4. Check the dedup window (repeat delivery -> 200, nothing written).
  5. Normalise into context_records, record the dedup row, and append the
     KER-107 ledger entry — all on one connection, so the record, the
     dedup memory, and the audit entry commit or roll back together.

The tenant every accepted event lands under comes from the verified
registration ONLY. The body's tenant_id_hint is logged for diagnostics and
influences nothing (§13 KER-205 AC-3). Rate limiting for this public surface
is the deferred gateway-level SEC-05 item (§9).

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/api/test_webhooks.py -v
"""

from __future__ import annotations

import logging
import uuid

import pydantic
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from config.constants import RbacRole
from src.api.dependencies import get_conn, get_tenant_id, require_role
from src.api.schemas.webhooks import (
    WebhookIngestRequest,
    WebhookIngestResponse,
    WebhookRegistrationCreate,
    WebhookRegistrationCreatedResponse,
    WebhookRegistrationResponse,
    WebhookRotateResponse,
)
from src.db.rls import set_tenant_context
from src.exceptions import UnsupportedEventTypeError, WebhookAuthenticationError
from src.services.audit_log import append_audit_entry
from src.services.evidence_service import link_evidence
from src.services.webhook_service import (
    is_duplicate,
    normalise_event,
    record_dedup,
    register_webhook,
    rotate_secret,
    verify_and_resolve_tenant,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_SELECT_REGISTRATION_FOR_READ = """
SELECT id, tenant_id, source_system, created_at, is_active
FROM webhook_registrations
WHERE id = :id AND tenant_id = :tenant_id
"""

_INSERT_CONTEXT_RECORD = """
INSERT INTO context_records
    (record_id, tenant_id, source_system, external_id, record_type,
     title, body, content_hash)
VALUES
    (:record_id, :tenant_id, :source_system, :external_id, :record_type,
     :title, :body, :content_hash)
"""

_STATUS_INGESTED = "ingested"
_STATUS_DUPLICATE = "duplicate"

# Resolves a delivery's human-readable control_ref to the catalogue UUID.
# compliance_controls is global platform data (no tenant column), so this
# lookup needs no tenant scoping; the LINK it feeds is tenant-isolated through
# its context_record (control_evidence_links has no tenant_id of its own).
_SELECT_CONTROL_BY_REF = """
SELECT control_id
FROM compliance_controls
WHERE control_ref = :control_ref AND is_active = TRUE
"""


@router.post("", status_code=201)
def create_registration(
    body: WebhookRegistrationCreate,
    tenant_id: str = Depends(get_tenant_id),
    rbac_role: str = Depends(require_role(RbacRole.PLATFORM_ENGINEER)),
    conn=Depends(get_conn),
) -> WebhookRegistrationCreatedResponse:
    """Register a webhook source for the authenticated tenant (platform_engineer only).

    The 201 response is the ONLY place the signing secret ever appears —
    the caller must store it now. Every later read returns the registration
    without the secret.
    """
    record, plaintext_secret = register_webhook(conn, tenant_id, body.source_system)
    return WebhookRegistrationCreatedResponse(
        id=record.id,
        tenant_id=record.tenant_id,
        source_system=record.source_system,
        created_at=record.created_at,
        is_active=record.is_active,
        signing_secret=plaintext_secret,
    )


@router.get("/{registration_id}")
def get_registration(
    registration_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    rbac_role: str = Depends(require_role(RbacRole.PLATFORM_ENGINEER)),
    conn=Depends(get_conn),
) -> WebhookRegistrationResponse:
    """Return one of the tenant's registrations — never including the signing secret.

    404 when the id does not exist under the caller's tenant; another
    tenant's registration is indistinguishable from a nonexistent one.
    """
    set_tenant_context(conn, tenant_id)
    row = conn.execute(
        _SELECT_REGISTRATION_FOR_READ,
        {"id": str(registration_id), "tenant_id": tenant_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return WebhookRegistrationResponse(
        id=str(row[0]), tenant_id=str(row[1]), source_system=row[2],
        created_at=row[3], is_active=row[4],
    )


@router.post("/{registration_id}/rotate")
def rotate_registration_secret(
    registration_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    rbac_role: str = Depends(require_role(RbacRole.PLATFORM_ENGINEER)),
    conn=Depends(get_conn),
) -> WebhookRotateResponse:
    """Replace the registration's signing secret; the new value is shown once.

    The previous secret stops working the moment this commits. 404 when the
    id does not exist under the caller's tenant.
    """
    try:
        new_secret = rotate_secret(conn, registration_id, tenant_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="not found")
    return WebhookRotateResponse(signing_secret=new_secret)


@router.post("/ingest", status_code=201)
async def ingest_webhook(
    request: Request,
    response: Response,
    conn=Depends(get_conn),
) -> WebhookIngestResponse:
    """Accept one signed webhook delivery (public — the signature is the auth).

    Verifies HMAC-SHA256 over the RAW body before anything else, so a
    signature failure is always 401 and never 422. Resolves the tenant from
    the verified registration only, ignores tenant_id_hint for routing,
    deduplicates within the window (repeat -> 200, no writes), then writes
    the context record, the dedup row, and the KER-107 ledger entry on one
    connection.
    """
    body_bytes = await request.body()
    try:
        tenant_id = verify_and_resolve_tenant(
            conn,
            request.headers.get("X-Kerno-Webhook-Id", ""),
            request.headers.get("X-Kerno-Signature"),
            body_bytes,
        )
    except WebhookAuthenticationError:
        raise HTTPException(status_code=401, detail="invalid webhook signature")
    event = _parse_ingest_body(body_bytes)
    _log_hint_for_diagnostics(event, tenant_id)
    try:
        normalised = normalise_event(event.event_type, event.external_ref, event.payload)
    except UnsupportedEventTypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # Resolved BEFORE the dedupe short-circuit and before any write, so a
    # sender naming an unknown control gets the same 422 whether or not the
    # delivery is a repeat, and never leaves a half-written record behind.
    control_id = _resolve_control_ref(conn, event.control_ref)
    if is_duplicate(conn, tenant_id, event.source_system, event.external_ref):
        # A repeat delivery is acknowledged, not re-created: 200, zero writes.
        response.status_code = status.HTTP_200_OK
        return WebhookIngestResponse(status=_STATUS_DUPLICATE, correlation_id=None)
    record_id = _persist_context_record(conn, tenant_id, event.source_system, normalised)
    if control_id is not None:
        _link_ingested_evidence(
            conn, tenant_id, control_id, record_id,
            request.headers.get("X-Kerno-Webhook-Id", ""),
        )
    record_dedup(conn, tenant_id, event.source_system, event.external_ref)
    _record_ingest_ledger_entry(conn, tenant_id, record_id, event)
    return WebhookIngestResponse(status=_STATUS_INGESTED, correlation_id=record_id)


def _resolve_control_ref(conn, control_ref: str | None) -> str | None:
    """Return the catalogue control_id for a delivery's control_ref, or None if unset.

    An unknown or inactive ref raises 422 rather than storing an unlinkable
    record: silently accepting evidence that names a control we do not have is
    exactly the orphan bug this resolution closes. Returns None only when the
    sender supplied no ref at all (still permitted — see the AC-3 unlinked
    list in KER-406 for how those are surfaced).
    """
    if not control_ref:
        return None
    row = conn.execute(_SELECT_CONTROL_BY_REF, {"control_ref": control_ref}).fetchone()
    if row is None:
        raise HTTPException(
            status_code=422, detail=f"unknown control_ref {control_ref!r}"
        )
    return str(row[0])


def _link_ingested_evidence(
    conn, tenant_id: str, control_id: str, record_id: str, webhook_id: str
) -> None:
    """Link the freshly ingested record to its control, on the same transaction.

    relevance_score is deliberately left NULL: an automated delivery carries no
    human assessment of how well the evidence covers the control, and the
    scorer already treats an unscored link as DEFAULT_RELEVANCE_SCORE. A human
    can set a real score later. linked_by records the VERIFIED registration id
    (the HMAC-authenticated identity) rather than the caller-supplied
    source_system string.
    """
    link_evidence(
        conn,
        tenant_id,
        control_id=control_id,
        record_id=record_id,
        linked_by=f"webhook:{webhook_id}",
        relevance_score=None,
        note=None,
    )


def _parse_ingest_body(body_bytes: bytes) -> WebhookIngestRequest:
    """Validate the raw body into a WebhookIngestRequest — AFTER signature checks.

    Parsing happens manually (not as a FastAPI parameter) precisely so the
    signature is verified first; a malformed body on an authenticated
    delivery is an honest 422.
    """
    try:
        return WebhookIngestRequest.model_validate_json(body_bytes)
    except pydantic.ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc.errors()[0]["msg"]))


def _log_hint_for_diagnostics(event: WebhookIngestRequest, resolved_tenant_id: str) -> None:
    """Log the caller's tenant_id_hint next to the real tenant — and use it for nothing.

    The hint exists so a support engineer can spot a misconfigured sender
    (hint disagreeing with the registration). It never influences routing.
    """
    if event.tenant_id_hint and event.tenant_id_hint != resolved_tenant_id:
        logger.info(
            "webhook tenant_id_hint %s disagrees with secret-resolved tenant %s "
            "(hint ignored)",
            event.tenant_id_hint, resolved_tenant_id,
        )


def _persist_context_record(conn, tenant_id: str, source_system: str, normalised: dict) -> str:
    """Insert the normalised event as a context_records row and return its id.

    Runs under the tenant context set by is_duplicate on the same connection;
    the id is generated here so the ledger entry can reference it without a
    RETURNING round-trip.
    """
    record_id = str(uuid.uuid4())
    conn.execute(
        _INSERT_CONTEXT_RECORD,
        {
            "record_id": record_id,
            "tenant_id": tenant_id,
            "source_system": source_system,
            "external_id": normalised["external_id"],
            "record_type": normalised["record_type"],
            "title": normalised["title"],
            "body": normalised["body"],
            "content_hash": normalised["content_hash"],
        },
    )
    return record_id


def _record_ingest_ledger_entry(
    conn, tenant_id: str, record_id: str, event: WebhookIngestRequest
) -> None:
    """Append the KER-107 ledger entry for one accepted delivery (AC-8).

    Same connection and transaction as the context record and dedup writes,
    so all three commit or roll back together. actor_id None marks the event
    as system-ingested.
    """
    append_audit_entry(
        conn,
        tenant_id,
        actor_id=None,
        actor_role="system",
        action_type="webhook_ingested",
        object_type="context_record",
        object_id=record_id,
        control_id=None,
        after_state={
            "source_system": event.source_system,
            "event_type": event.event_type,
            "external_ref": event.external_ref,
        },
    )
