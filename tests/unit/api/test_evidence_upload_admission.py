"""PILOT-READY-004 (G6) — evidence uploads are admitted into a fixed number of slots and bounded at every stage.

What:  drives POST /api/v1/evidence through the REAL application's ASGI entry
       point (routing, the real HS256 token dependencies, exception handlers,
       the real get_conn over a recording stand-in for the process pool) with
       scripted, trickled and held-open receive streams, overlapping requests
       on one event loop, the real PDF worker process, and probe processes
       that hang or cannot start. It shows: a body that does not arrive within
       the reception deadline is a 408 however its chunks are spaced; a full
       house refuses the next upload at once with 503 and Retry-After, before
       reading its body, while authentication and role refusals never take a
       slot; the slot is released after every outcome and can be used again;
       text is extracted before the first database lease and every refused
       upload leases nothing, inserts nothing and writes no ledger entry;
       real PDFs are stored with the hash of their stripped text, deduplicated
       and ledgered, while refused PDFs give their exact detail; a PDF whose
       memory grows until the real worker's 512 MiB ceiling stops it is a 422
       that leases, inserts and ledgers nothing, its worker reaped with both
       pipes closed and its private directory gone, and the freed slot then
       stores a normal PDF; a PDF worker
       past its deadline is killed and reaped before the slot is released,
       even when the request is cancelled; a worker that cannot start is a
       503; and the pilot app behaves the same.
Why:   gate G6 of PILOT-READY-004: an authorised client must not be able to
       hold the API's memory, threads, database connections or a PDF parser
       indefinitely. Only real overlapping requests, real timing and real
       processes show that; constants alone prove nothing.
How:   pytest tests/unit/api/test_evidence_upload_admission.py -v
       The tests marked slow wait for a hanging probe to outlive its deadline.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import logging
import sys
import threading
import time
from pathlib import Path
from tempfile import SpooledTemporaryFile

import psycopg2
import pytest
import starlette.formparsers as formparsers

import src.api.dependencies as dependencies
import src.api.evidence_upload_intake as intake
import src.api.evidence_upload_processing as processing
import src.api.routers.evidence as evidence_router
import src.services.evidence_intake as evidence_intake_service
from config.constants import (
    EVIDENCE_UPLOAD_MAX_ACTIVE,
    EVIDENCE_UPLOAD_MAX_BODY_BYTES,
    EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS,
    PDF_MAX_PAGES,
    PDF_MAX_STREAM_OUTPUT_BYTES,
)
from src.api.app import create_app
from src.api.evidence_upload_processing import UploadAdmission, get_upload_admission
from src.services import pdf_text_extraction
from tests.unit.api.test_evidence_upload_bounds import (
    _STORED_AT,
    _TENANT_ID,
    _USER_ID,
    _Outcome,
    _ScriptedBody,
    _chunks,
    _file_part,
    _headers,
    _multipart,
    _part,
    _scope,
    _token,
    _upload_body,
)
from tests.unit.services._pdf_documents import (
    build_pdf,
    build_pdf_with_amplifying_to_unicode,
    build_pdf_with_deflated_page,
    deflated_text_with_padding,
    reference_text,
    text_operations,
)

_PROBE_PATH = Path(__file__).resolve().parents[1] / "services" / "pdf_worker_probes" / "probe_worker.py"

# The exact details the dashboard shows. Written out here, not imported, so a
# change to the wording fails a test.
_BUSY = "evidence uploads are busy; try again shortly"
_TOO_SLOW = "upload was not received within the time limit"
_UNREADABLE = "the PDF could not be read"
_ENCRYPTED = "encrypted PDFs are not supported"
_TOO_MANY_PAGES = "the PDF has more than 500 pages"
_OVER_LIMITS = "the PDF exceeds the text-extraction limits"
_TIMED_OUT = "the PDF could not be processed within the time limit"
_UNAVAILABLE = "evidence processing is unavailable; try again later"

_CAPACITY = 2
_RACING_THREADS = 16
_SCENARIO_BOUND_SECONDS = 30.0
_WAIT_BOUND_SECONDS = 10.0
_POLL_SECONDS = 0.02
# Small chunks, so a held upload pauses with most of its body still unsent.
_HELD_CHUNK_BYTES = 64
_PAUSE_AFTER_CHUNKS = 2
# A refusal at a full house must not wait for anything: far under any deadline.
_PROMPT_SECONDS = 1.0
_SHORT_RECEPTION_SECONDS = 0.4
_TRICKLE_INTERVAL_SECONDS = 0.05
_TRICKLE_CHUNK_BYTES = 8
_UNHURRIED_CHUNK_BYTES = 128
_TIMING_ALLOWANCE_SECONDS = 1.0
_SHORT_EXTRACTION_SECONDS = 1.5
_PROBE_SLEEP_SECONDS = 3.0
_KILL_ALLOWANCE_SECONDS = 1.5
_SURVIVAL_MARGIN_SECONDS = 1.0
_HELD_AFTER_CANCEL_SECONDS = 0.3
_SMALL_FILE_LIMIT = 64
_SMALL_BODY_LIMIT = 1024
# A few kilobytes of PDF whose ToUnicode map makes the worker's memory grow
# 4,000,000 characters at a time until its real 512 MiB ceiling stops it.
_GROWING_MAPPED_CHARACTERS = 4_000_000
_GROWING_TIMES_SHOWN = 400

_AUTHORISED = _headers(token=_token())
_REUSE_TEXT = b"Evidence uploaded through a slot that was released."
_PDF_TEXT_LINES = ("Board approved ISMS policy", "Reviewed every quarter by the CISO")
_PROBE_PDF = build_pdf([text_operations("any document; the probe never reads it")])


# ── Stand-ins: receive streams, slots, the pool and its connection ──────────


class _GatedBody:
    """ASGI receive: the body in chunks, pausing after pause_at of them until released.

    With complete=False the stream disconnects at the pause instead of sending the rest.
    """

    def __init__(self, chunks: list[bytes], pause_at: int, *, complete: bool = True) -> None:
        """Hold the chunks to deliver and the point at which to pause."""
        self._chunks = list(chunks)
        self._pause_at = pause_at
        self._complete = complete
        self.delivered = 0
        self.waiting = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self) -> dict:
        """Deliver the next chunk, pausing once at pause_at; after a release, the rest or a disconnect."""
        if self.delivered == self._pause_at and not self.release.is_set():
            self.waiting.set()
            await self.release.wait()
        if self.delivered < self._pause_at or (self._complete and self.delivered < len(self._chunks)):
            chunk = self._chunks[self.delivered]
            self.delivered += 1
            return {"type": "http.request", "body": chunk, "more_body": self.delivered < len(self._chunks)}
        return {"type": "http.disconnect"}


class _TrickledBody:
    """ASGI receive: every chunk after the same short pause, so no single gap is long but the total can be."""

    def __init__(self, chunks: list[bytes], interval: float) -> None:
        """Hold the chunks and the pause before each one."""
        self._chunks = list(chunks)
        self._interval = interval
        self.delivered = 0

    async def __call__(self) -> dict:
        """Wait the interval, then deliver the next chunk."""
        await asyncio.sleep(self._interval)
        chunk = self._chunks[self.delivered]
        self.delivered += 1
        return {"type": "http.request", "body": chunk, "more_body": self.delivered < len(self._chunks)}


class _RecordingAdmission(UploadAdmission):
    """Real upload slots that also count attempts, log each admission and release, and observe each release."""

    def __init__(self, capacity: int, sequence: list | None = None, observe_release=None) -> None:
        """Create the slots; sequence receives the log, observe_release is called at every release."""
        super().__init__(capacity)
        self.attempts = 0
        self.release_observations: list = []
        self._sequence = [] if sequence is None else sequence
        self._observe_release = observe_release

    def try_admit(self) -> bool:
        """Count the attempt and log a successful admission."""
        self.attempts += 1
        admitted = super().try_admit()
        if admitted:
            self._sequence.append("slot taken")
        return admitted

    def release(self) -> None:
        """Observe the moment of release, give the slot back and log it."""
        if self._observe_release is not None:
            self.release_observations.append(self._observe_release())
        super().release()
        self._sequence.append("slot released")


class _LibraryCursor:
    """A cursor over one _LibraryConnection: each statement is logged and answered from the library."""

    def __init__(self, connection: _LibraryConnection) -> None:
        """Remember the connection this cursor runs on."""
        self._connection = connection
        self._row = None

    def execute(self, sql: str, params=None) -> None:
        """Log the statement with its parameters and keep the answer for fetchone."""
        statement = " ".join(sql.split())
        self._connection.statements.append((statement, params))
        self._row = self._connection.answer(statement, params)

    def fetchone(self):
        """Return the answer to the last statement."""
        return self._row

    def fetchall(self) -> list:
        """Return the answer to the last statement as a list of rows."""
        return [] if self._row is None else [self._row]


class _LibraryConnection:
    """A raw connection standing in for one tenant's evidence library.

    It remembers every insert and answers the upload's reads from them.
    """

    def __init__(self, sequence: list) -> None:
        """Start with an empty library; sequence receives commits and rollbacks."""
        self.statements: list[tuple[str, object]] = []
        self.records: dict[str, tuple] = {}
        self.fail_inserts = False
        self._sequence = sequence

    def cursor(self) -> _LibraryCursor:
        """Return a cursor on this connection."""
        return _LibraryCursor(self)

    def commit(self) -> None:
        """Log the commit."""
        self._sequence.append("commit")

    def rollback(self) -> None:
        """Log the rollback."""
        self._sequence.append("rollback")

    def answer(self, statement: str, params):
        """Answer one statement: store an insert, find a record by content hash, give a creation time."""
        if statement.startswith("INSERT"):
            if self.fail_inserts:
                raise psycopg2.OperationalError("server closed the connection unexpectedly")
            self.records[params["content_hash"]] = (
                params["record_id"], params["source_system"], params["external_id"], params["record_type"],
                params["title"], params["content_hash"], _STORED_AT,
            )
            return None
        if statement.startswith("SELECT record_id, source_system"):
            return self.records.get(params["content_hash"])
        if statement.startswith("SELECT created_at"):
            return (_STORED_AT,)
        return None

    def inserts(self) -> list[dict]:
        """Return the parameters of every insert that reached this connection."""
        return [params for statement, params in self.statements if statement.startswith("INSERT")]


class _LibraryPool:
    """Stands in for the process pool: leases its one library connection and logs every lease and return."""

    def __init__(self, sequence: list) -> None:
        """Create the library connection; sequence receives every lease and return."""
        self.connection = _LibraryConnection(sequence)
        self.leases = 0
        self.outstanding = 0
        self._sequence = sequence

    def getconn(self) -> _LibraryConnection:
        """Lease the library connection."""
        self.leases += 1
        self.outstanding += 1
        self._sequence.append("lease")
        return self.connection

    def putconn(self, conn) -> None:
        """Take the library connection back."""
        self.outstanding -= 1
        self._sequence.append("lease returned")


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def sequence() -> list:
    """Return the shared log of slot, extraction, lease and transaction events, in order."""
    return []


@pytest.fixture
def pool(monkeypatch, sequence) -> _LibraryPool:
    """Put a recording library pool in place of the process pool, so the real get_conn leases from it."""
    library = _LibraryPool(sequence)
    monkeypatch.setattr(dependencies, "_pool", library)
    return library


@pytest.fixture
def audit_calls(monkeypatch) -> list:
    """Record every ledger entry the upload endpoint asks for, instead of writing it."""
    calls: list = []

    def record_ledger_entry(conn, tenant_id, **entry):
        """Record the ledger entry the endpoint asked for, with its tenant."""
        calls.append({"tenant_id": tenant_id, **entry})

    monkeypatch.setattr(evidence_router, "append_audit_entry", record_ledger_entry)
    return calls


@pytest.fixture
def spooled(monkeypatch) -> list:
    """Record every spooled file Starlette's multipart parser creates."""
    created: list = []

    class _RecordingSpool(SpooledTemporaryFile):
        """Starlette's spooled upload file, remembered so a test can see whether it was created and closed."""

        def __init__(self, *args, **kwargs) -> None:
            """Create the spooled file and remember it."""
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", _RecordingSpool)
    return created


