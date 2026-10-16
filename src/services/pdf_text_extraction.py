"""
pdf_text_extraction.py

What:  Reads an uploaded PDF's text in a separate worker process
       (pdf_text_worker.py) under a wall-clock deadline. One thread feeds the
       worker the document, another collects its output in bounded chunks,
       and the calling thread waits for the worker to exit. The worker first
       announces, in a ready line, that its OS ceiling and parser checks are
       in place, then writes one result line. A result is trusted only after
       a ready line reporting an enforced OS ceiling and a pypdf at or above
       PDF_PARSER_MINIMUM_VERSION, and only when it repeats that ceiling and
       version; it is then turned into the text or the refusal the API shows.
       A worker that announced it was ready and then exited without a
       complete result died on the document, and the document is refused as
       over the limits.
Why:   A hostile PDF parsed inside the API process could hold a request
       thread and its database connection indefinitely or exhaust the
       process's memory. A child process can be killed at a deadline, capped
       by the operating system, started with no credentials, and kept from
       making this process buffer without bound (PILOT-READY-004, G6,
       amendments R1 to R4). It runs in a private directory made for it
       alone and removed once it has been reaped, and its Python search path
       keeps only absolute entries, so nothing in a shared temporary
       directory can be imported into it (SEC-G6-02). On POSIX it leads a
       process group of its own, and a kill reaches the whole group, so
       nothing it started outlives it (SEC-G6-01; on Windows its Job Object
       lets it start nothing). The ready line keeps the status honest: a
       document that crashes the worker is a 422, while a worker that cannot
       get ready is a 503. pypdf is never imported in this process.
How:   extract_pdf_text(content) blocks until the worker has exited, been
       waited for, both pipe threads have been joined, both pipes closed and
       its directory removed: call it from a worker thread, never on the
       event loop. Tests swap worker_command, _start_worker,
       _start_pipe_thread, _create_private_directory or the module's limit
       constants with monkeypatch.
       pytest tests/unit/services/test_pdf_text_extraction.py
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from config.constants import (
    MAX_EVIDENCE_UPLOAD_BYTES,
    MILLISECONDS_PER_SECOND,
    PDF_EXTRACTION_DEADLINE_SECONDS,
    PDF_MAX_DECODED_BYTES,
    PDF_MAX_EXTRACTED_TEXT_CHARACTERS,
    PDF_MAX_PAGES,
    PDF_MAX_STREAM_OUTPUT_BYTES,
    PDF_PARSER_MINIMUM_VERSION,
    PDF_WORKER_CPU_LIMIT_SECONDS,
    PDF_WORKER_HEADER_MAX_BYTES,
    PDF_WORKER_MAX_OUTPUT_BYTES,
    PDF_WORKER_MEMORY_LIMIT_BYTES,
    PDF_WORKER_OUTPUT_READ_CHUNK_BYTES,
)
from src.exceptions import EvidenceExtractionError, EvidenceProcessingUnavailableError
from src.services import pdf_text_worker
from src.services.pdf_text_worker import ResultHeader

logger = logging.getLogger(__name__)

WORKER_PATH: Path = Path(pdf_text_worker.__file__).resolve()

# The 422 details, shown by the dashboard as-is. None carries parser output,
# document text or a path.
PDF_UNREADABLE_DETAIL = "the PDF could not be read"
PDF_ENCRYPTED_DETAIL = "encrypted PDFs are not supported"
PDF_TOO_MANY_PAGES_DETAIL = f"the PDF has more than {PDF_MAX_PAGES} pages"
PDF_OVER_LIMITS_DETAIL = "the PDF exceeds the text-extraction limits"
PDF_TIMED_OUT_DETAIL = "the PDF could not be processed within the time limit"
# The 503 detail: the worker could not start or gave no result this process
# can trust, which is the server's fault, not the document's.
PDF_PROCESSING_UNAVAILABLE_DETAIL = "evidence processing is unavailable; try again later"

_REFUSAL_DETAILS: dict[str, str] = {
    pdf_text_worker.RESULT_UNREADABLE: PDF_UNREADABLE_DETAIL,
    pdf_text_worker.RESULT_ENCRYPTED: PDF_ENCRYPTED_DETAIL,
    pdf_text_worker.RESULT_TOO_MANY_PAGES: PDF_TOO_MANY_PAGES_DETAIL,
    pdf_text_worker.RESULT_OVER_LIMITS: PDF_OVER_LIMITS_DETAIL,
}

# The only environment variables the worker inherits. An allowlist, never a
# denylist, so a credential added to the API's environment later cannot reach
# the worker by default. Windows Python needs SYSTEMROOT to start and APPDATA
# to find user site-packages, where pypdf may be installed. The Python paths
# among them are passed only in their absolute form (worker_environment).
WORKER_ENVIRONMENT_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUSERBASE",
    "VIRTUAL_ENV",
)

# Python resolves a relative or empty PYTHONPATH entry against the worker's
# working directory and puts it ahead of the standard library, whatever -P
# says; PYTHONHOME and PYTHONUSERBASE steer where the standard library and
# user site-packages are found. So PYTHONPATH keeps only its absolute entries
# and the other two pass only when absolute.
_SEARCH_PATH_VARIABLE = "PYTHONPATH"
_ABSOLUTE_ONLY_VARIABLES: tuple[str, ...] = ("PYTHONHOME", "PYTHONUSERBASE")

# -P keeps the worker's own directory (src/services) off sys.path, so no
# module beside it can shadow one pypdf imports. Its working directory is a
# fresh, empty directory only this user can enter (tempfile.mkdtemp), made
# for one run and removed after it, so neither the repository and its .env
# nor anything another user put in the shared temporary directory is in
# reach. A directory left behind by a removal that failed is recognisable by
# this prefix.
_SAFE_PATH_FLAG = "-P"
_PRIVATE_DIRECTORY_PREFIX = "kerno-pdf-worker-"
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_WINDOWS_PLATFORM = "win32"
_WORKER_EXIT_SUCCESS = 0
_WRITER_THREAD_NAME = "pdf-worker-stdin-writer"
_READER_THREAD_NAME = "pdf-worker-stdout-reader"
_TIMEOUT_CATEGORY = "timeout"
_DIED_WHILE_PARSING_CATEGORY = "died-while-parsing"
_LAUNCH_FAILED_CATEGORY = "launch-failed"
_DIRECTORY_FAILED_CATEGORY = "working-directory-failed"
_CLEANUP_FAILED_CATEGORY = "working-directory-not-removed"
_WORKER_FAILED_CATEGORY = "worker-failed"
_MALFORMED_RESULT_CATEGORY = "malformed-result"
_OUTPUT_OVERFLOW_CATEGORY = "output-overflow"
_OUTPUT_READ_FAILED_CATEGORY = "output-read-failed"
_WORKER_UNAVAILABLE_CATEGORY = "worker-unavailable"
_NO_CEILING_CATEGORY = "no-ceiling"
_PARSER_VERSION_CATEGORY = "parser-version"
# Logged for the ceiling and version when no trusted header was read.
_NOT_REPORTED = "none"


@dataclass(frozen=True)
class _WorkerRun:
    """What one finished worker run left behind: its exit, whether it timed out, and its output."""

    exit_status: int | None
    timed_out: bool
    pipe_failure: str | None
    output: bytearray


def extract_pdf_text(content: bytes) -> str:
    """Return the joined page text of a PDF, read by a bounded, killable worker process.

    Blocking. On every path the worker has exited, been waited for, both pipe
    threads have been joined, both pipes closed and the worker's private
    directory removed before this returns or raises. Raises
    EvidenceExtractionError with one of the PDF_*_DETAIL
    messages when the document is refused, including when it is not read by
    PDF_EXTRACTION_DEADLINE_SECONDS or the worker dies after announcing it
    was ready, and EvidenceProcessingUnavailableError when the worker could
    not start or gave no result this process trusts. The text is returned
    unstripped; the caller normalises it.
    """
    started_at = time.monotonic()
    run = _run_worker(content, started_at)
    return _interpret_result(run, started_at)


def worker_limits() -> dict[str, int | str]:
    """Return the worker's limits and parser minimum from the config constants, read at call time."""
    return {
        pdf_text_worker.MAX_INPUT_BYTES_KEY: MAX_EVIDENCE_UPLOAD_BYTES,
        pdf_text_worker.MAX_PAGES_KEY: PDF_MAX_PAGES,
        pdf_text_worker.MAX_STREAM_OUTPUT_BYTES_KEY: PDF_MAX_STREAM_OUTPUT_BYTES,
        pdf_text_worker.MAX_DECODED_BYTES_KEY: PDF_MAX_DECODED_BYTES,
        pdf_text_worker.MAX_TEXT_CHARACTERS_KEY: PDF_MAX_EXTRACTED_TEXT_CHARACTERS,
        pdf_text_worker.MEMORY_LIMIT_BYTES_KEY: PDF_WORKER_MEMORY_LIMIT_BYTES,
        pdf_text_worker.CPU_LIMIT_SECONDS_KEY: PDF_WORKER_CPU_LIMIT_SECONDS,
        pdf_text_worker.PARSER_MINIMUM_VERSION_KEY: PDF_PARSER_MINIMUM_VERSION,
    }


