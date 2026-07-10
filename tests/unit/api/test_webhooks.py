"""Unit tests for the webhook endpoints (KER-205).

Drives the real endpoints through TestClient with genuinely computed
HMAC-SHA256 signatures over the raw request body; a spy connection serves the
registration lookup, the dedup check, and records every write. Includes the
three §13-mandated security tests:
  9a — invalid HMAC -> 401;
  9b — tenant_id_hint cannot override the secret-resolved tenant;
  9c — duplicate external_ref within the window -> 200 and no second
       context_record.
Plus: the signing secret appears exactly once (creation), never in GET;
rotation returns a fresh secret once; unsupported event types are 422 only
AFTER the signature verifies; and registration/rotation demand the
platform_engineer role.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_OTHER_TENANT_ID = "b0000000-0000-4000-b000-000000000002"
_REGISTRATION_ID = str(uuid.UUID("d0000000-0000-4000-d000-000000000005"))
_SECRET = "e" * 64


class _RowResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class _SpyConn:
    """Serves registration + dedup lookups; records all SQL for assertions."""

    def __init__(self, registration_row=..., dedup_hit=False):
        self.calls: list[tuple[str, object]] = []
        self._registration_row = (
            (_TENANT_ID, _SECRET, True) if registration_row is ... else registration_row
        )
        self._dedup_hit = dedup_hit

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM webhook_registrations" in sql and "signing_secret" in sql:
            return _RowResult(self._registration_row)
        if "FROM webhook_registrations" in sql:  # management read (no secret column)
            row = self._registration_row
            if row is None:
                return _RowResult(None)
            from datetime import datetime, timezone
            return _RowResult(
                (_REGISTRATION_ID, _TENANT_ID, "jira", datetime.now(timezone.utc), True)
            )
        if "FROM webhook_ingest_dedup" in sql:
            return _RowResult((1,) if self._dedup_hit else None)
        if "UPDATE webhook_registrations" in sql:
            return _RowResult((_REGISTRATION_ID,))
        return _RowResult(None)

    def statements(self):
        return [sql for sql, _ in self.calls]


def _ingest_app(spy: _SpyConn):
    app = create_app()
    def _conn():
        yield spy
    app.dependency_overrides[get_conn] = _conn
    return TestClient(app)


def _admin_app(spy: _SpyConn, role: str = "platform_engineer"):
    app = create_app()
    def _conn():
        yield spy
    app.dependency_overrides[get_conn] = _conn
    app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    app.dependency_overrides[get_role] = lambda: role
    return TestClient(app)


def _event_body(**overrides) -> bytes:
    event = {
        "source_system": "jira",
        "event_type": "jira.issue.updated",
        "external_ref": "PROJ-123",
        "payload": {"summary": "Fix firewall rule", "description": "Done"},
        "tenant_id_hint": None,
    }
    event.update(overrides)
    return json.dumps(event).encode("utf-8")


def _sign(body: bytes, secret: str = _SECRET) -> dict:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Kerno-Webhook-Id": _REGISTRATION_ID,
        "X-Kerno-Signature": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def _post_ingest(client, body: bytes, headers: dict):
    return client.post("/api/v1/webhooks/ingest", content=body, headers=headers)


# ── Registration management ───────────────────────────────────────────────────


def test_registration_returns_secret_once_and_get_excludes_it():
    spy = _SpyConn()
    client = _admin_app(spy)
    created = client.post("/api/v1/webhooks", json={"source_system": "jira"})
    assert created.status_code == 201
    created_body = created.json()
    assert len(created_body["signing_secret"]) == 64
    assert created_body["source_system"] == "jira"

    fetched = client.get(f"/api/v1/webhooks/{_REGISTRATION_ID}")
    assert fetched.status_code == 200
    assert "signing_secret" not in fetched.json(), "GET must never return the secret"


def test_rotate_returns_new_secret_once():
    spy = _SpyConn()
    client = _admin_app(spy)
    response = client.post(f"/api/v1/webhooks/{_REGISTRATION_ID}/rotate")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"signing_secret"}
    assert len(body["signing_secret"]) == 64


def test_registration_and_rotation_require_platform_engineer():
    for role in ("auditor", "vciso", "compliance_lead", "security_engineer"):
        client = _admin_app(_SpyConn(), role=role)
        assert client.post(
            "/api/v1/webhooks", json={"source_system": "jira"}
        ).status_code == 403, f"{role} must not register webhooks"
        assert client.post(
            f"/api/v1/webhooks/{_REGISTRATION_ID}/rotate"
        ).status_code == 403, f"{role} must not rotate secrets"


# ── Ingest happy path ─────────────────────────────────────────────────────────


def test_ingest_valid_signature_returns_201_and_writes_everything():
    spy = _SpyConn()
    body = _event_body()
    response = _post_ingest(_ingest_app(spy), body, _sign(body))
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "ingested"
    assert uuid.UUID(payload["correlation_id"])  # the new context record's id
    statements = " ".join(spy.statements())
    assert "INSERT INTO context_records" in statements
    assert "webhook_ingest_dedup" in statements
    assert "INSERT INTO audit_log" in statements
    audit_params = next(p for s, p in spy.calls if "INSERT INTO audit_log" in s)
    assert audit_params["action_type"] == "webhook_ingested"
    assert audit_params["object_type"] == "context_record"


# ── Security test 9a — invalid HMAC → 401 ─────────────────────────────────────


def test_ingest_invalid_hmac_returns_401_security_9a():
    spy = _SpyConn()
    body = _event_body()
    headers = _sign(body, secret="0" * 64)  # signed with the WRONG secret
    response = _post_ingest(_ingest_app(spy), body, headers)
    assert response.status_code == 401
    assert "INSERT" not in " ".join(spy.statements()), "nothing may be written"


def test_ingest_missing_signature_returns_401_not_422():
    spy = _SpyConn()
    body = b"not even json"
    response = _post_ingest(
        _ingest_app(spy), body, {"X-Kerno-Webhook-Id": _REGISTRATION_ID}
    )
    assert response.status_code == 401, "signature failures are never 422"


def test_ingest_unknown_webhook_id_returns_401_security_9a():
    spy = _SpyConn(registration_row=None)
    body = _event_body()
    response = _post_ingest(_ingest_app(spy), body, _sign(body))
    assert response.status_code == 401, "unknown id must equal bad signature"


# ── Security test 9b — hint cannot override resolved tenant ───────────────────


def test_ingest_tenant_id_hint_cannot_override_secret_resolved_tenant_security_9b():
    spy = _SpyConn()  # registration resolves to _TENANT_ID
    body = _event_body(tenant_id_hint=_OTHER_TENANT_ID)  # hint claims tenant B
    response = _post_ingest(_ingest_app(spy), body, _sign(body))
    assert response.status_code == 201
    record_params = next(p for s, p in spy.calls if "INSERT INTO context_records" in s)
    assert record_params["tenant_id"] == _TENANT_ID, (
        "the record must land under the SECRET-resolved tenant"
    )
    assert record_params["tenant_id"] != _OTHER_TENANT_ID
    for sql, params in spy.calls:
        if "SET LOCAL" in sql:
            assert params == [_TENANT_ID], "context must never point at the hint"


# ── Security test 9c — duplicate within window → 200, no second record ────────


def test_ingest_duplicate_within_window_returns_200_no_second_record_security_9c():
    spy = _SpyConn(dedup_hit=True)
    body = _event_body()
    response = _post_ingest(_ingest_app(spy), body, _sign(body))
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "duplicate"
    assert payload["correlation_id"] is None
    statements = " ".join(spy.statements())
    assert "INSERT INTO context_records" not in statements, "no second record"
    assert "INSERT INTO audit_log" not in statements, "no duplicate ledger entry"


# ── Event-type validation ordering ────────────────────────────────────────────


def test_ingest_unknown_event_type_returns_422_after_valid_signature():
    spy = _SpyConn()
    body = _event_body(event_type="slack.message.posted")
    response = _post_ingest(_ingest_app(spy), body, _sign(body))
    assert response.status_code == 422


def test_ingest_unknown_event_type_with_bad_signature_is_still_401():
    # The signature gate runs first: an attacker cannot probe supported event
    # types without holding a valid secret.
    spy = _SpyConn()
    body = _event_body(event_type="slack.message.posted")
    response = _post_ingest(_ingest_app(spy), body, _sign(body, secret="0" * 64))
    assert response.status_code == 401
