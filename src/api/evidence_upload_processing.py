"""evidence_upload_processing.py — admission and bounded processing for POST /api/v1/evidence (PILOT-READY-004, G6).

What:  a FastAPI dependency that admits one evidence upload into a fixed
       number of slots for this API process, receives it under the
       SEC-REMED-003 limits and the reception deadline
       (evidence_upload_intake), reads its text in a worker thread (a PDF in
       a separate, killable worker process with its own limits and deadline,
       src/services/pdf_text_extraction.py), and hands the endpoint the
       processed upload. The slot is held through reception, parsing,
       extraction and the endpoint's database work, and released on every
       outcome.
Why:   gate G6. Without a cap, any number of authorised clients could each
       hold a request, its memory and its threads open at once; without a
       deadline at every stage, one slow body or one hostile PDF could hold
       its slot for ever. A full house is refused at once (503 with
       Retry-After) rather than queued, because a queue of waiting requests
       is the exhaustion this exists to prevent. The count is PER API
       PROCESS: each uvicorn worker process admits EVIDENCE_UPLOAD_MAX_ACTIVE
       of its own.
How:   the upload endpoint declares Depends(accept_evidence_upload) after its
       token and role dependencies and before its connection dependency.
       Tests replace the slots with
       app.dependency_overrides[get_upload_admission].
       pytest tests/unit/api/test_evidence_upload_admission.py -v
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from config.constants import EVIDENCE_UPLOAD_MAX_ACTIVE, EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS
from src.api.evidence_upload_intake import receive_evidence_upload, refuse_by_headers
from src.exceptions import (
    EvidenceExtractionError,
    EvidenceProcessingUnavailableError,
    UnsupportedFileTypeError,
)
from src.services.evidence_intake import extract_text

_UPLOADS_BUSY = "evidence uploads are busy; try again shortly"
# Refused before any body byte is read, so the connection cannot carry another
# request; the client is told to close it rather than stream the body into it.
_BUSY_HEADERS = {
    "Retry-After": str(EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS),
    "Connection": "close",
}


class UploadAdmission:
    """A fixed number of upload slots for one API process; admission never waits."""

    def __init__(self, capacity: int) -> None:
        """Create capacity empty slots; capacity must be at least one."""
        if capacity < 1:
            raise ValueError("an upload admission needs at least one slot")
        self._capacity = capacity
        self._active = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        """Return how many uploads may be admitted at once."""
        return self._capacity

    @property
    def active(self) -> int:
        """Return how many uploads hold a slot now."""
        with self._lock:
            return self._active

    def try_admit(self) -> bool:
        """Take a slot and return True, or return False at once when every slot is taken."""
        with self._lock:
            if self._active >= self._capacity:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        """Give back one slot; releasing more than were admitted is a programming error (RuntimeError)."""
        with self._lock:
            if self._active == 0:
                raise RuntimeError("an upload slot was released that was never admitted")
            self._active -= 1


# One per API process (each uvicorn worker process has its own), never shared
# between processes.
_PROCESS_ADMISSION = UploadAdmission(EVIDENCE_UPLOAD_MAX_ACTIVE)


async def get_upload_admission() -> UploadAdmission:
    """Return this process's upload slots (EVIDENCE_UPLOAD_MAX_ACTIVE of them)."""
    return _PROCESS_ADMISSION


@dataclass(frozen=True)
class ProcessedEvidenceUpload:
    """One admitted, received and extracted upload: plain values, no open file."""

    filename: str
    record_type: str
    title: str | None
    text: str


async def accept_evidence_upload(
    request: Request, admission: UploadAdmission = Depends(get_upload_admission)
) -> AsyncIterator[ProcessedEvidenceUpload]:
    """Admit, receive and extract one upload, holding its slot until the endpoint has finished.

    What the headers alone rule out (415, 400, 413: refuse_by_headers) is
    refused first, without a slot, so it gets its own status even at a full
    house. A full house is then refused at once with 503, Retry-After and
    Connection: close, before any body byte is read. Otherwise the slot is
    taken, the upload is received (receive_evidence_upload's refusals), its
    text is read
    off the event loop (_extract_in_worker_thread's refusals), and the
    processed upload is yielded to the endpoint. FastAPI resumes this
    generator when the endpoint's dependencies are torn down, after the
    endpoint's connection has been returned, so the finally clause releases
    the slot after every outcome: success, refusal, timeout, disconnect,
    cancellation and a failure in the endpoint itself.
    """
    refuse_by_headers(request.headers)
    if not admission.try_admit():
        raise HTTPException(status_code=503, detail=_UPLOADS_BUSY, headers=_BUSY_HEADERS)
    try:
        upload = await receive_evidence_upload(request)
        text = await _extract_in_worker_thread(upload.filename, upload.content)
        yield ProcessedEvidenceUpload(
            filename=upload.filename, record_type=upload.record_type, title=upload.title, text=text,
        )
    finally:
        admission.release()


async def _extract_in_worker_thread(filename: str, content: bytes) -> str:
    """Return the document's text, read in a worker thread that this request never abandons.

    run_in_threadpool alone protects the thread from anyio cancellation but
    not from a native asyncio Task.cancel(), which abandons it (verified with
    anyio 4.14.1): the slot would then be released while a PDF worker process
    still runs. So the thread runs in a task of its own, awaited through
    asyncio.wait, which never cancels the task it waits on and removes its
    own completion callback when it returns or is cancelled. A cancelled
    request first waits for that task to finish (at most the PDF extraction
    deadline, after which the worker has been killed and reaped) and only
    then lets its cancellation continue; otherwise the task's text is
    returned, or its refusal raised. asyncio.shield is deliberately not
    used: once the request is cancelled it leaves a callback on the task that
    logs the task's later refusal as an asyncio ERROR with a traceback
    (verified on Python 3.14.3), outside the one-line category log of each
    refusal.
    """
    extraction = asyncio.ensure_future(run_in_threadpool(_extract_or_refuse, filename, content))
    try:
        await asyncio.wait({extraction})
    except asyncio.CancelledError:
        await _outlast(extraction)
        raise
    return extraction.result()


async def _outlast(extraction: asyncio.Future) -> None:
    """Wait until extraction has finished, whatever cancellation arrives meanwhile; its outcome is discarded.

    The shielded anyio scope stops an anyio cancel scope from re-delivering
    its cancellation on every loop iteration while this waits; a native
    cancellation is caught and the wait resumed. The outcome is then
    retrieved, because the cancelled request will never read it, and asyncio
    reports a task whose exception was never retrieved as an ERROR when the
    task is collected.
    """
    with anyio.CancelScope(shield=True):
        while not extraction.done():
            try:
                await asyncio.wait({extraction})
            except asyncio.CancelledError:
                continue
    if not extraction.cancelled():
        extraction.exception()


def _extract_or_refuse(filename: str, content: bytes) -> str:
    """Return the document's text, translating each extraction failure to its HTTP refusal.

    Blocking (a PDF waits for its worker process): runs in a worker thread.
    Unsupported type and unreadable or refused content are the caller's
    problem (422, string detail); an oversize upload is 413, the honest
    status for a payload refused on size alone; a PDF worker that could not
    run is the server's fault (503).
    """
    try:
        return extract_text(filename, content)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except EvidenceExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except EvidenceProcessingUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))