def worker_command(limits: dict[str, int | str] | None = None) -> list[str]:
    """Return the worker's argv: this interpreter, -P, the worker file and the limits as JSON.

    The limits default to worker_limits(). Raises
    EvidenceProcessingUnavailableError when this interpreter's own path is
    unknown, since there is then nothing safe to start.
    """
    if not sys.executable:
        raise EvidenceProcessingUnavailableError(PDF_PROCESSING_UNAVAILABLE_DETAIL)
    chosen_limits = worker_limits() if limits is None else limits
    return [sys.executable, _SAFE_PATH_FLAG, str(WORKER_PATH), json.dumps(chosen_limits)]


def worker_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the allowlisted variables of environ (default os.environ) that are present, their Python paths made absolute.

    PYTHONPATH keeps only its absolute entries, in their order, and is left
    out when none remain; PYTHONHOME and PYTHONUSERBASE are left out unless
    absolute. A relative or empty entry would be resolved against the
    worker's working directory and searched before the standard library,
    so a module planted there would run in the worker before its ceiling
    (SEC-G6-02).
    """
    source = os.environ if environ is None else environ
    allowed = {name: source[name] for name in WORKER_ENVIRONMENT_ALLOWLIST if name in source}
    search_path = _absolute_entries(allowed.pop(_SEARCH_PATH_VARIABLE, ""))
    if search_path:
        allowed[_SEARCH_PATH_VARIABLE] = search_path
    for name in _ABSOLUTE_ONLY_VARIABLES:
        if name in allowed and not os.path.isabs(allowed[name]):
            del allowed[name]
    return allowed


def _absolute_entries(search_path: str) -> str:
    """Return a search path with only its absolute entries, in order; relative and empty entries are dropped."""
    entries = [entry for entry in search_path.split(os.pathsep) if os.path.isabs(entry)]
    return os.pathsep.join(entries)


def _run_worker(content: bytes, started_at: float) -> _WorkerRun:
    """Run the worker in a private directory made for this run, and remove the directory once the worker is reaped."""
    working_directory = _make_working_directory(started_at)
    try:
        return _run_worker_in(working_directory, content, started_at)
    finally:
        _remove_working_directory(working_directory)


def _run_worker_in(working_directory: str, content: bytes, started_at: float) -> _WorkerRun:
    """Start the worker, feed and drain it on two threads, and wait for it until the deadline.

    However this ends (exit, deadline, output overflow, or an exception or
    cancellation in this thread), the finally clause kills the worker if it
    is still running, waits for it, and joins both threads; leaving the
    Popen context then closes both pipes. Only then does this return or raise.
    """
    with _launch_worker(working_directory, started_at) as process:
        pipes = _WorkerPipes(process, content)
        try:
            pipes.start()
            timed_out = not _exited_by_deadline(process)
        finally:
            try:
                _kill_and_reap(process)
            finally:
                pipes.join()
    return _WorkerRun(process.returncode, timed_out, pipes.failure, pipes.output)


def _make_working_directory(started_at: float) -> str:
    """Return a new private directory for one worker run, raising EvidenceProcessingUnavailableError if none can be made."""
    try:
        return _create_private_directory()
    except OSError as exc:
        raise _unavailable(_DIRECTORY_FAILED_CATEGORY, started_at, None) from exc


def _create_private_directory() -> str:
    """Create and return a new, empty directory only this user can enter, under the temporary directory."""
    return tempfile.mkdtemp(prefix=_PRIVATE_DIRECTORY_PREFIX)


def _remove_working_directory(working_directory: str) -> None:
    """Remove a worker's directory and anything in it; a failure is logged, never raised over the run's outcome.

    Called only once the worker has been reaped, so nothing of it still
    holds the directory open.
    """
    try:
        shutil.rmtree(working_directory)
    except OSError:
        logger.warning("PDF text extraction cleanup: category=%s", _CLEANUP_FAILED_CATEGORY)


def _launch_worker(working_directory: str, started_at: float) -> subprocess.Popen[bytes]:
    """Start the worker in working_directory, raising EvidenceProcessingUnavailableError if it cannot be started."""
    try:
        return _start_worker(worker_command(), working_directory)
    except (OSError, EvidenceProcessingUnavailableError) as exc:
        raise _unavailable(_LAUNCH_FAILED_CATEGORY, started_at, None) from exc


def _start_worker(command: list[str], working_directory: str) -> subprocess.Popen[bytes]:
    """Start a process with stdin and stdout piped, stderr discarded, the worker environment, in working_directory.

    On POSIX it starts a new session, so it leads a process group of its own
    that the kill path can end whole; Windows ignores the flag, and there the
    worker's own Job Object lets it start no other process.
    """
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=worker_environment(),
        cwd=working_directory,
        close_fds=True,
        creationflags=_CREATE_NO_WINDOW,
        start_new_session=True,
    )


def _exited_by_deadline(process: subprocess.Popen[bytes]) -> bool:
    """Wait for the worker to exit and return True, or return False once the deadline has passed."""
    try:
        process.wait(timeout=PDF_EXTRACTION_DEADLINE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def _kill_and_reap(process: subprocess.Popen[bytes], platform: str | None = None) -> None:
    """Kill the worker if it is still running, then wait for it, so it never outlives the request."""
    _kill_if_running(process, platform)
    process.wait()


def _kill_if_running(process: subprocess.Popen[bytes], platform: str | None = None) -> None:
    """Kill a worker that has not exited; on POSIX first its whole process group, so nothing it started survives it.

    The group is signalled only once poll() has found the worker, its
    leader, not yet reaped, so the group id still names the worker's own
    group: the same guard subprocess's send_signal relies on. platform
    defaults to sys.platform; tests name another.
    """
    if process.poll() is not None:
        return
    chosen_platform = sys.platform if platform is None else platform
    if chosen_platform != _WINDOWS_PLATFORM:
        _kill_process_group(process.pid)
    process.kill()


def _kill_process_group(group_id: int) -> None:
    """Send SIGKILL to every process in the group the worker leads; a signal that cannot be sent is ignored.

    A group already gone (ProcessLookupError) has nothing left to kill. No
    other failure may stop the caller from then killing the worker itself
    and waiting for it, so every OSError is ignored here.
    """
    try:
        os.killpg(group_id, signal.SIGKILL)
    except OSError:
        pass


def _start_pipe_thread(target: Callable[[], None], name: str) -> threading.Thread:
    """Start one daemon thread running target and return it; tests wrap this to see every thread."""
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


def _close_quietly(stream: BinaryIO) -> None:
    """Close a pipe, ignoring the error a pipe whose other end has gone raises on its final flush."""
    try:
        stream.close()
    except OSError:
        pass


class _WorkerPipes:
    """The two threads that feed the worker its document and collect its bounded result.

    The writer ignores a pipe the worker has closed: a worker that answers
    before reading everything, or is killed, leaves nothing to write to. The
    reader keeps at most PDF_WORKER_MAX_OUTPUT_BYTES (both header lines and
    the largest text); past that it stops
    reading and kills the worker, so a misbehaving worker cannot make this
    process buffer without bound. Both threads end once the worker has
    exited, so join() always returns after the worker is reaped.
    """

    def __init__(self, process: subprocess.Popen[bytes], content: bytes) -> None:
        """Prepare, without starting, the threads for one worker and one document."""
        self._process = process
        self._content = content
        self._threads: list[threading.Thread] = []
        self.output = bytearray()
        self.failure: str | None = None

    def start(self) -> None:
        """Start the writer, then the reader; a thread that failed to start is never joined."""
        self._threads.append(_start_pipe_thread(self._feed_document, _WRITER_THREAD_NAME))
        self._threads.append(_start_pipe_thread(self._collect_result, _READER_THREAD_NAME))

    def join(self) -> None:
        """Wait for every started thread to finish."""
        for thread in self._threads:
            thread.join()

    def _feed_document(self) -> None:
        """Write the whole document to the worker's stdin, then close it so the worker sees the end."""
        stdin = self._process.stdin
        try:
            stdin.write(self._content)
        except OSError:
            pass
        finally:
            _close_quietly(stdin)

    def _collect_result(self) -> None:
        """Read the worker's stdout in chunks until it ends, stopping and killing the worker past the cap."""
        stdout = self._process.stdout
        try:
            while True:
                chunk = stdout.read(PDF_WORKER_OUTPUT_READ_CHUNK_BYTES)
                if not chunk:
                    return
                if len(self.output) + len(chunk) > PDF_WORKER_MAX_OUTPUT_BYTES:
                    self._give_up(_OUTPUT_OVERFLOW_CATEGORY)
                    return
                self.output += chunk
        except OSError:
            self._give_up(_OUTPUT_READ_FAILED_CATEGORY)

    def _give_up(self, category: str) -> None:
        """Record why the output cannot be trusted, then kill the worker (and on POSIX its group) so it cannot write on."""
        self.failure = category
        _kill_if_running(self._process)