@pytest.fixture
def admission(sequence) -> _RecordingAdmission:
    """Return two recording upload slots."""
    return _RecordingAdmission(_CAPACITY, sequence)


@pytest.fixture
def single_slot(sequence) -> _RecordingAdmission:
    """Return one recording upload slot, so a second upload is admitted only once it is released."""
    return _RecordingAdmission(1, sequence)


@pytest.fixture
def small_limits(monkeypatch) -> None:
    """Shrink the file and body limits so a small upload crosses them."""
    _shrink_intake_limits(monkeypatch, None, None)


@pytest.fixture
def short_reception(monkeypatch) -> None:
    """Shorten the reception deadline so a trickled body misses it."""
    _shorten_reception(monkeypatch, None, None)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _app_with(slots: UploadAdmission, serving_profile: str = "full"):
    """Return a fresh app of the given profile whose uploads are admitted into slots."""
    application = create_app(serving_profile=serving_profile)

    def these_slots() -> UploadAdmission:
        """Return the test's slots in place of the process's."""
        return slots

    application.dependency_overrides[get_upload_admission] = these_slots
    return application


def _text_upload(content: bytes, filename: str = "policy.txt") -> bytes:
    """Return a multipart body carrying content as a text file with record_type policy."""
    return _upload_body(content, filename)


