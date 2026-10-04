"""SEC-REMED-001 — webhook intake is bounded and leases no database connection until the body is complete.

Regression for finding resource-exhaustion.webhook-pool-lease (medium /
medium, source-established at 75f2bf18). Drives the REAL application through
its ASGI entry point — routing, dependency resolution and exception handlers
included — with a recording connection pool standing in for psycopg2's and a
scripted ASGI receive stream standing in for the client. No database, no
network, no running server: a route-level reproduction, not a browser or
deployment test. Limits are shrunk per test so nothing large is ever sent.

Run: pytest tests/unit/api/test_webhook_intake_bounds.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid

import pytest

import src.api.dependencies as dependencies
import src.api.webhooks as webhooks
from src.api.app import create_app

_PATH = "/api/v1/webhooks/ingest"
_WEBHOOK_ID = str(uuid.UUID("d0000000-0000-4000-d000-0000000000a1"))
_PLAUSIBLE_SIGNATURE = "sha256=" + "ab" * 32
_RUN_BOUND_SECONDS = 5.0
_SMALL_BODY_CAP = 1000
_SHORT_DEADLINE_SECONDS = 0.3
_DRIP_INTERVAL_SECONDS = 0.05


class _InertConnection:
    """A leased connection on which intake must never run SQL; records how the transaction ended."""

    def __init__(self) -> None:
        self.endings: list[str] = []

    def cursor(self):
        raise AssertionError("intake reached the database")

    def commit(self) -> None:
        self.endings.append("commit")

    def rollback(self) -> None:
        self.endings.append("rollback")


class _RecordingPool:
    """Stands in for the process pool and counts every lease."""

    def __init__(self) -> None:
        self.getconn_calls = 0
        self.putconn_calls = 0
        self.outstanding = 0
        self.connections: list[_InertConnection] = []

    def getconn(self):
        self.getconn_calls += 1
        self.outstanding += 1
        self.connections.append(_InertConnection())
        return self.connections[-1]

    def putconn(self, conn) -> None:
        self.putconn_calls += 1
        self.outstanding -= 1


class _ScriptedBody:
    """ASGI receive: delivers the given chunks; if incomplete, then waits until released and disconnects."""

    def __init__(self, chunks: list[bytes], *, complete: bool) -> None:
        self._chunks = list(chunks)
        self._complete = complete
        self.delivered = 0
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> dict:
        if self.delivered < len(self._chunks):
            chunk = self._chunks[self.delivered]
            self.delivered += 1
            last = self._complete and self.delivered == len(self._chunks)
            return {"type": "http.request", "body": chunk, "more_body": not last}
        self.waiting.set()
        await self.release.wait()
        return {"type": "http.disconnect"}


class _DripBody:
    """ASGI receive: one small chunk per interval, forever — never finishes on its own."""

    def __init__(self, chunk: bytes, interval: float) -> None:
        self._chunk = chunk
        self._interval = interval
        self.delivered = 0

    async def __call__(self) -> dict:
        await asyncio.sleep(self._interval)
        self.delivered += 1
        return {"type": "http.request", "body": self._chunk, "more_body": True}


def _scope(headers: dict[str, str]) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": _PATH,
        "raw_path": _PATH.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }


async def _drive(headers: dict, receive, *, while_pending=None) -> int:
    """Run one request through the real app; optionally check state while the body is pending. Return the status.

    Starlette's server-error middleware sends the 500 and then re-raises for
    the server to log; that re-raise is swallowed here only when a response
    was already sent, which is what a client would have seen.
    """
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    task = asyncio.create_task(create_app()(_scope(headers), receive, send))
    try:
        if while_pending is not None:
            await asyncio.wait_for(receive.waiting.wait(), _RUN_BOUND_SECONDS)
            while_pending()
            receive.release.set()
        await asyncio.wait_for(task, _RUN_BOUND_SECONDS)
    except Exception:
        if not any(m["type"] == "http.response.start" for m in sent):
            raise
    finally:
        if not task.done():
            task.cancel()
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def _run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def pool(monkeypatch) -> _RecordingPool:
    recording = _RecordingPool()
    monkeypatch.setattr(dependencies, "_pool", recording)
    return recording


@pytest.fixture
def small_limits(monkeypatch) -> None:
    monkeypatch.setattr(webhooks, "WEBHOOK_MAX_BODY_BYTES", _SMALL_BODY_CAP, raising=False)
    monkeypatch.setattr(webhooks, "WEBHOOK_BODY_DEADLINE_SECONDS", _SHORT_DEADLINE_SECONDS, raising=False)


def _plausible_headers(**extra: str) -> dict:
    return {"X-Kerno-Webhook-Id": _WEBHOOK_ID, "X-Kerno-Signature": _PLAUSIBLE_SIGNATURE,
            "Content-Type": "application/json", **extra}


# ── a. Missing or malformed authentication headers: no body read, no lease ──


@pytest.mark.parametrize("headers", [
    {},
    {"X-Kerno-Webhook-Id": _WEBHOOK_ID},
    {"X-Kerno-Signature": _PLAUSIBLE_SIGNATURE},
    {"X-Kerno-Webhook-Id": "not-a-uuid", "X-Kerno-Signature": _PLAUSIBLE_SIGNATURE},
    {"X-Kerno-Webhook-Id": _WEBHOOK_ID, "X-Kerno-Signature": "md5=" + "ab" * 16},
    {"X-Kerno-Webhook-Id": _WEBHOOK_ID, "X-Kerno-Signature": "sha256=" + "zz" * 32},
    {"X-Kerno-Webhook-Id": _WEBHOOK_ID, "X-Kerno-Signature": "sha256=" + "ab" * 31},
    {"X-Kerno-Webhook-Id": _WEBHOOK_ID, "X-Kerno-Signature": "sha256=" + "AB" * 32},
    {"X-Kerno-Webhook-Id": _WEBHOOK_ID, "X-Kerno-Signature": "sha256=" + "é" * 64},
], ids=["none", "no-signature", "no-id", "id-not-uuid", "other-scheme", "not-hex", "short-hex",
        "uppercase-hex", "non-ascii"])
def test_missing_or_malformed_auth_headers_are_refused_before_the_body_or_a_lease(pool, headers):
    body = _ScriptedBody([b'{"event_type": "x"}'], complete=True)
    status = _run(_drive(headers, body))
    assert status == 401
    assert body.delivered == 0, "the body was read before the headers were checked"
    assert pool.getconn_calls == 0


# ── b. Plausible headers and a pending body: no lease while it is pending ───


def test_a_pending_anonymous_body_holds_no_database_lease(pool):
    body = _ScriptedBody([b'{"source_system": '], complete=False)

    def no_lease_while_pending():
        assert pool.outstanding == 0, "a database connection is leased while the anonymous body is pending"

    status = _run(_drive(_plausible_headers(), body, while_pending=no_lease_while_pending))
    assert status == 400
    assert pool.getconn_calls == 0


# ── c. Small chunks cannot stretch reception past the total deadline ────────


def test_small_chunks_cannot_extend_reception_past_the_total_deadline(pool, small_limits):
    drip = _DripBody(b"x", _DRIP_INTERVAL_SECONDS)
    started = time.monotonic()
    status = _run(_drive(_plausible_headers(), drip))
    elapsed = time.monotonic() - started
    assert status == 408
    assert elapsed < _SHORT_DEADLINE_SECONDS + 1.0, f"reception ran {elapsed:.2f}s against a {_SHORT_DEADLINE_SECONDS}s deadline"
    assert drip.delivered <= int(_SHORT_DEADLINE_SECONDS / _DRIP_INTERVAL_SECONDS) + 1
    assert pool.getconn_calls == 0


# ── d. Over-limit bodies stop intake without consuming the rest ─────────────


def test_a_declared_over_limit_body_is_refused_without_reading_any_of_it(pool, small_limits):
    body = _ScriptedBody([b"x" * 400] * 10, complete=True)
    status = _run(_drive(_plausible_headers(**{"Content-Length": str(_SMALL_BODY_CAP * 4)}), body))
    assert status == 413
    assert body.delivered == 0
    assert pool.getconn_calls == 0


def test_an_unknown_length_over_limit_body_is_cut_off_without_consuming_the_rest(pool, small_limits):
    body = _ScriptedBody([b"x" * 400] * 10, complete=True)
    status = _run(_drive(_plausible_headers(), body))
    assert status == 413
    assert body.delivered == 3, "intake must stop at the first chunk that crosses the cap"
    assert pool.getconn_calls == 0


def test_an_understated_content_length_does_not_lift_the_cap(pool, small_limits):
    body = _ScriptedBody([b"x" * 400] * 10, complete=True)
    status = _run(_drive(_plausible_headers(**{"Content-Length": "100"}), body))
    assert status == 413
    assert body.delivered == 3
    assert pool.getconn_calls == 0


@pytest.mark.parametrize("declared", ["lots", "-1", "+2", " 2", "1_000", "2.0", "²"])
def test_a_malformed_content_length_is_refused_without_reading(pool, declared):
    body = _ScriptedBody([b"{}"], complete=True)
    status = _run(_drive(_plausible_headers(**{"Content-Length": declared}), body))
    assert status == 400
    assert body.delivered == 0
    assert pool.getconn_calls == 0


# ── The lease is taken only for a complete, bounded body, and returned once ─


def _signed_headers(body: bytes, secret: str) -> dict:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Kerno-Webhook-Id": _WEBHOOK_ID, "X-Kerno-Signature": f"sha256={digest}",
            "Content-Type": "application/json"}


def test_a_complete_bounded_body_leases_once_and_returns_the_lease_once(pool):
    payload = json.dumps({"source_system": "jira", "event_type": "jira.issue.updated",
                          "external_ref": "PROJ-1", "payload": {}}).encode()
    body = _ScriptedBody([payload[:10], payload[10:]], complete=True)
    status = _run(_drive(_signed_headers(payload, "e" * 64), body))
    # The inert connection refuses SQL, so verification fails closed as a 500
    # after the lease; what matters here is when, and how often, it was leased.
    assert body.delivered == 2
    assert (pool.getconn_calls, pool.putconn_calls, pool.outstanding) == (1, 1, 0)
    assert pool.connections[0].endings == ["rollback"]
    assert status == 500