def _interpret_result(run: _WorkerRun, started_at: float) -> str:
    """Return the text of a trusted RESULT_TEXT result, or raise what the run or its output names.

    A timeout is the document's refusal, and so is a worker that announced
    it was ready (ceiling verified, parser checked, document next) and then
    exited without a complete result: it died on the document. Everything
    else that is not a well-formed result repeating a trusted ready line (a
    nonzero exit, too much output, no ready line, a malformed or mismatched
    header, an unavailable worker, bytes after a refusal, text that does not
    decode) is the worker failing, never a verdict on the document.
    """
    if run.timed_out:
        raise _refused(_TIMEOUT_CATEGORY, PDF_TIMED_OUT_DETAIL, started_at, run.exit_status)
    if run.pipe_failure is not None:
        raise _unavailable(run.pipe_failure, started_at, run.exit_status)
    ready, after_ready = _split_header(run.output, pdf_text_worker.parse_ready_line)
    if ready is None:
        raise _failure_before_ready(run, started_at)
    _require_trusted_header(ready, started_at, run.exit_status)
    return _interpret_after_ready(run, ready, after_ready, started_at)


def _split_header(
    output: bytearray, parse: Callable[[bytes], ResultHeader | None]
) -> tuple[ResultHeader | None, bytearray]:
    """Return the first line as parse reads it (None if absent, too long or malformed) and the bytes after it."""
    head, separator, rest = output.partition(pdf_text_worker.RESULT_SEPARATOR)
    if not separator or len(head) + len(separator) > PDF_WORKER_HEADER_MAX_BYTES:
        return None, rest
    return parse(bytes(head)), rest