def _pdf_upload(pdf: bytes, filename: str = "isms.pdf") -> bytes:
    """Return a multipart body carrying a PDF with record_type policy."""
    return _multipart(_file_part(pdf, filename, "application/pdf"), _part("record_type", b"policy"))


def _complete(body: bytes) -> _ScriptedBody:
    """Return a receive stream that delivers body in full."""
    return _ScriptedBody(_chunks(body))


def _response_started(sent: list[dict]) -> bool:
    """Return whether the app had begun a response."""
    return any(message["type"] == "http.response.start" for message in sent)


async def _request(app, headers: dict, receive) -> _Outcome:
    """Run one request through the app to its end, dependency teardown included, and return what was sent.

    Starlette's server-error middleware sends the 500 and then re-raises for
    the server to log; that re-raise is swallowed only once a response was
    sent, which is what a client would have seen. Cancellation propagates.
    """
    sent: list[dict] = []

    async def send(message: dict) -> None:
        """Keep every message the app sends."""
        sent.append(message)

    try:
        await app(_scope(headers), receive, send)
    except Exception:
        if not _response_started(sent):
            raise
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_headers = {name.decode().lower(): value.decode() for name, value in start["headers"]}
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return _Outcome(start["status"], response_headers, body)


def _run(coroutine):
    """Run a scenario on a fresh event loop, failing it rather than hanging if it never finishes."""
    return asyncio.run(asyncio.wait_for(coroutine, _SCENARIO_BOUND_SECONDS))


def _send(app, headers: dict, receive) -> _Outcome:
    """Run one request on a fresh event loop and return its outcome."""
    return _run(_request(app, headers, receive))


async def _hold_uploads(app, count: int) -> tuple[list[asyncio.Task], list[_GatedBody]]:
    """Start count authorised uploads and return once each is paused mid-body, holding its slot."""
    bodies = []
    for number in range(count):
        content = f"held evidence number {number}".encode("ascii")
        bodies.append(_GatedBody(_chunks(_text_upload(content), _HELD_CHUNK_BYTES), _PAUSE_AFTER_CHUNKS))
    tasks = [asyncio.create_task(_request(app, _AUTHORISED, body)) for body in bodies]
    for body in bodies:
        await asyncio.wait_for(body.waiting.wait(), _WAIT_BOUND_SECONDS)
    return tasks, bodies


async def _finish(tasks: list[asyncio.Task], bodies: list[_GatedBody]) -> list[_Outcome]:
    """Let the held uploads send the rest of their bodies and return their outcomes."""
    for body in bodies:
        body.release.set()
    return list(await asyncio.gather(*tasks))


async def _wait_for_path(path: Path) -> None:
    """Wait, without blocking the event loop, until path exists."""
    give_up_at = time.monotonic() + _WAIT_BOUND_SECONDS
    while not path.exists():
        assert time.monotonic() < give_up_at, f"{path.name} never appeared"
        await asyncio.sleep(_POLL_SECONDS)


def _sleep_until(moment: float) -> None:
    """Sleep until the monotonic clock reaches moment."""
    remaining = moment - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _use_hanging_worker(monkeypatch, started_marker: Path, survived_marker: Path) -> None:
    """Make PDF extraction start a probe that marks its start, sleeps past the deadline, then marks survival."""

    def hanging_command(limits=None) -> list[str]:
        """Return the hanging probe's argv; the worker's limits are irrelevant to it."""
        return [sys.executable, str(_PROBE_PATH), "hang", str(started_marker), str(survived_marker),
                str(_PROBE_SLEEP_SECONDS)]

    monkeypatch.setattr(pdf_text_extraction, "worker_command", hanging_command)
    monkeypatch.setattr(pdf_text_extraction, "PDF_EXTRACTION_DEADLINE_SECONDS", _SHORT_EXTRACTION_SECONDS)


def _record_worker_processes(monkeypatch) -> list:
    """Record every worker process PDF extraction starts, still starting it the real way."""
    real_start = pdf_text_extraction._start_worker
    started = []

    def recording_start(command, working_directory):
        """Start the process as extraction would and keep a handle to it."""
        process = real_start(command, working_directory)
        started.append(process)
        return process

    monkeypatch.setattr(pdf_text_extraction, "_start_worker", recording_start)
    return started


def _record_worker_directories(monkeypatch) -> list[Path]:
    """Record every private directory PDF extraction makes for a worker, still making it the real way."""
    real_create = pdf_text_extraction._create_private_directory
    directories: list[Path] = []

    def recording_create() -> str:
        """Make the directory as extraction would and remember it."""
        directory = real_create()
        directories.append(Path(directory))
        return directory

    monkeypatch.setattr(pdf_text_extraction, "_create_private_directory", recording_create)
    return directories


