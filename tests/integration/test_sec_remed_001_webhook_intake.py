"""SEC-REMED-001 — bounded webhook intake against the live database: the authenticated path still works, and leases balance.

What:  sends webhook deliveries through the real application (TestClient,
       no dependency overrides) whose connection pool is a small real
       psycopg2 pool on the approved test database, wrapped to count every
       lease. Proves: a valid signed delivery creates its context record,
       dedup row and ledger entry under the registration's tenant, in one
       transaction; a sequential repeat is acknowledged with nothing new;
       an invalid signature, malformed headers and an over-limit body write
       nothing, the last two without leasing at all; and a failure after the
       writes rolls all of them back and returns the lease exactly once,
       leaving the connection clean for the next delivery.
Why:   The route-level regression (tests/unit/api/test_webhook_intake_bounds.py)
       uses a recording pool; this proves the same order of operations with
       real connections and real rows. The concurrent dedup race reported
       separately is not exercised or fixed here.
How:   pytest tests/integration/test_sec_remed_001_webhook_intake.py -m integration -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading

import psycopg2.pool
import pytest
from fastapi.testclient import TestClient

import src.api.dependencies as dependencies
import src.api.webhooks as webhooks
from src.api.app import create_app
from src.services.webhook_service import register_webhook

_TINY_POOL_MAX = 2
_SMALL_BODY_CAP = 1000


class _CountingPool:
    """A real pool on the approved test database that counts every lease."""

    def __init__(self, inner: psycopg2.pool.ThreadedConnectionPool) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self.getconn_calls = 0
        self.putconn_calls = 0

    def getconn(self):
        with self._lock:
            self.getconn_calls += 1
        return self._inner.getconn()

    def putconn(self, conn) -> None:
        with self._lock:
            self.putconn_calls += 1
        self._inner.putconn(conn)

    def leases(self) -> tuple[int, int]:
        return self.getconn_calls, self.putconn_calls


@pytest.fixture
def counted_pool(db_connection, monkeypatch):
    inner = psycopg2.pool.ThreadedConnectionPool(1, _TINY_POOL_MAX, dsn=os.environ["DATABASE_URL"])
    pool = _CountingPool(inner)
    monkeypatch.setattr(dependencies, "_pool", pool)
    yield pool
    inner.closeall()


@pytest.fixture
def registration(db_connection, tenant_a_id) -> tuple[str, str]:
    with db_connection.transaction():
        record, secret = register_webhook(db_connection, tenant_a_id, "jira")
    return record.id, secret


def _delivery(external_ref: str, tenant_id_hint=None) -> bytes:
    return json.dumps({
        "source_system": "jira",
        "event_type": "jira.issue.updated",
        "external_ref": external_ref,
        "payload": {"summary": "Rotate the firewall keys", "description": "Done."},
        "tenant_id_hint": tenant_id_hint,
    }).encode("utf-8")


def _signed(registration: tuple[str, str], body: bytes, secret: str | None = None) -> dict:
    webhook_id, real_secret = registration
    digest = hmac.new((secret or real_secret).encode(), body, hashlib.sha256).hexdigest()
    return {"X-Kerno-Webhook-Id": webhook_id, "X-Kerno-Signature": f"sha256={digest}",
            "Content-Type": "application/json"}


def _post(body: bytes, headers: dict):
    client = TestClient(create_app(), raise_server_exceptions=False)
    return client.post("/api/v1/webhooks/ingest", content=body, headers=headers)


def _stored(conn, tenant_id, external_ref: str) -> tuple[list, int, int]:
    """Return (context records, dedup rows, webhook ledger entries) for one external_ref in one tenant."""
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        records = conn.execute(
            "SELECT record_id, tenant_id FROM context_records WHERE tenant_id = %s AND external_id = %s",
            [str(tenant_id), external_ref],
        ).fetchall()
        dedup = conn.execute(
            "SELECT count(*) FROM webhook_ingest_dedup WHERE tenant_id = %s AND external_ref = %s",
            [str(tenant_id), external_ref],
        ).fetchone()[0]
        ledger = conn.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND action_type = 'webhook_ingested' "
            "AND after_state->>'external_ref' = %s",
            [str(tenant_id), external_ref],
        ).fetchone()[0]
    return records, dedup, ledger


@pytest.mark.integration
def test_a_valid_signed_delivery_lands_under_the_registration_tenant_in_one_transaction(
    db_connection, counted_pool, registration, tenant_a_id, tenant_b_id
):
    body = _delivery("PROJ-1", tenant_id_hint=str(tenant_b_id))
    response = _post(body, _signed(registration, body))
    assert response.status_code == 201
    assert response.json()["status"] == "ingested"
    records, dedup, ledger = _stored(db_connection, tenant_a_id, "PROJ-1")
    assert [str(row[0]) for row in records] == [response.json()["correlation_id"]]
    assert (dedup, ledger) == (1, 1)
    assert _stored(db_connection, tenant_b_id, "PROJ-1") == ([], 0, 0)
    assert counted_pool.leases() == (1, 1)


@pytest.mark.integration
def test_a_sequential_repeat_is_acknowledged_without_a_second_record(db_connection, counted_pool, registration, tenant_a_id):
    body = _delivery("PROJ-2")
    first = _post(body, _signed(registration, body))
    repeat = _post(body, _signed(registration, body))
    assert (first.status_code, repeat.status_code) == (201, 200)
    assert repeat.json() == {"status": "duplicate", "correlation_id": None}
    records, dedup, ledger = _stored(db_connection, tenant_a_id, "PROJ-2")
    assert (len(records), dedup, ledger) == (1, 1, 1)
    assert counted_pool.leases() == (2, 2)


@pytest.mark.integration
def test_an_invalid_signature_writes_nothing_and_returns_its_lease(db_connection, counted_pool, registration, tenant_a_id):
    body = _delivery("PROJ-3")
    response = _post(body, _signed(registration, body, secret="0" * 64))
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid webhook signature"}
    assert _stored(db_connection, tenant_a_id, "PROJ-3") == ([], 0, 0)
    assert counted_pool.leases() == (1, 1)


@pytest.mark.integration
def test_malformed_headers_are_refused_without_a_lease(db_connection, counted_pool, registration, tenant_a_id):
    body = _delivery("PROJ-4")
    headers = {**_signed(registration, body), "X-Kerno-Signature": "sha256=not-a-digest"}
    response = _post(body, headers)
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid webhook signature"}
    assert counted_pool.leases() == (0, 0)
    assert _stored(db_connection, tenant_a_id, "PROJ-4") == ([], 0, 0)


@pytest.mark.integration
def test_an_over_limit_body_is_refused_before_any_lease(db_connection, counted_pool, registration, tenant_a_id, monkeypatch):
    monkeypatch.setattr(webhooks, "WEBHOOK_MAX_BODY_BYTES", _SMALL_BODY_CAP)
    body = _delivery("PROJ-5").replace(b"Done.", b"x" * (_SMALL_BODY_CAP * 2))
    response = _post(body, _signed(registration, body))
    assert response.status_code == 413
    assert counted_pool.leases() == (0, 0)
    assert _stored(db_connection, tenant_a_id, "PROJ-5") == ([], 0, 0)


@pytest.mark.integration
def test_a_failure_after_the_writes_rolls_them_all_back_and_returns_the_lease_once(
    db_connection, counted_pool, registration, tenant_a_id, monkeypatch
):
    def ledger_unavailable(*args, **kwargs):
        raise RuntimeError("ledger unavailable")

    working_ledger = webhooks._record_ingest_ledger_entry
    monkeypatch.setattr(webhooks, "_record_ingest_ledger_entry", ledger_unavailable)
    body = _delivery("PROJ-6")
    response = _post(body, _signed(registration, body))
    assert response.status_code == 500
    assert _stored(db_connection, tenant_a_id, "PROJ-6") == ([], 0, 0)
    assert counted_pool.leases() == (1, 1)

    monkeypatch.setattr(webhooks, "_record_ingest_ledger_entry", working_ledger)
    retry = _delivery("PROJ-7")
    assert _post(retry, _signed(registration, retry)).status_code == 201
    assert counted_pool.leases() == (2, 2)