def _failure_before_ready(run: _WorkerRun, started_at: float) -> EvidenceProcessingUnavailableError:
    """Return the 503 for a worker that never announced it was ready, and so never parsed the document.

    Its only trustworthy answer is a lone, well-formed RESULT_UNAVAILABLE:
    the worker refusing on its own account. A nonzero exit is the worker
    failing; anything else, a verdict included, is malformed, because the
    real worker decides nothing about a document before its ready line.
    """
    if run.exit_status != _WORKER_EXIT_SUCCESS:
        return _unavailable(_WORKER_FAILED_CATEGORY, started_at, run.exit_status)
    header, payload = _split_header(run.output, pdf_text_worker.parse_result_header)
    if header is not None and header.outcome == pdf_text_worker.RESULT_UNAVAILABLE and not payload:
        return _unavailable(_WORKER_UNAVAILABLE_CATEGORY, started_at, run.exit_status, header)
    return _unavailable(_MALFORMED_RESULT_CATEGORY, started_at, run.exit_status, header)


def _interpret_after_ready(run: _WorkerRun, ready: ResultHeader, after_ready: bytearray, started_at: float) -> str:
    """Return the text, or raise what a worker that announced a trusted ready line earned with what followed.

    No complete result line after it means the worker died on the document
    (stopped by its ceiling or the OS, crashed, or exited with nothing more
    to say): refused as over the limits. The exception is exit 0 with a
    partial line, a worker claiming success with a malformed result. A
    complete result line is judged as before, and must repeat the ready
    line's ceiling and pypdf version.
    """
    if pdf_text_worker.RESULT_SEPARATOR not in after_ready:
        if after_ready and run.exit_status == _WORKER_EXIT_SUCCESS:
            raise _unavailable(_MALFORMED_RESULT_CATEGORY, started_at, run.exit_status, ready)
        raise _refused(_DIED_WHILE_PARSING_CATEGORY, PDF_OVER_LIMITS_DETAIL, started_at, run.exit_status, ready)
    if run.exit_status != _WORKER_EXIT_SUCCESS:
        raise _unavailable(_WORKER_FAILED_CATEGORY, started_at, run.exit_status, ready)
    header, payload = _split_header(after_ready, pdf_text_worker.parse_result_header)
    if header is None or not _same_ceiling_and_version(header, ready):
        raise _unavailable(_MALFORMED_RESULT_CATEGORY, started_at, run.exit_status, ready)
    return _accept_result(header, payload, started_at, run.exit_status)