def _asyncio_errors(caplog) -> list[str]:
    """Return every ERROR-or-worse message the asyncio logger emitted, after collecting unreferenced tasks.

    The collection makes asyncio report, now, any finished task whose
    exception nothing ever retrieved; otherwise that report could come at an
    arbitrary later moment, outside the test.
    """
    gc.collect()
    return [record.getMessage() for record in caplog.records
            if record.name == "asyncio" and record.levelno >= logging.ERROR]


def _assert_slot_free_and_reusable(app, slots: _RecordingAdmission) -> None:
    """Assert no slot is held, then that a further upload is admitted through the freed slot and stored."""
    assert slots.active == 0
    outcome = _send(app, _AUTHORISED, _complete(_text_upload(_REUSE_TEXT)))
    assert outcome.status == 201, outcome.body
    assert slots.active == 0


# ── a. The slots themselves ──────────────────────────────────────────────────


def test_admission_refuses_at_once_beyond_its_capacity_and_admits_again_after_a_release():
    slots = UploadAdmission(_CAPACITY)
    assert slots.try_admit() is True
    assert slots.try_admit() is True
    assert slots.try_admit() is False
    assert (slots.capacity, slots.active) == (_CAPACITY, _CAPACITY)
    slots.release()
    assert slots.active == _CAPACITY - 1
    assert slots.try_admit() is True


def test_releasing_a_slot_that_was_never_admitted_is_a_programming_error():
    slots = UploadAdmission(1)
    with pytest.raises(RuntimeError):
        slots.release()
    assert slots.try_admit() is True
    slots.release()
    with pytest.raises(RuntimeError):
        slots.release()
    assert slots.active == 0


def test_an_admission_needs_at_least_one_slot():
    with pytest.raises(ValueError):
        UploadAdmission(0)


