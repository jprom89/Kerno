"""SEC-REMED-003 — direct evidence uploads are authenticated, then bounded, before any multipart processing.

What:  drives POST /api/v1/evidence through the REAL application's ASGI entry
       point (routing, the real HS256 token dependencies, exception handlers)
       with a scripted receive stream, a recording connection pool and a
       recording stand-in for Starlette's spooled upload file, which logs
       every read size it is asked for. Limits are shrunk per test, so nothing
       large is ever sent.
Why:   regression for finding resource-exhaustion.evidence-buffering (medium /
       medium, source-established at 75f2bf18). The body used to be parsed
       before the token was checked, and the whole file read before the
       10 MiB check. This is a route-level reproduction, not a deployment test.
How:   pytest tests/unit/api/test_evidence_upload_bounds.py -v
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import SpooledTemporaryFile

import jwt
import pytest
import starlette.formparsers as formparsers

from config.constants import (
    EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES,
    EVIDENCE_UPLOAD_MAX_BODY_BYTES,
    EVIDENCE_UPLOAD_MAX_FIELD_BYTES,
    EVIDENCE_UPLOAD_MAX_FIELDS,
    MAX_EVIDENCE_UPLOAD_BYTES,
)
import src.api.dependencies as dependencies
import src.api.evidence_upload_intake as intake
import src.api.routers.evidence as evidence_router
import src.services.evidence_intake as evidence_intake_service
from src.api.app import create_app
from src.api.dependencies import get_conn
from tests.unit.services.test_evidence_intake import _minimal_pdf

_PATH = "/api/v1/evidence"
_TENANT_ID = "a0000000-0000-4000-a000-000000000031"
_USER_ID = "d0000000-0000-4000-d000-000000000031"
_JWT_SECRET = os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")
_BOUNDARY = "KernoSecRemed003Boundary"
_RUN_BOUND_SECONDS = 5.0
_TOKEN_LIFETIME_SECONDS = 3600
_SMALL_FILE_LIMIT = 64
_SMALL_BODY_LIMIT = 1024
_SMALL_FIELD_LIMIT = 32
# Far past CPython's default limit of 4,300 digits on int() of a decimal string.
_LONG_DIGIT_COUNT = 10_000
_CHUNK_BYTES = 256
_LARGE_CHUNK_BYTES = 64 * 1024
_RECORD_TYPE_COLUMN_CHARS = 64
_LONG_FILENAME = "f" * 251 + ".txt"
_FRONTEND_LIMITS = Path(__file__).resolve().parents[3] / "frontend" / "lib" / "evidence-upload-limits.ts"
_STORED_AT = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class _InertConnection:
    """A leased connection on which a rejected upload must never run SQL."""

    def cursor(self):
        raise AssertionError("a rejected upload reached the database")

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _RecordingPool:
    """Stands in for the process pool and counts every lease."""

    def __init__(self) -> None:
        self.getconn_calls = 0
        self.outstanding = 0

    def getconn(self):
        self.getconn_calls += 1
        self.outstanding += 1
        return _InertConnection()

    def putconn(self, conn) -> None:
        self.outstanding -= 1


class _Rows:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _WritableConnection:
    """Answers the upload handler's statements like an empty tenant library, or one already holding `existing`."""

    def __init__(self, existing: tuple | None = None) -> None:
        self.statements: list[tuple[str, object]] = []
        self._existing = existing

    def execute(self, sql: str, params=None) -> _Rows:
        statement = " ".join(sql.split())
        self.statements.append((statement, params))
        if statement.startswith("SELECT record_id, source_system"):
            return _Rows(self._existing)
        if statement.startswith("SELECT created_at"):
            return _Rows((_STORED_AT,))
        return _Rows(None)

    def inserted_bodies(self) -> list[str]:
        return [params["body"] for statement, params in self.statements if statement.startswith("INSERT")]


class _ScriptedBody:
    """ASGI receive: delivers the given chunks; if incomplete, then waits until released and disconnects."""

    def __init__(self, chunks: list[bytes], *, complete: bool = True) -> None:
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


@dataclass
class _Outcome:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        return json.loads(self.body)


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