def _same_ceiling_and_version(header: ResultHeader, ready: ResultHeader) -> bool:
    """Return whether a result line reports the same ceiling and pypdf version as the ready line before it."""
    return header.ceiling == ready.ceiling and header.parser_version == ready.parser_version


def _accept_result(header: ResultHeader, payload: bytearray, started_at: float, exit_status: int | None) -> str:
    """Return a trusted text result's text or raise a trusted refusal; anything else is the 503."""
    _require_trusted_header(header, started_at, exit_status)
    if header.outcome == pdf_text_worker.RESULT_TEXT:
        return _decode_text(payload, started_at, exit_status, header)
    if payload:
        raise _unavailable(_MALFORMED_RESULT_CATEGORY, started_at, exit_status, header)
    detail = _REFUSAL_DETAILS[header.outcome]
    raise _refused(header.outcome, detail, started_at, exit_status, header)


def _require_trusted_header(header: ResultHeader, started_at: float, exit_status: int | None) -> None:
    """Raise the 503 unless a ready or result line reports an enforced ceiling and a recent enough pypdf.

    A result line must also carry a verdict rather than RESULT_UNAVAILABLE.
    """
    if header.outcome == pdf_text_worker.RESULT_UNAVAILABLE:
        raise _unavailable(_WORKER_UNAVAILABLE_CATEGORY, started_at, exit_status, header)
    if header.ceiling not in pdf_text_worker.ENFORCED_CEILINGS:
        raise _unavailable(_NO_CEILING_CATEGORY, started_at, exit_status, header)
    if not pdf_text_worker.version_at_least(header.parser_version, PDF_PARSER_MINIMUM_VERSION):
        raise _unavailable(_PARSER_VERSION_CATEGORY, started_at, exit_status, header)