def test_threads_racing_for_the_slots_never_take_more_than_the_capacity():
    slots = UploadAdmission(_CAPACITY)
    start_together = threading.Barrier(_RACING_THREADS)
    results: list[bool] = []

    def race() -> None:
        """Wait for every thread, then try to take a slot."""
        start_together.wait()
        results.append(slots.try_admit())

    threads = [threading.Thread(target=race) for _ in range(_RACING_THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == _CAPACITY
    assert slots.active == _CAPACITY


# ── b. The reception deadline is total, not per chunk ───────────────────────


def test_a_trickled_body_is_refused_at_the_total_deadline_though_no_single_gap_is_long(
    admission, pool, spooled, audit_calls, short_reception,
):
    chunks = _chunks(_text_upload(b"trickled evidence"), _TRICKLE_CHUNK_BYTES)
    assert len(chunks) * _TRICKLE_INTERVAL_SECONDS > _SHORT_RECEPTION_SECONDS + _TIMING_ALLOWANCE_SECONDS
    body = _TrickledBody(chunks, _TRICKLE_INTERVAL_SECONDS)
    began = time.monotonic()
    outcome = _send(_app_with(admission), _AUTHORISED, body)
    elapsed = time.monotonic() - began
    assert (outcome.status, outcome.json()) == (408, {"detail": _TOO_SLOW})
    assert outcome.headers.get("connection") == "close"
    assert _SHORT_RECEPTION_SECONDS <= elapsed < _SHORT_RECEPTION_SECONDS + _TIMING_ALLOWANCE_SECONDS
    assert 0 < body.delivered < len(chunks)
    assert spooled == [], "a body that never completed must not reach the multipart parser"
    assert pool.leases == 0
    assert audit_calls == []
    assert (admission.attempts, admission.active) == (1, 0)


def test_the_same_gaps_within_the_deadline_are_accepted(admission, pool, audit_calls, short_reception):
    chunks = _chunks(_text_upload(b"unhurried evidence"), _UNHURRIED_CHUNK_BYTES)
    assert len(chunks) * _TRICKLE_INTERVAL_SECONDS < _SHORT_RECEPTION_SECONDS
    outcome = _send(_app_with(admission), _AUTHORISED, _TrickledBody(chunks, _TRICKLE_INTERVAL_SECONDS))
    assert outcome.status == 201, outcome.body
    assert [entry["action_type"] for entry in audit_calls] == ["evidence_uploaded"]
    assert admission.active == 0


def test_a_first_chunk_that_never_arrives_in_time_is_refused(admission, pool, spooled, audit_calls, short_reception):
    body = _TrickledBody([_text_upload(b"late evidence")], _SHORT_RECEPTION_SECONDS + _TIMING_ALLOWANCE_SECONDS)
    began = time.monotonic()
    outcome = _send(_app_with(admission), _AUTHORISED, body)
    elapsed = time.monotonic() - began
    assert (outcome.status, outcome.json()) == (408, {"detail": _TOO_SLOW})
    assert outcome.headers.get("connection") == "close"
    assert elapsed < _SHORT_RECEPTION_SECONDS + _TIMING_ALLOWANCE_SECONDS
    assert body.delivered == 0
    assert spooled == []
    assert pool.leases == 0
    assert admission.active == 0


# ── c. A full house refuses at once; authentication comes first ────────────


def test_a_third_upload_is_refused_at_once_while_two_are_held_mid_body(admission, pool, audit_calls):
    app = _app_with(admission)

    async def scenario() -> None:
        """Hold two uploads mid-body, send a third, then let the two finish."""
        tasks, bodies = await _hold_uploads(app, _CAPACITY)
        assert admission.active == _CAPACITY
        third_body = _complete(_text_upload(b"third evidence"))
        began = time.monotonic()
        third = await _request(app, _AUTHORISED, third_body)
        assert time.monotonic() - began < _PROMPT_SECONDS
        assert not any(task.done() for task in tasks), "the third upload must be refused while the two are pending"
        assert (third.status, third.json()) == (503, {"detail": _BUSY})
        assert third.headers.get("retry-after") == str(EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS)
        assert third.headers.get("connection") == "close"
        assert third_body.delivered == 0, "a refused upload's body must never be read"
        held = await _finish(tasks, bodies)
        assert [outcome.status for outcome in held] == [201, 201]

    _run(scenario())
    assert admission.active == 0
    assert admission.attempts == _CAPACITY + 1
    assert len(pool.connection.inserts()) == _CAPACITY
    assert len(audit_calls) == _CAPACITY


def test_the_process_admits_evidence_upload_max_active_uploads_at_once(pool, audit_calls):
    process_slots = asyncio.run(get_upload_admission())
    assert process_slots is processing._PROCESS_ADMISSION
    assert process_slots.active == 0
    app = create_app()

    async def scenario() -> None:
        """Hold the process's every slot, then send one more upload."""
        tasks, bodies = await _hold_uploads(app, EVIDENCE_UPLOAD_MAX_ACTIVE)
        refused = await _request(app, _AUTHORISED, _complete(_text_upload(b"one too many")))
        assert (refused.status, refused.json()) == (503, {"detail": _BUSY})
        held = await _finish(tasks, bodies)
        assert [outcome.status for outcome in held] == [201] * EVIDENCE_UPLOAD_MAX_ACTIVE

    _run(scenario())
    assert process_slots.active == 0


def test_unauthenticated_and_unpermitted_uploads_never_take_a_slot_even_at_a_full_house(admission, pool, audit_calls):
    app = _app_with(admission)
    refused_headers = [_headers(), _headers(token="not-a-jwt"), _headers(token=_token("auditor"))]

    async def scenario() -> None:
        """Fill the house, then send uploads without a valid token or with a read-only role."""
        tasks, bodies = await _hold_uploads(app, _CAPACITY)
        attempts_when_full = admission.attempts
        refusals = []
        for headers in refused_headers:
            body = _complete(_text_upload(b"refused evidence"))
            outcome = await _request(app, headers, body)
            refusals.append((outcome.status, body.delivered))
        assert refusals == [(401, 0), (401, 0), (403, 0)]
        assert admission.attempts == attempts_when_full == _CAPACITY
        held = await _finish(tasks, bodies)
        assert [outcome.status for outcome in held] == [201, 201]

    _run(scenario())
    assert admission.active == 0


@pytest.mark.parametrize(("headers", "status"), [
    (_headers(token=_token(), content_type="text/plain"), 415),
    (_headers(token=_token(), content_type="multipart/form-data"), 400),
    (_headers(token=_token(), **{"Content-Length": "12abc"}), 400),
    (_headers(token=_token(), **{"Content-Length": str(EVIDENCE_UPLOAD_MAX_BODY_BYTES + 1)}), 413),
])
def test_header_refusals_take_no_slot_and_keep_their_status_at_a_full_house(admission, pool, audit_calls, headers, status):
    app = _app_with(admission)

    async def scenario() -> None:
        """Fill the house, then send an upload its headers alone rule out."""
        tasks, bodies = await _hold_uploads(app, _CAPACITY)
        body = _complete(_text_upload(b"ruled out by its headers"))
        outcome = await _request(app, headers, body)
        assert outcome.status == status
        assert outcome.headers.get("connection") == "close"
        assert body.delivered == 0
        assert admission.attempts == _CAPACITY
        held = await _finish(tasks, bodies)
        assert [held_outcome.status for held_outcome in held] == [201, 201]

    _run(scenario())
    assert admission.active == 0


# ── d. The slot is released after every outcome and can be used again ───────


def test_the_slot_is_released_after_a_stored_upload(single_slot, pool, audit_calls):
    app = _app_with(single_slot)
    assert _send(app, _AUTHORISED, _complete(_text_upload(b"stored evidence"))).status == 201
    _assert_slot_free_and_reusable(app, single_slot)


@pytest.mark.parametrize(("body", "status"), [
    (_text_upload(b"y" * (_SMALL_FILE_LIMIT + 1)), 413),
    (_multipart(_file_part(b"evidence")), 422),
], ids=["file-too-large", "no-record-type"])
def test_the_slot_is_released_after_an_intake_refusal(single_slot, pool, audit_calls, small_limits, body, status):
    app = _app_with(single_slot)
    assert _send(app, _AUTHORISED, _complete(body)).status == status
    _assert_slot_free_and_reusable(app, single_slot)


def test_the_slot_is_released_after_a_reception_timeout(single_slot, pool, audit_calls, short_reception):
    app = _app_with(single_slot)
    chunks = _chunks(_text_upload(b"trickled evidence"), _TRICKLE_CHUNK_BYTES)
    assert _send(app, _AUTHORISED, _TrickledBody(chunks, _TRICKLE_INTERVAL_SECONDS)).status == 408
    _assert_slot_free_and_reusable(app, single_slot)


def test_the_slot_is_released_after_a_client_disconnect(single_slot, pool, spooled, audit_calls):
    app = _app_with(single_slot)
    body = _GatedBody(_chunks(_text_upload(b"abandoned evidence"), _HELD_CHUNK_BYTES), _PAUSE_AFTER_CHUNKS,
                      complete=False)
    body.release.set()
    outcome = _send(app, _AUTHORISED, body)
    assert (outcome.status, outcome.json()) == (400, {"detail": "upload was not received completely"})
    assert spooled == []
    assert pool.leases == 0
    _assert_slot_free_and_reusable(app, single_slot)


def test_the_slot_is_released_when_the_request_is_cancelled_mid_body(single_slot, pool, spooled, audit_calls):
    app = _app_with(single_slot)

    async def scenario() -> None:
        """Hold one upload mid-body, then cancel its request task."""
        tasks, _bodies = await _hold_uploads(app, 1)
        assert single_slot.active == 1
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]

    _run(scenario())
    assert spooled == []
    assert pool.leases == 0
    _assert_slot_free_and_reusable(app, single_slot)


def test_the_slot_is_released_after_a_pdf_is_refused(single_slot, pool, audit_calls):
    app = _app_with(single_slot)
    outcome = _send(app, _AUTHORISED, _complete(_pdf_upload(b"%PDF-1.4 this is not a real pdf")))
    assert (outcome.status, outcome.json()) == (422, {"detail": _UNREADABLE})
    _assert_slot_free_and_reusable(app, single_slot)


def test_the_slot_is_released_after_the_database_write_fails(single_slot, pool, audit_calls, sequence):
    app = _app_with(single_slot)
    pool.connection.fail_inserts = True
    outcome = _send(app, _AUTHORISED, _complete(_text_upload(b"evidence the database refuses")))
    assert (outcome.status, outcome.json()) == (500, {"detail": "internal server error"})
    assert len(pool.connection.inserts()) == 1, "the failure must come from the database write itself"
    assert sequence[-3:] == ["rollback", "lease returned", "slot released"]
    assert "commit" not in sequence
    assert audit_calls == []
    pool.connection.fail_inserts = False
    _assert_slot_free_and_reusable(app, single_slot)


# ── e. Extraction finishes before the first lease; refusals touch nothing ───


def _record_extraction(monkeypatch, sequence: list) -> None:
    """Log the start and end of text extraction and of any PDF worker run into sequence."""
    real_extract = processing.extract_text
    real_worker_run = pdf_text_extraction.extract_pdf_text

    def recording_extract(filename: str, content: bytes) -> str:
        """Extract as the endpoint would, logging the start and end."""
        sequence.append("extraction started")
        try:
            return real_extract(filename, content)
        finally:
            sequence.append("extraction finished")

    def recording_worker_run(content: bytes) -> str:
        """Run the real PDF worker, logging the start and end."""
        sequence.append("PDF worker started")
        try:
            return real_worker_run(content)
        finally:
            sequence.append("PDF worker finished")

    monkeypatch.setattr(processing, "extract_text", recording_extract)
    monkeypatch.setattr(pdf_text_extraction, "extract_pdf_text", recording_worker_run)


_ADMITTED_AND_EXTRACTED = ["slot taken", "extraction started"]
_LEASED_AFTER_EXTRACTION = ["extraction finished", "lease", "commit", "lease returned", "slot released"]


@pytest.mark.parametrize(("body", "worker_steps"), [
    (_text_upload(b"Plain text evidence."), []),
    (_pdf_upload(build_pdf([text_operations(*_PDF_TEXT_LINES)])), ["PDF worker started", "PDF worker finished"]),
], ids=["text", "pdf"])
def test_text_is_extracted_before_the_first_lease_and_the_slot_outlives_the_lease(
    admission, pool, audit_calls, sequence, monkeypatch, body, worker_steps,
):
    _record_extraction(monkeypatch, sequence)
    outcome = _send(_app_with(admission), _AUTHORISED, _complete(body))
    assert outcome.status == 201, outcome.body
    assert sequence == _ADMITTED_AND_EXTRACTED + worker_steps + _LEASED_AFTER_EXTRACTION


def _take_the_only_slot(monkeypatch, slots, tmp_path) -> None:
    """Fill a one-slot house before the request arrives."""
    assert slots.try_admit() is True


def _worker_cannot_start(monkeypatch, slots, tmp_path) -> None:
    """Make PDF extraction start an interpreter that does not exist."""
    missing_interpreter = tmp_path / "no-such-python.exe"

    def missing_command(limits=None) -> list[str]:
        """Return an argv whose executable does not exist."""
        return [str(missing_interpreter), "worker.py"]

    monkeypatch.setattr(pdf_text_extraction, "worker_command", missing_command)


def _shrink_intake_limits(monkeypatch, slots, tmp_path) -> None:
    """Shrink the file and body limits so a small upload crosses them."""
    monkeypatch.setattr(intake, "MAX_EVIDENCE_UPLOAD_BYTES", _SMALL_FILE_LIMIT)
    monkeypatch.setattr(intake, "EVIDENCE_UPLOAD_MAX_BODY_BYTES", _SMALL_BODY_LIMIT)
    monkeypatch.setattr(evidence_intake_service, "MAX_EVIDENCE_UPLOAD_BYTES", _SMALL_FILE_LIMIT)


def _shorten_reception(monkeypatch, slots, tmp_path) -> None:
    """Shorten the reception deadline so a trickled body misses it."""
    monkeypatch.setattr(intake, "EVIDENCE_UPLOAD_RECEPTION_DEADLINE_SECONDS", _SHORT_RECEPTION_SECONDS)


def _receive_stream(kind: str, body: bytes):
    """Return a receive stream that delivers body completely, trickles it, or disconnects part-way."""
    if kind == "trickled":
        return _TrickledBody(_chunks(body, _TRICKLE_CHUNK_BYTES), _TRICKLE_INTERVAL_SECONDS)
    if kind == "disconnect":
        gated = _GatedBody(_chunks(body, _HELD_CHUNK_BYTES), _PAUSE_AFTER_CHUNKS, complete=False)
        gated.release.set()
        return gated
    return _complete(body)


_OVERSIZE_TEXT = _text_upload(b"y" * (_SMALL_FILE_LIMIT + 1))


@pytest.mark.parametrize(("headers", "kind", "body", "prepare", "status"), [
    pytest.param(_headers(), "complete", _text_upload(b"anonymous"), None, 401, id="anonymous"),
    pytest.param(_headers(token=_token("auditor")), "complete", _text_upload(b"read-only"), None, 403, id="auditor"),
    pytest.param(_headers(token=_token(), content_type="application/json"), "complete", b"{}", None, 415,
                 id="not-multipart"),
    pytest.param(_headers(token=_token(), content_type="multipart/form-data"), "complete", b"--x", None, 400,
                 id="no-boundary"),
    pytest.param(_headers(token=_token(), **{"Content-Length": str(_SMALL_BODY_LIMIT + 1)}), "complete",
                 _OVERSIZE_TEXT, _shrink_intake_limits, 413, id="declared-too-large"),
    pytest.param(_AUTHORISED, "complete", _text_upload(b"z" * _SMALL_BODY_LIMIT), _shrink_intake_limits, 413,
                 id="body-too-large"),
    pytest.param(_AUTHORISED, "complete", _OVERSIZE_TEXT, _shrink_intake_limits, 413, id="file-too-large"),
    pytest.param(_AUTHORISED, "complete",
                 _multipart(_file_part(b"one"), _file_part(b"two", "two.txt"), _part("record_type", b"policy")),
                 None, 400, id="two-files"),
    pytest.param(_AUTHORISED, "complete", _multipart(_file_part(b"evidence")), None, 422, id="no-record-type"),
    pytest.param(_AUTHORISED, "complete", _text_upload(b"MZ binary", "tool.exe"), None, 422, id="unsupported-type"),
    pytest.param(_AUTHORISED, "complete", _text_upload(b"   \n\t  "), None, 422, id="no-text"),
    pytest.param(_AUTHORISED, "complete", _pdf_upload(b"%PDF-1.4 not a pdf"), None, 422, id="unreadable-pdf"),
    pytest.param(_AUTHORISED, "trickled", _text_upload(b"trickled"), _shorten_reception, 408, id="too-slow"),
    pytest.param(_AUTHORISED, "disconnect", _text_upload(b"abandoned evidence"), None, 400, id="disconnect"),
    pytest.param(_AUTHORISED, "complete", _text_upload(b"busy"), _take_the_only_slot, 503, id="busy"),
    pytest.param(_AUTHORISED, "complete", _pdf_upload(_PROBE_PDF), _worker_cannot_start, 503,
                 id="processing-unavailable"),
])
def test_a_refused_upload_leases_nothing_inserts_nothing_and_writes_no_ledger_entry(
    single_slot, pool, audit_calls, monkeypatch, tmp_path, headers, kind, body, prepare, status,
):
    if prepare is not None:
        prepare(monkeypatch, single_slot, tmp_path)
    held_before = single_slot.active
    outcome = _send(_app_with(single_slot), headers, _receive_stream(kind, body))
    assert outcome.status == status, outcome.body
    assert isinstance(outcome.json()["detail"], str)
    assert pool.leases == 0
    assert pool.connection.statements == []
    assert audit_calls == []
    assert single_slot.active == held_before


# ── f. Real PDFs through the app and the real worker ────────────────────────


def test_a_real_pdf_is_stored_hashed_and_ledgered_and_a_re_upload_is_deduplicated(admission, pool, audit_calls):
    app = _app_with(admission)
    pdf = build_pdf([text_operations(*_PDF_TEXT_LINES), None, text_operations("Annex A (Zürich)")])
    expected_text = reference_text(pdf).strip()
    expected_hash = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()

    first = _send(app, _AUTHORISED, _complete(_pdf_upload(pdf, "isms.pdf")))
    assert first.status == 201, first.body
    stored = first.json()
    assert (stored["deduplicated"], stored["source_system"], stored["external_id"], stored["title"]) == (
        False, "upload", "isms.pdf", "isms.pdf",
    )
    assert stored["content_hash"] == expected_hash
    [inserted] = pool.connection.inserts()
    assert (inserted["record_id"], inserted["tenant_id"], inserted["body"], inserted["content_hash"]) == (
        stored["record_id"], _TENANT_ID, expected_text, expected_hash,
    )
    assert audit_calls == [{
        "tenant_id": _TENANT_ID, "actor_id": _USER_ID, "actor_role": "compliance_lead",
        "action_type": "evidence_uploaded", "object_type": "context_record", "object_id": stored["record_id"],
        "control_id": None, "after_state": {"external_id": "isms.pdf", "record_type": "policy"},
    }]

    again = _send(app, _AUTHORISED, _complete(_pdf_upload(pdf, "isms-copy.pdf")))
    assert again.status == 201, again.body
    assert (again.json()["deduplicated"], again.json()["record_id"], again.json()["content_hash"]) == (
        True, stored["record_id"], expected_hash,
    )
    assert len(pool.connection.inserts()) == 1
    assert len(audit_calls) == 1
    assert admission.active == 0


@pytest.mark.parametrize(("pdf", "detail"), [
    pytest.param(b"%PDF-1.4 this is not a real pdf", _UNREADABLE, id="malformed"),
    pytest.param(build_pdf([text_operations("confidential board minutes")], user_password="pw"), _ENCRYPTED,
                 id="encrypted"),
    pytest.param(build_pdf([None] * (PDF_MAX_PAGES + 1)), _TOO_MANY_PAGES, id="501-pages"),
    pytest.param(build_pdf_with_deflated_page(deflated_text_with_padding(text_operations("bomb"),
                                                                         PDF_MAX_STREAM_OUTPUT_BYTES + 1)),
                 _OVER_LIMITS, id="stream-over-the-limit"),
])
def test_a_refused_pdf_gives_its_exact_detail_and_leaves_no_row_no_ledger_entry_and_no_lease(
    admission, pool, audit_calls, pdf, detail,
):
    outcome = _send(_app_with(admission), _AUTHORISED, _complete(_pdf_upload(pdf)))
    assert (outcome.status, outcome.json()) == (422, {"detail": detail})
    assert pool.leases == 0
    assert pool.connection.inserts() == []
    assert audit_calls == []
    assert admission.active == 0


@pytest.mark.slow
def test_a_pdf_the_real_memory_ceiling_stops_is_refused_untouched_and_its_slot_then_stores_a_normal_pdf(
    single_slot, pool, audit_calls, monkeypatch,
):
    processes = _record_worker_processes(monkeypatch)
    directories = _record_worker_directories(monkeypatch)
    app = _app_with(single_slot)
    growing = build_pdf_with_amplifying_to_unicode(_GROWING_MAPPED_CHARACTERS, _GROWING_TIMES_SHOWN)
    refused = _send(app, _AUTHORISED, _complete(_pdf_upload(growing, "grows-past-the-ceiling.pdf")))
    assert (refused.status, refused.json()) == (422, {"detail": _OVER_LIMITS})
    assert pool.leases == 0
    assert pool.connection.statements == []
    assert pool.connection.inserts() == []
    assert audit_calls == []
    assert single_slot.active == 0
    [worker] = processes
    assert worker.returncode is not None
    assert (worker.stdin.closed, worker.stdout.closed) == (True, True)
    assert not directories[0].exists()

    normal = build_pdf([text_operations(*_PDF_TEXT_LINES)])
    stored = _send(app, _AUTHORISED, _complete(_pdf_upload(normal)))
    assert stored.status == 201, stored.body
    assert single_slot.attempts == 2
    assert pool.connection.inserts()[0]["body"] == reference_text(normal).strip()
    assert [entry["action_type"] for entry in audit_calls] == ["evidence_uploaded"]
    assert single_slot.active == 0
    assert len(processes) == 2
    assert processes[1].returncode is not None


def test_a_pdf_worker_that_cannot_start_is_a_503_with_no_lease(admission, pool, audit_calls, monkeypatch, tmp_path):
    _worker_cannot_start(monkeypatch, admission, tmp_path)
    outcome = _send(_app_with(admission), _AUTHORISED, _complete(_pdf_upload(_PROBE_PDF)))
    assert (outcome.status, outcome.json()) == (503, {"detail": _UNAVAILABLE})
    assert pool.leases == 0
    assert audit_calls == []
    assert admission.active == 0


# ── g. A PDF worker past its deadline is killed and reaped before the slot frees


@pytest.mark.slow
def test_a_pdf_worker_past_the_deadline_is_killed_and_reaped_before_the_slot_is_released(
    pool, audit_calls, sequence, monkeypatch, tmp_path,
):
    started_marker, survived_marker = tmp_path / "started", tmp_path / "survived"
    _use_hanging_worker(monkeypatch, started_marker, survived_marker)
    processes = _record_worker_processes(monkeypatch)
    slots = _RecordingAdmission(_CAPACITY, sequence, observe_release=lambda: processes[0].returncode)
    began = time.monotonic()
    outcome = _send(_app_with(slots), _AUTHORISED, _complete(_pdf_upload(_PROBE_PDF)))
    elapsed = time.monotonic() - began
    assert (outcome.status, outcome.json()) == (422, {"detail": _TIMED_OUT})
    assert _SHORT_EXTRACTION_SECONDS <= elapsed < _SHORT_EXTRACTION_SECONDS + _KILL_ALLOWANCE_SECONDS
    assert started_marker.exists()
    assert len(processes) == 1
    assert len(slots.release_observations) == 1
    assert slots.release_observations[0] is not None, "the slot was released before the worker was reaped"
    assert slots.active == 0
    assert pool.leases == 0
    assert audit_calls == []
    _sleep_until(began + _PROBE_SLEEP_SECONDS + _SURVIVAL_MARGIN_SECONDS)
    assert not survived_marker.exists(), "the worker ran on after its deadline"


@pytest.mark.slow
def test_a_request_cancelled_while_its_pdf_worker_runs_ends_only_after_the_worker_is_reaped(
    pool, audit_calls, sequence, monkeypatch, tmp_path, caplog,
):
    caplog.set_level(logging.WARNING, logger="asyncio")
    started_marker, survived_marker = tmp_path / "started", tmp_path / "survived"
    _use_hanging_worker(monkeypatch, started_marker, survived_marker)
    processes = _record_worker_processes(monkeypatch)
    slots = _RecordingAdmission(_CAPACITY, sequence, observe_release=lambda: processes[0].returncode)
    app = _app_with(slots)
    began = time.monotonic()

    async def scenario() -> float:
        """Cancel the upload while its worker runs; return when the request task ended."""
        task = asyncio.create_task(_request(app, _AUTHORISED, _complete(_pdf_upload(_PROBE_PDF))))
        await _wait_for_path(started_marker)
        task.cancel()
        await asyncio.sleep(_HELD_AFTER_CANCEL_SECONDS)
        assert not task.done(), "the cancelled request did not wait for its worker"
        assert slots.active == 1, "the slot was released while the worker still ran"
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic()

    ended_at = _run(scenario())
    assert ended_at - began >= _SHORT_EXTRACTION_SECONDS
    assert len(processes) == 1
    assert processes[0].returncode is not None
    assert len(slots.release_observations) == 1
    assert slots.release_observations[0] is not None, "the slot was released before the worker was reaped"
    assert slots.active == 0
    assert pool.leases == 0
    assert audit_calls == []
    # The worker's refusal (its deadline passed) lands after the cancellation:
    # the one-line refusal log is the whole record, not an asyncio ERROR.
    assert _asyncio_errors(caplog) == []
    _sleep_until(began + _PROBE_SLEEP_SECONDS + _SURVIVAL_MARGIN_SECONDS)
    assert not survived_marker.exists(), "the worker ran on after the request was cancelled"


# ── h. The pilot app admits, bounds and stores the same way ─────────────────


def test_the_pilot_app_admits_bounds_and_stores_uploads_the_same_way(
    single_slot, pool, audit_calls, monkeypatch, tmp_path, short_reception,
):
    app = _app_with(single_slot, serving_profile="pilot")
    assert app.state.serving_profile == "pilot"

    async def scenario() -> None:
        """Hold the one slot, send a second upload, then let the first finish."""
        tasks, bodies = await _hold_uploads(app, 1)
        refused = await _request(app, _AUTHORISED, _complete(_text_upload(b"second")))
        assert (refused.status, refused.json()) == (503, {"detail": _BUSY})
        assert refused.headers.get("retry-after") == str(EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS)
        assert [outcome.status for outcome in await _finish(tasks, bodies)] == [201]

    _run(scenario())
    with monkeypatch.context() as shrunk:
        _shrink_intake_limits(shrunk, single_slot, tmp_path)
        oversize = _send(app, _AUTHORISED, _complete(_OVERSIZE_TEXT))
    assert oversize.status == 413
    trickled = _send(app, _AUTHORISED, _receive_stream("trickled", _text_upload(b"trickled")))
    assert (trickled.status, trickled.json()) == (408, {"detail": _TOO_SLOW})
    pdf = build_pdf([text_operations(*_PDF_TEXT_LINES)])
    stored = _send(app, _AUTHORISED, _complete(_pdf_upload(pdf)))
    assert stored.status == 201, stored.body
    assert pool.connection.inserts()[-1]["body"] == reference_text(pdf).strip()
    assert [entry["action_type"] for entry in audit_calls] == ["evidence_uploaded", "evidence_uploaded"]
    assert single_slot.active == 0