async def _drive(app, headers: dict, receive: _ScriptedBody, *, while_pending=None) -> _Outcome:
    """Run one request through the app; optionally check state while the body is pending.

    Starlette's server-error middleware sends the 500 and then re-raises for
    the server to log; that re-raise is swallowed only when a response was
    already sent, which is what a client would have seen.
    """
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    task = asyncio.create_task(app(_scope(headers), receive, send))
    try:
        if while_pending is not None:
            await asyncio.wait_for(receive.waiting.wait(), _RUN_BOUND_SECONDS)
            while_pending()
            receive.release.set()
        await asyncio.wait_for(task, _RUN_BOUND_SECONDS)
    except Exception:
        if not any(message["type"] == "http.response.start" for message in sent):
            raise
    finally:
        if not task.done():
            task.cancel()
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_headers = {name.decode().lower(): value.decode() for name, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return _Outcome(start["status"], response_headers, body)


def _send(app, headers: dict, receive: _ScriptedBody, *, while_pending=None) -> _Outcome:
    return asyncio.run(_drive(app, headers, receive, while_pending=while_pending))


def _token(role: str = "compliance_lead", secret: str = _JWT_SECRET) -> str:
    payload = {"sub": _USER_ID, "user_id": _USER_ID, "email": "u@example.test", "role": role,
               "tenant_id": _TENANT_ID, "exp": int(time.time()) + _TOKEN_LIFETIME_SECONDS}
    return jwt.encode(payload, secret, algorithm="HS256")


def _headers(*, token: str | None = None, content_type: str | None = None, **extra: str) -> dict:
    headers = {"Content-Type": content_type or f"multipart/form-data; boundary={_BOUNDARY}", **extra}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _part(name: str, value: bytes, *, filename: str | None = None, content_type: str | None = None) -> bytes:
    disposition = f'form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    head = f"--{_BOUNDARY}\r\nContent-Disposition: {disposition}\r\n"
    if content_type is not None:
        head += f"Content-Type: {content_type}\r\n"
    return head.encode("latin-1") + b"\r\n" + value + b"\r\n"


def _multipart(*parts: bytes) -> bytes:
    return b"".join(parts) + f"--{_BOUNDARY}--\r\n".encode("latin-1")


def _file_part(content: bytes, filename: str = "policy.txt", content_type: str = "text/plain") -> bytes:
    return _part("file", content, filename=filename, content_type=content_type)


def _upload_body(content: bytes, filename: str = "policy.txt", title: bytes | None = None) -> bytes:
    parts = [_file_part(content, filename), _part("record_type", b"policy")]
    if title is not None:
        parts.append(_part("title", title))
    return _multipart(*parts)


def _chunks(body: bytes, size: int = _CHUNK_BYTES) -> list[bytes]:
    return [body[start:start + size] for start in range(0, len(body), size)]


@pytest.fixture
def pool(monkeypatch) -> _RecordingPool:
    recording = _RecordingPool()
    monkeypatch.setattr(dependencies, "_pool", recording)
    return recording


@pytest.fixture
def spooled(monkeypatch) -> list:
    created: list = []

    class _RecordingSpool(SpooledTemporaryFile):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.read_sizes: list[int] = []
            created.append(self)

        def read(self, *args):
            self.read_sizes.append(args[0] if args else -1)
            return super().read(*args)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", _RecordingSpool)
    return created


@pytest.fixture
def small_limits(monkeypatch) -> None:
    monkeypatch.setattr(intake, "MAX_EVIDENCE_UPLOAD_BYTES", _SMALL_FILE_LIMIT)
    monkeypatch.setattr(intake, "EVIDENCE_UPLOAD_MAX_BODY_BYTES", _SMALL_BODY_LIMIT)
    monkeypatch.setattr(intake, "EVIDENCE_UPLOAD_MAX_FIELD_BYTES", _SMALL_FIELD_LIMIT)
    monkeypatch.setattr(evidence_intake_service, "MAX_EVIDENCE_UPLOAD_BYTES", _SMALL_FILE_LIMIT)


@pytest.fixture
def audit_calls(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(evidence_router, "append_audit_entry", lambda *args, **kwargs: calls.append(kwargs))
    return calls


@pytest.fixture
def writable_app():
    connection = _WritableConnection()
    app = create_app()

    def _conn():
        yield connection

    app.dependency_overrides[get_conn] = _conn
    return app, connection


# ── a. Authentication and role are checked before any body byte is read ─────


@pytest.mark.parametrize("token", [None, _token(secret="not-the-server-secret"), "not-a-jwt"],
                         ids=["anonymous", "forged-signature", "malformed-token"])
def test_an_unauthenticated_upload_is_refused_before_any_body_byte_is_read(pool, spooled, token):
    body = _ScriptedBody(_chunks(_upload_body(b"synthetic evidence")))
    outcome = _send(create_app(), _headers(token=token), body)
    assert outcome.status == 401
    assert body.delivered == 0, "the multipart body was read before authentication"
    assert spooled == []
    assert pool.getconn_calls == 0


def test_a_role_without_upload_permission_is_refused_before_any_body_byte_is_read(pool, spooled):
    body = _ScriptedBody(_chunks(_upload_body(b"synthetic evidence")))
    outcome = _send(create_app(), _headers(token=_token("auditor")), body)
    assert outcome.status == 403
    assert body.delivered == 0
    assert spooled == []
    assert pool.getconn_calls == 0


def test_a_pending_authenticated_body_holds_no_database_lease(pool, spooled):
    body = _ScriptedBody([_file_part(b"synthetic evidence")], complete=False)

    def no_lease_while_pending():
        assert pool.outstanding == 0, "a database connection is leased while the upload body is pending"

    outcome = _send(create_app(), _headers(token=_token()), body, while_pending=no_lease_while_pending)
    assert outcome.status == 400
    assert spooled == [], "a body that never completed must not reach the multipart parser"
    assert pool.getconn_calls == 0


# ── b. Header refusals read nothing ──────────────────────────────────────────


@pytest.mark.parametrize("content_type", ["application/json", "text/plain",
                                          "application/x-www-form-urlencoded", "multipart/mixed; boundary=x"])
def test_a_body_that_is_not_multipart_form_data_is_refused_without_reading(pool, content_type):
    body = _ScriptedBody([b"{}"])
    outcome = _send(create_app(), _headers(token=_token(), content_type=content_type), body)
    assert outcome.status == 415
    assert outcome.headers.get("connection") == "close"
    assert body.delivered == 0
    assert pool.getconn_calls == 0


def test_a_multipart_body_without_a_boundary_is_refused_without_reading(pool):
    body = _ScriptedBody([b"--x"])
    outcome = _send(create_app(), _headers(token=_token(), content_type="multipart/form-data"), body)
    assert outcome.status == 400
    assert body.delivered == 0


def test_the_media_type_is_matched_without_regard_to_case(writable_app, audit_calls):
    app, _connection = writable_app
    content_type = f"Multipart/Form-Data; boundary={_BOUNDARY}"
    outcome = _send(app, _headers(token=_token(), content_type=content_type),
                    _ScriptedBody(_chunks(_upload_body(b"synthetic evidence"))))
    assert outcome.status == 201


@pytest.mark.parametrize("declared", ["lots", "-1", "+2", " 2", "1_000", "2.0", "²"])
def test_a_malformed_content_length_is_refused_without_reading(pool, declared):
    body = _ScriptedBody([b"--x"])
    outcome = _send(create_app(), _headers(token=_token(), **{"Content-Length": declared}), body)
    assert outcome.status == 400
    assert outcome.headers.get("connection") == "close"
    assert body.delivered == 0
    assert pool.getconn_calls == 0


def test_a_declared_over_limit_body_is_refused_without_reading_any_of_it(pool, spooled, small_limits):
    body = _ScriptedBody(_chunks(_upload_body(b"x" * _SMALL_BODY_LIMIT)))
    outcome = _send(create_app(), _headers(token=_token(), **{"Content-Length": str(_SMALL_BODY_LIMIT + 1)}), body)
    assert outcome.status == 413
    assert outcome.headers.get("connection") == "close"
    assert body.delivered == 0
    assert spooled == []
    assert pool.getconn_calls == 0


@pytest.mark.parametrize("declared", ["9" * _LONG_DIGIT_COUNT, "1" + "0" * _LONG_DIGIT_COUNT],
                         ids=["all-nines", "one-then-zeros"])
def test_an_extremely_long_content_length_is_a_controlled_refusal_without_reading(pool, spooled, declared):
    body = _ScriptedBody(_chunks(_upload_body(b"synthetic evidence")))
    outcome = _send(create_app(), _headers(token=_token(), **{"Content-Length": declared}), body)
    assert outcome.status == 413
    assert outcome.headers.get("connection") == "close"
    assert outcome.json() == {"detail": "upload exceeds the size limit"}
    assert body.delivered == 0
    assert spooled == []
    assert pool.getconn_calls == 0


def test_leading_zeros_do_not_lift_an_over_limit_declared_length(pool, spooled, small_limits):
    body = _ScriptedBody(_chunks(_upload_body(b"synthetic evidence")))
    declared = "0" * _LONG_DIGIT_COUNT + str(_SMALL_BODY_LIMIT + 1)
    outcome = _send(create_app(), _headers(token=_token(), **{"Content-Length": declared}), body)
    assert outcome.status == 413
    assert body.delivered == 0
    assert spooled == []
    assert pool.getconn_calls == 0


@pytest.mark.parametrize("declared", [str(_SMALL_BODY_LIMIT), "0" * _LONG_DIGIT_COUNT + str(_SMALL_BODY_LIMIT),
                                      "0" * _LONG_DIGIT_COUNT],
                         ids=["at-limit", "zero-padded-at-limit", "long-run-of-zeros"])
def test_a_declared_length_within_the_limit_passes_whatever_its_leading_zeros(
    small_limits, writable_app, audit_calls, declared,
):
    app, _connection = writable_app
    outcome = _send(app, _headers(token=_token(), **{"Content-Length": declared}),
                    _ScriptedBody(_chunks(_upload_body(b"synthetic evidence"))))
    assert outcome.status == 201


# ── c. The actual bytes received are the authority, whatever is declared ────


def test_an_unknown_length_over_limit_body_is_cut_off_without_consuming_the_rest(pool, spooled, small_limits):
    body = _ScriptedBody([b"x" * 400] * 10)
    outcome = _send(create_app(), _headers(token=_token()), body)
    assert outcome.status == 413
    assert outcome.headers.get("connection") == "close"
    assert body.delivered == 3, "intake must stop at the first chunk that crosses the total limit"
    assert spooled == []
    assert pool.getconn_calls == 0


def test_an_understated_content_length_does_not_lift_the_limit(pool, spooled, small_limits):
    body = _ScriptedBody([b"x" * 400] * 10)
    outcome = _send(create_app(), _headers(token=_token(), **{"Content-Length": "100"}), body)
    assert outcome.status == 413
    assert body.delivered == 3
    assert spooled == []
    assert pool.getconn_calls == 0


def test_metadata_that_pushes_the_body_over_the_total_limit_is_refused(pool, spooled, small_limits):
    long_filename = "a" * _SMALL_BODY_LIMIT + ".txt"
    body = _ScriptedBody(_chunks(_multipart(_file_part(b"tiny", long_filename), _part("record_type", b"policy"))))
    outcome = _send(create_app(), _headers(token=_token()), body)
    assert outcome.status == 413
    assert spooled == []
    assert pool.getconn_calls == 0


# ── d. The file itself: at most limit + 1 bytes are ever requested ──────────


def test_a_file_of_exactly_the_limit_is_accepted_and_read_with_one_bounded_request(
    spooled, small_limits, writable_app, audit_calls,
):
    app, connection = writable_app
    content = b"y" * _SMALL_FILE_LIMIT
    outcome = _send(app, _headers(token=_token()), _ScriptedBody(_chunks(_upload_body(content))))
    assert outcome.status == 201
    assert connection.inserted_bodies() == [content.decode()]
    assert len(audit_calls) == 1
    assert [spool.read_sizes for spool in spooled] == [[_SMALL_FILE_LIMIT + 1]]
    assert all(spool.closed for spool in spooled)


def test_a_file_one_byte_over_the_limit_is_refused_after_reading_at_most_limit_plus_one(
    pool, spooled, small_limits, audit_calls,
):
    content = b"y" * (_SMALL_FILE_LIMIT + 1)
    outcome = _send(create_app(), _headers(token=_token()), _ScriptedBody(_chunks(_upload_body(content))))
    assert outcome.status == 413
    assert [spool.read_sizes for spool in spooled] == [[_SMALL_FILE_LIMIT + 1]]
    assert all(spool.closed for spool in spooled)
    assert pool.getconn_calls == 0, "an oversized file must be refused before a database connection is leased"
    assert audit_calls == []


def test_a_far_oversized_file_inside_the_body_limit_is_never_read_in_full(pool, spooled, small_limits, audit_calls):
    content = b"z" * (_SMALL_BODY_LIMIT // 2)
    outcome = _send(create_app(), _headers(token=_token()), _ScriptedBody(_chunks(_upload_body(content))))
    assert outcome.status == 413
    assert max(size for spool in spooled for size in spool.read_sizes) == _SMALL_FILE_LIMIT + 1
    assert -1 not in [size for spool in spooled for size in spool.read_sizes], "an unbounded read was requested"
    assert pool.getconn_calls == 0


# ── e. File, field and metadata counts and sizes are bounded ─────────────────


def test_a_second_file_is_refused_and_every_spooled_file_is_closed(pool, spooled, small_limits, audit_calls):
    body = _multipart(_file_part(b"one"), _file_part(b"two", "second.txt"), _part("record_type", b"policy"))
    outcome = _send(create_app(), _headers(token=_token()), _ScriptedBody(_chunks(body)))
    assert outcome.status == 400
    assert spooled and all(spool.closed for spool in spooled)
    assert pool.getconn_calls == 0
    assert audit_calls == []


def test_a_third_field_is_refused(pool, small_limits, audit_calls):
    body = _multipart(_file_part(b"evidence"), _part("record_type", b"policy"), _part("title", b"t"),
                      _part("extra", b"x"))
    outcome = _send(create_app(), _headers(token=_token()), _ScriptedBody(_chunks(body)))
    assert outcome.status == 400
    assert pool.getconn_calls == 0
    assert audit_calls == []


def test_a_field_over_the_per_field_limit_is_refused(pool, small_limits, audit_calls):
    body = _upload_body(b"evidence", title=b"t" * (_SMALL_FIELD_LIMIT + 1))
    outcome = _send(create_app(), _headers(token=_token()), _ScriptedBody(_chunks(body)))
    assert outcome.status == 400
    assert pool.getconn_calls == 0
    assert audit_calls == []


@pytest.mark.parametrize(("parts", "detail"), [
    ([_part("record_type", b"policy")], "file is required"),
    ([_part("file", b"not a file upload"), _part("record_type", b"policy")], "file must be an uploaded file"),
    ([_file_part(b"evidence")], "record_type is required"),
    ([_file_part(b"evidence"), _part("record_type", b"")], "record_type is required"),
    ([_file_part(b"evidence"), _part("record_type", b"policy"), _part("colour", b"red")],
     "unexpected form field; only file, record_type and title are accepted"),
    ([_file_part(b"evidence"), _part("record_type", b"policy"), _part("record_type", b"report")],
     "each form field may be sent only once"),
], ids=["no-file", "file-as-text", "no-record-type", "empty-record-type", "unknown-field", "repeated-field"])
def test_a_misshapen_form_is_a_422_with_a_readable_detail(pool, spooled, small_limits, audit_calls, parts, detail):
    outcome = _send(create_app(), _headers(token=_token()), _ScriptedBody(_chunks(_multipart(*parts))))
    assert outcome.status == 422
    assert outcome.json() == {"detail": detail}
    assert all(spool.closed for spool in spooled)
    assert pool.getconn_calls == 0
    assert audit_calls == []


# ── f. Cleanup: no spooled file outlives the request ────────────────────────


def test_a_file_part_that_never_finishes_has_its_spooled_file_closed(pool, spooled, small_limits, audit_calls):
    truncated = f"--{_BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n\r\n"
    body = truncated.encode("latin-1") + b"partial evidence with no closing boundary"
    outcome = _send(create_app(), _headers(token=_token()), _ScriptedBody(_chunks(body)))
    assert outcome.status == 422
    assert len(spooled) == 1, "the parser should have started the file part"
    assert spooled[0].closed, "Starlette leaves an unfinished part's spooled file open; intake must close it"
    assert pool.getconn_calls == 0


def test_a_malformed_tail_after_a_file_part_has_its_spooled_file_closed(pool, spooled, small_limits):
    body = _file_part(b"evidence") + f"--{_BOUNDARY}!!garbage".encode("latin-1")
    outcome = _send(create_app(), _headers(token=_token()), _ScriptedBody(_chunks(body)))
    assert outcome.status in (400, 422)
    assert spooled and all(spool.closed for spool in spooled)
    assert pool.getconn_calls == 0


def test_a_client_that_disconnects_mid_file_leaves_no_spooled_file_and_no_lease(pool, spooled, small_limits):
    body = _ScriptedBody([_file_part(b"evidence")[:-4]], complete=False)
    outcome = _send(create_app(), _headers(token=_token()), body, while_pending=lambda: None)
    assert outcome.status == 400
    assert spooled == []
    assert pool.getconn_calls == 0


def test_a_successful_upload_leaves_no_spooled_file_open(spooled, writable_app, audit_calls):
    app, _connection = writable_app
    outcome = _send(app, _headers(token=_token()), _ScriptedBody(_chunks(_upload_body(b"synthetic evidence"))))
    assert outcome.status == 201
    assert spooled and all(spool.closed for spool in spooled)


# ── g. Valid uploads keep their existing contract ────────────────────────────


def test_a_small_text_upload_is_stored_and_ledgered(writable_app, audit_calls):
    app, connection = writable_app
    body = _upload_body(b"Access review policy v2.", filename="policy.txt", title=b"Access review")
    outcome = _send(app, _headers(token=_token()), _ScriptedBody(_chunks(body)))
    assert outcome.status == 201
    result = outcome.json()
    assert result["deduplicated"] is False
    assert result["external_id"] == "policy.txt"
    assert result["record_type"] == "policy"
    assert result["title"] == "Access review"
    assert connection.inserted_bodies() == ["Access review policy v2."]
    assert [call["action_type"] for call in audit_calls] == ["evidence_uploaded"]
    assert audit_calls[0]["actor_id"] == _USER_ID


def test_a_small_pdf_upload_is_extracted_and_stored(writable_app, audit_calls):
    app, connection = writable_app
    pdf = _minimal_pdf("Board approved ISMS policy")
    body = _multipart(_file_part(pdf, "isms.pdf", "application/pdf"), _part("record_type", b"policy"))
    outcome = _send(app, _headers(token=_token()), _ScriptedBody(_chunks(body)))
    assert outcome.status == 201
    assert outcome.json()["title"] == "isms.pdf"
    assert "Board approved ISMS policy" in connection.inserted_bodies()[0]
    assert len(audit_calls) == 1


def test_a_repeated_upload_returns_the_existing_record_without_a_write(audit_calls):
    existing = ("e0000000-0000-4000-e000-000000000031", "upload", "policy.txt", "policy", "Policy", "a" * 64,
                _STORED_AT)
    connection = _WritableConnection(existing=existing)
    app = create_app()

    def _conn():
        yield connection

    app.dependency_overrides[get_conn] = _conn
    outcome = _send(app, _headers(token=_token()), _ScriptedBody(_chunks(_upload_body(b"synthetic evidence"))))
    assert outcome.status == 201
    assert outcome.json()["deduplicated"] is True
    assert outcome.json()["record_id"] == existing[0]
    assert connection.inserted_bodies() == []
    assert audit_calls == []


# ── h. The real limits: a maximum file never fails for its envelope ─────────


def test_the_envelope_allowance_covers_every_accepted_field_at_its_limit():
    assert EVIDENCE_UPLOAD_MAX_BODY_BYTES == MAX_EVIDENCE_UPLOAD_BYTES + EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES
    assert EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES > EVIDENCE_UPLOAD_MAX_FIELDS * EVIDENCE_UPLOAD_MAX_FIELD_BYTES


def test_a_maximum_size_file_with_maximal_metadata_is_accepted_under_the_real_limits(writable_app, audit_calls):
    app, connection = writable_app
    body = _multipart(
        _file_part(b"x" * MAX_EVIDENCE_UPLOAD_BYTES, _LONG_FILENAME),
        _part("record_type", b"r" * _RECORD_TYPE_COLUMN_CHARS),
        _part("title", b"t" * EVIDENCE_UPLOAD_MAX_FIELD_BYTES),
    )
    assert len(body) <= EVIDENCE_UPLOAD_MAX_BODY_BYTES
    outcome = _send(app, _headers(token=_token(), **{"Content-Length": str(len(body))}),
                    _ScriptedBody(_chunks(body, _LARGE_CHUNK_BYTES)))
    assert outcome.status == 201
    assert len(connection.inserted_bodies()[0]) == MAX_EVIDENCE_UPLOAD_BYTES


def _typescript_constant(source: str, name: str) -> str:
    match = re.search(rf"export const {name} = ([^;]+);", source)
    assert match, f"{name} is missing from {_FRONTEND_LIMITS.name}"
    return match.group(1).strip()


def _plain_product(expression: str) -> int:
    assert re.fullmatch(r"\d+(\s*\*\s*\d+)*", expression), f"not a plain product of integers: {expression}"
    return math.prod(int(factor) for factor in expression.split("*"))


def test_the_next_js_proxy_limits_match_the_backend_constants():
    source = _FRONTEND_LIMITS.read_text(encoding="utf-8")
    assert _plain_product(_typescript_constant(source, "MAX_EVIDENCE_FILE_BYTES")) == MAX_EVIDENCE_UPLOAD_BYTES
    assert (_plain_product(_typescript_constant(source, "EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES"))
            == EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES)
    assert (_typescript_constant(source, "EVIDENCE_UPLOAD_MAX_BODY_BYTES")
            == "MAX_EVIDENCE_FILE_BYTES + EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES")