def _decode_text(
    payload: bytearray, started_at: float, exit_status: int | None, header: ResultHeader
) -> str:
    """Decode the worker's text exactly as it was encoded; undecodable bytes are a worker fault."""
    try:
        return payload.decode(pdf_text_worker.TEXT_ENCODING, pdf_text_worker.TEXT_ENCODING_ERRORS)
    except UnicodeDecodeError:
        raise _unavailable(_MALFORMED_RESULT_CATEGORY, started_at, exit_status, header) from None


def _refused(
    category: str,
    detail: str,
    started_at: float,
    exit_status: int | None,
    header: ResultHeader | None = None,
) -> EvidenceExtractionError:
    """Log one refusal line and return the 422 exception carrying its detail."""
    _log_outcome(logging.WARNING, "refused", category, started_at, exit_status, header)
    return EvidenceExtractionError(detail)


def _unavailable(
    category: str, started_at: float, exit_status: int | None, header: ResultHeader | None = None
) -> EvidenceProcessingUnavailableError:
    """Log one failure line and return the 503 exception."""
    _log_outcome(logging.ERROR, "unavailable", category, started_at, exit_status, header)
    return EvidenceProcessingUnavailableError(PDF_PROCESSING_UNAVAILABLE_DETAIL)


def _log_outcome(
    level: int,
    outcome: str,
    category: str,
    started_at: float,
    exit_status: int | None,
    header: ResultHeader | None,
) -> None:
    """Log the category, elapsed ms, exit status, ceiling and pypdf version — never the document.

    The ceiling and version come from a ready or result line that passed the
    worker's header parser, so they are short tokens of safe characters.
    """
    elapsed_ms = int((time.monotonic() - started_at) * MILLISECONDS_PER_SECOND)
    ceiling = _NOT_REPORTED if header is None else header.ceiling
    parser_version = _NOT_REPORTED if header is None else header.parser_version
    logger.log(
        level,
        "PDF text extraction %s: category=%s elapsed_ms=%d exit_status=%s ceiling=%s parser_version=%s",
        outcome,
        category,
        elapsed_ms,
        exit_status,
        ceiling,
        parser_version,
    )
