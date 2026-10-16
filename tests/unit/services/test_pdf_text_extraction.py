"""
test_pdf_text_extraction.py — the API side of PDF extraction, with the real worker and probes.

What:  Drives src/services/pdf_text_extraction.extract_pdf_text against the
       real worker process: normal text identical to today's formula, and
       each refusal mapped to its exact detail with no parser text, document
       text or path in it (unreadable, encrypted, 501 pages, a stream over
       PDF_MAX_STREAM_OUTPUT_BYTES, a document over the cumulative decoded
       budget, too much text). PDFs that amplify one shown byte into
       gigabytes of text, on the page or inside a Form XObject pypdf would
       otherwise skip, are refused while the worker's peak committed memory,
       measured on its process handle, stays within the real
       PDF_WORKER_MEMORY_LIMIT_BYTES. The ceiling itself is shown apart from
       every earlier refusal (Windows): under it, memory committed in fixed
       steps is refused only within one step of 512 MiB; a document whose
       memory grows gradually is stopped close to the ceiling and never
       above it; and the same worker code on the same document, with only
       its ceiling installation replaced, grows past it until it is killed.
       With probe scripts in place of the
       worker it shows the process lifecycle and the ready line: a worker
       past the deadline is killed and reaped and never runs on, before or
       after announcing it was ready; a worker that announced it was ready
       and then died, exited or was ended by the operating system without a
       complete result is a 422 over the limits, logged as a warning with its
       exit status; output past the cap is refused and its writer killed;
       after every outcome both pipes are closed, the exit status is
       collected, neither pipe thread is alive and the worker's private
       directory is gone; the kill path ends the worker's whole process group
       on POSIX before reaping it (with a stand-in process); the worker's
       environment carries no credentials, keeps only absolute Python paths,
       and its working directory is a fresh, empty directory of its own, so
       a sitecustomize.py planted in the shared temporary directory or its
       own never runs; read from outside the real worker while it parses
       (Windows), its environment block holds no credential, no name from
       .env.example and nothing from a .env file in, beside or above its
       directories, and its current directory is its private one; a missing
       executable, a worker that stops before
       its ready line, a nonzero exit, a malformed result, a result that does
       not repeat the ready line's ceiling and version, or a verdict without
       an enforced ceiling or from a too-old pypdf is "unavailable"; and an
       exception while waiting still kills and reaps the child. Finally, this
       process's pypdf is never touched.
Why:   The guarantees live in a separate process, so they can only be proven
       with real processes: a mocked Popen would prove nothing about kills,
       reaping, memory ceilings or the environment a child actually receives.
How:   pytest tests/unit/services/test_pdf_text_extraction.py -v
       Each real worker run costs an interpreter start and a pypdf import;
       the tests marked slow wait out a deadline or a slow document. The
       tests that read a process from outside it, or measure committed
       memory, use Windows APIs and are defined only on Windows.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pypdf
import pypdf.filters
import pytest

from config.constants import (
    PDF_MAX_EXTRACTED_TEXT_CHARACTERS,
    PDF_PARSER_MINIMUM_VERSION,
    PDF_WORKER_HEADER_MAX_BYTES,
    PDF_WORKER_MAX_OUTPUT_BYTES,
    PDF_WORKER_MEMORY_LIMIT_BYTES,
    UTF8_MAX_BYTES_PER_CHARACTER,
)
from src.exceptions import EvidenceExtractionError, EvidenceProcessingUnavailableError
from src.services import pdf_text_extraction
from src.services.pdf_text_worker import PYPDF_DECODER_ATTRIBUTE, PYPDF_STREAM_LIMIT_ATTRIBUTES
from tests.unit.services.pdf_worker_probes import outside_view
from tests.unit.services._pdf_documents import (
    AMPLIFIED_FORM_PAGE_TEXT,
    build_pdf,
    build_pdf_with_amplifying_encoding,
    build_pdf_with_amplifying_form,
    build_pdf_with_amplifying_to_unicode,
    build_pdf_with_deflated_page,
    build_pdf_with_form,
    build_pdf_with_undecodable_form,
    deflated_text_with_padding,
    padded,
    reference_text,
    text_operations,
)
from tests.unit.services.pdf_worker_probes.probe_worker import NONZERO_EXIT_STATUS, TERMINATED_EXIT_STATUS

_PROBES = Path(__file__).resolve().parent / "pdf_worker_probes"
_PROBE_PATH = _PROBES / "probe_worker.py"
_UNCAPPED_PROBE_PATH = _PROBES / "probe_uncapped_worker.py"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_ENV_EXAMPLE_PATH = _REPOSITORY_ROOT / ".env.example"
_LOGGER_NAME = "src.services.pdf_text_extraction"
_ON_WINDOWS = sys.platform == "win32"
_PLATFORM_CEILING = "windows-job-object" if _ON_WINDOWS else "posix-rlimit"
_PIPE_THREAD_NAMES = ["pdf-worker-stdin-writer", "pdf-worker-stdout-reader"]
_PRIVATE_DIRECTORY_PREFIX = "kerno-pdf-worker-"
_OWNER_ONLY_MODE = 0o700

# The exact details the design fixes. Written out here, not imported, so a
# change to the wording the dashboard shows fails a test.
_UNREADABLE = "the PDF could not be read"
_ENCRYPTED = "encrypted PDFs are not supported"
_TOO_MANY_PAGES = "the PDF has more than 500 pages"
_OVER_LIMITS = "the PDF exceeds the text-extraction limits"
_TIMED_OUT = "the PDF could not be processed within the time limit"
_UNAVAILABLE = "evidence processing is unavailable; try again later"

_MEBIBYTE = 1024 * 1024
_SHORT_DEADLINE_SECONDS = 1.5
_PROBE_SLEEP_SECONDS = 3.0
_KILL_ALLOWANCE_SECONDS = 1.0
_SURVIVAL_MARGIN_SECONDS = 1.0
_MARKER_WAIT_SECONDS = 15.0
_POLL_SECONDS = 0.02
_UNREAD_BODY_BYTES = 4 * _MEBIBYTE
_SURROGATE_TEXT = "lone \ud800 surrogate"
_SMALL_OUTPUT_CAP_BYTES = 256 * 1024
_ROOMY_DEADLINE_SECONDS = 20.0
_OVERFLOW_ALLOWANCE_SECONDS = 10.0
_EXACT_OUTPUT_BYTES = 300_000
_FAKE_WORKER_PID = 4242
_FAKE_KILLED_STATUS = -9
_STAND_IN_SIGKILL = 9

# The real ceiling, shown on its own (Windows). A Job Object's per-process
# memory limit caps exactly the committed memory psapi reports as
# PeakPagefileUsage, so nothing is allowed above the ceiling. Below it: memory
# committed in steps of _ALLOCATION_STEP_BYTES must be refused within one
# step of the ceiling, plus what the heap adds to each block and the
# interpreter allocates around them (_ALLOCATION_BOOKKEEPING_BYTES; measured
# 0.89 MiB below the ceiling with 1 MiB steps). A document whose memory grows
# gradually is refused when the next growth no longer fits: the ToUnicode
# amplifier grows one fragment by 4,000,000 characters at a time, measured at
# 4.3 to 6.1 MiB below the ceiling over six runs, so _CLOSE_TO_CEILING_BYTES
# allows well over twice that and still says "close".
_ALLOCATION_STEP_BYTES = _MEBIBYTE
_ALLOCATION_BOOKKEEPING_BYTES = _MEBIBYTE
_CLOSE_TO_CEILING_BYTES = 16 * _MEBIBYTE
_GROWING_MAPPED_CHARACTERS = 4_000_000
_GROWING_TIMES_SHOWN = 400
# Where the counterfactual run without the ceiling is killed: far enough
# above the ceiling that no accounting detail could explain passing it.
_PAST_THE_CEILING_THRESHOLD_BYTES = PDF_WORKER_MEMORY_LIMIT_BYTES + 128 * _MEBIBYTE
_WATCH_POLL_SECONDS = 0.005
_WATCH_BOUND_SECONDS = 60.0
_CHILD_TIMEOUT_SECONDS = 60

# A legitimate document slow enough (about 1.6 s of parsing after the ready
# line, measured) that the worker is certainly still parsing when it is read
# from outside, milliseconds after its ready line arrives.
_SLOW_DOCUMENT_PAGES = 100
_SLOW_DOCUMENT_LINES_PER_PAGE = 42
_SLOW_LINE_PHRASE = "Access control review completed for every production system and signed off. "
_SLOW_LINE_REPEATS = 6
_DOTENV_PROBE_HOLD_SECONDS = 2.0

_CREDENTIAL_SENTINELS = {
    "DATABASE_URL": "postgresql://sentinel-user:sentinel-pass@127.0.0.1:1/none",
    "KERNO_MIGRATION_DATABASE_URL": "postgresql://sentinel-migration@127.0.0.1:1/none",
    "KERNO_JWT_SECRET": "sentinel-jwt-secret",
    "PGPASSWORD": "sentinel-pg-password",
    "PGPASSFILE": "sentinel-pg-passfile",
    "MISTRAL_API_KEY": "sentinel-mistral-key",
    "KERNO_TEST_DATABASE_URL": "postgresql://sentinel-test@127.0.0.1:1/none",
    "KERNO_TEST_RUNTIME_DATABASE_URL": "postgresql://sentinel-runtime@127.0.0.1:1/none",
    "WEBHOOK_SIGNING_SECRET": "sentinel-signing-secret",
    "GITHUB_TOKEN": "sentinel-github-token",
    "AWS_ACCESS_KEY_ID": "sentinel-access-key",
    "SMTP_PASSWORD": "sentinel-smtp-password",
}
_FORBIDDEN_NAME_FRAGMENTS = ("SECRET", "TOKEN", "KEY", "PASSWORD", "DATABASE", "KERNO", "PG")
# Names an operating system may add to every process it starts.
_PLATFORM_ADDED_NAMES = frozenset({"__CF_USER_TEXT_ENCODING"})
# An assignment line of .env.example: a variable name at the start of a line.
_ENV_EXAMPLE_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")

# Absolute on any host: what PYTHONPATH may keep. The relative entries are
# relative on every host, Windows' drive-relative and rooted forms included.
_ABSOLUTE_ENTRY = os.path.abspath("kerno-absolute-entry")
_OTHER_ABSOLUTE_ENTRY = os.path.abspath("kerno-other-absolute-entry")
_RELATIVE_ENTRIES = (".", "..", "src", "C:relative", "\\rooted-without-a-drive")
_PLANTED_MODULE = "sitecustomize.py"
_PLANTED_MODULE_SOURCE = "import pathlib\npathlib.Path({marker!r}).write_text('imported', encoding='utf-8')\n"


@pytest.fixture(autouse=True)
def thread_failures(monkeypatch):
    """Fail the test if any thread, a pipe thread above all, ends with an uncaught exception."""
    failures = []

    def record_failure(arguments):
        """Keep the uncaught exception's type and the thread it ended."""
        failures.append((arguments.thread.name if arguments.thread else None, arguments.exc_type))

    monkeypatch.setattr(threading, "excepthook", record_failure)
    yield failures
    assert failures == []


def _refusal_of(pdf: bytes) -> EvidenceExtractionError:
    """Run the real worker on pdf and return the refusal it must raise."""
    with pytest.raises(EvidenceExtractionError) as refusal:
        pdf_text_extraction.extract_pdf_text(pdf)
    return refusal.value


def _unavailable_from(content: bytes = b"%PDF-1.4") -> EvidenceProcessingUnavailableError:
    """Run extraction and return the 503 failure it must raise."""
    with pytest.raises(EvidenceProcessingUnavailableError) as failure:
        pdf_text_extraction.extract_pdf_text(content)
    assert str(failure.value) == _UNAVAILABLE
    return failure.value


def _use_probe(monkeypatch, *probe_arguments: str) -> None:
    """Make extract_pdf_text start the probe script, with these arguments, in place of the worker."""

    def probe_command(limits=None):
        """Return the probe's argv; the worker's limits are irrelevant to it."""
        return [sys.executable, str(_PROBE_PATH), *probe_arguments]

    monkeypatch.setattr(pdf_text_extraction, "worker_command", probe_command)


def _record_started_processes(monkeypatch) -> list:
    """Record every process extract_pdf_text starts, still starting it the real way."""
    real_start = pdf_text_extraction._start_worker
    started = []

    def recording_start(command, working_directory):
        """Start the process as extract_pdf_text would and keep a handle to it."""
        process = real_start(command, working_directory)
        started.append(process)
        return process

    monkeypatch.setattr(pdf_text_extraction, "_start_worker", recording_start)
    return started


def _record_private_directories(monkeypatch, plant=None) -> list[Path]:
    """Record every private directory extract_pdf_text makes, still making it the real way.

    plant, if given, is called with each new directory before the worker
    starts in it. The directory's mode is kept on the list's .modes, read
    while it exists.
    """
    real_create = pdf_text_extraction._create_private_directory
    directories = _DirectoryRecord()

    def recording_create():
        """Make the directory as extract_pdf_text would, remember it and its mode, and plant anything asked for."""
        directory = real_create()
        directories.append(Path(directory))
        directories.modes.append(stat.S_IMODE(os.stat(directory).st_mode))
        if plant is not None:
            plant(Path(directory))
        return directory

    monkeypatch.setattr(pdf_text_extraction, "_create_private_directory", recording_create)
    return directories


class _DirectoryRecord(list):
    """The private directories one test saw made, in order, with the mode each had when made."""

    def __init__(self) -> None:
        """Start with no directories and no modes."""
        super().__init__()
        self.modes: list[int] = []


def _record_pipe_threads(monkeypatch) -> list:
    """Record every pipe thread extract_pdf_text starts, still starting it the real way."""
    real_start = pdf_text_extraction._start_pipe_thread
    threads = []

    def recording_start(target, name):
        """Start the thread as extract_pdf_text would and keep a handle to it."""
        thread = real_start(target, name)
        threads.append(thread)
        return thread

    monkeypatch.setattr(pdf_text_extraction, "_start_pipe_thread", recording_start)
    return threads


def _assert_cleaned_up(processes: list, threads: list, directories: list[Path]) -> None:
    """Assert the one worker was reaped, both its pipes are closed, both pipe threads have ended and its directory is gone."""
    assert len(processes) == 1
    process = processes[0]
    assert process.returncode is not None
    assert process.stdin.closed
    assert process.stdout.closed
    assert [thread.name for thread in threads] == _PIPE_THREAD_NAMES
    assert not [thread for thread in threads if thread.is_alive()]
    assert len(directories) == 1
    assert not directories[0].exists()


def _logged_lines(caplog) -> list[str]:
    """Return the messages this module's logger wrote."""
    return [record.getMessage() for record in caplog.records if record.name == _LOGGER_NAME]


def _logged_category(caplog) -> str:
    """Return the category of the one outcome line extraction logged."""
    lines = _logged_lines(caplog)
    assert len(lines) == 1, lines
    return re.search(r"category=(\S+)", lines[0]).group(1)


def _logged_record(caplog) -> logging.LogRecord:
    """Return the one outcome record extraction logged."""
    records = [record for record in caplog.records if record.name == _LOGGER_NAME]
    assert len(records) == 1, [record.getMessage() for record in records]
    return records[0]


def _wait_for(path: Path) -> None:
    """Wait until path exists, failing the test if it never appears."""
    give_up_at = time.monotonic() + _MARKER_WAIT_SECONDS
    while not path.exists():
        assert time.monotonic() < give_up_at, f"{path.name} never appeared"
        time.sleep(_POLL_SECONDS)


def _sleep_until(moment: float) -> None:
    """Sleep until the monotonic clock reaches moment."""
    remaining = moment - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)


def _same_path(first, second) -> bool:
    """Return whether two paths name the same location, compared as text, since either may no longer exist."""
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _has_forbidden_fragment(name: str) -> bool:
    """Return whether an environment variable name looks like it could carry a credential."""
    upper = name.upper()
    return any(fragment in upper for fragment in _FORBIDDEN_NAME_FRAGMENTS)


def _peak_memory_bytes(process: subprocess.Popen) -> int:
    """Return the worker's peak memory: Windows committed bytes, else the largest child's resident set.

    On Windows this reads PeakPagefileUsage through psapi on the child's
    process handle, which the Popen object keeps open after the child exits.
    On Linux it is the peak resident set of any child this process has
    reaped, which can only overstate the worker's own.
    """
    if _ON_WINDOWS:
        return outside_view.committed_memory(int(process._handle)).peak
    import resource

    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024


# ── Real worker: text ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pdf",
    [
        pytest.param(build_pdf([text_operations("Board approved ISMS policy")]), id="one-page"),
        pytest.param(
            build_pdf(
                [
                    text_operations("Incident runbook v4", "Escalate within 4h"),
                    None,
                    text_operations("Zürich café € (Annex A)"),
                ]
            ),
            id="multi-page-empty-page-non-ascii",
        ),
        pytest.param(build_pdf([text_operations("no password")], user_password=""), id="empty-password"),
    ],
)
def test_real_worker_text_is_identical_to_todays_formula(pdf):
    assert pdf_text_extraction.extract_pdf_text(pdf) == reference_text(pdf)


def test_a_document_exactly_at_the_page_cap_is_read():
    assert pdf_text_extraction.extract_pdf_text(build_pdf([None] * 500)) == "\n" * 499


# ── Real worker: every refusal has its exact detail and nothing else ────────


@pytest.mark.parametrize(
    ("pdf", "detail", "category"),
    [
        pytest.param(b"%PDF-1.4 this is not a real pdf", _UNREADABLE, "unreadable", id="unreadable"),
        pytest.param(
            build_pdf([text_operations("confidential board minutes")], user_password="pw"),
            _ENCRYPTED,
            "encrypted",
            id="encrypted",
        ),
        pytest.param(build_pdf([None] * 501), _TOO_MANY_PAGES, "too-many-pages", id="501-pages"),
        pytest.param(
            build_pdf_with_deflated_page(
                deflated_text_with_padding(text_operations("bomb"), 16 * _MEBIBYTE + 1)
            ),
            _OVER_LIMITS,
            "over-limits",
            id="stream-over-16-mib",
        ),
        pytest.param(
            build_pdf_with_form(
                text_operations("page text"),
                padded(text_operations("form text"), 16 * _MEBIBYTE + 1),
            ),
            _OVER_LIMITS,
            "over-limits",
            id="form-over-16-mib-that-pypdf-would-skip",
        ),
    ],
)
def test_real_worker_refusals_carry_only_their_exact_detail(pdf, detail, category, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    refusal = _refusal_of(pdf)
    assert str(refusal) == detail
    assert refusal.__cause__ is None
    assert refusal.__context__ is None
    log_lines = _logged_lines(caplog)
    assert len(log_lines) == 1
    assert f"category={category}" in log_lines[0]
    assert f"ceiling={_PLATFORM_CEILING} parser_version={pypdf.__version__}" in log_lines[0]
    assert "confidential" not in log_lines[0]
    assert str(pdf_text_extraction.WORKER_PATH) not in log_lines[0]


def test_real_worker_refuses_a_document_over_the_cumulative_decoded_budget(monkeypatch):
    pages = [padded(text_operations(f"page {number}"), 30 * 1024) for number in range(3)]
    three_streams = build_pdf(pages)
    expected = reference_text(three_streams)
    assert pdf_text_extraction.extract_pdf_text(three_streams) == expected
    monkeypatch.setattr(pdf_text_extraction, "PDF_MAX_DECODED_BYTES", 64 * 1024)
    assert str(_refusal_of(three_streams)) == _OVER_LIMITS


def test_real_worker_refuses_text_over_the_character_budget(monkeypatch):
    pdf = build_pdf([text_operations("x" * 80), text_operations("y" * 80)])
    monkeypatch.setattr(pdf_text_extraction, "PDF_MAX_EXTRACTED_TEXT_CHARACTERS", 100)
    assert str(_refusal_of(pdf)) == _OVER_LIMITS


# ── Real worker: amplifying fonts against the real memory ceiling (R1) ──────

# How each outcome of an amplifying PDF reaches the API: the worker's
# MemoryError (over-limits, 422), the worker dying after its ready line
# without a result (died-while-parsing, 422 with the same detail), or the
# deadline (timeout, 422). None of them may be a 503 or partial text.
_AMPLIFIER_OUTCOMES = {
    "over-limits": (EvidenceExtractionError, _OVER_LIMITS),
    "died-while-parsing": (EvidenceExtractionError, _OVER_LIMITS),
    "timeout": (EvidenceExtractionError, _TIMED_OUT),
}


@pytest.mark.parametrize(
    ("build", "characters", "times_shown", "possible_outcomes"),
    [
        pytest.param(
            build_pdf_with_amplifying_encoding, 4000, 400_000, {"over-limits"}, id="glyph-name-joined-at-once"
        ),
        pytest.param(
            build_pdf_with_amplifying_form, 4000, 400_000, {"over-limits"}, id="glyph-name-inside-a-form"
        ),
        pytest.param(
            build_pdf_with_amplifying_to_unicode,
            4_000_000,
            400,
            {"over-limits", "died-while-parsing"},
            id="to-unicode-long-mapping",
        ),
        pytest.param(
            build_pdf_with_amplifying_to_unicode,
            4000,
            400_000,
            {"over-limits", "died-while-parsing", "timeout"},
            id="to-unicode-few-thousand-characters-shown-400k-times",
            marks=pytest.mark.slow,
        ),
    ],
)
def test_an_amplifying_pdf_is_refused_without_the_worker_exceeding_its_real_memory_ceiling(
    monkeypatch, caplog, build, characters, times_shown, possible_outcomes
):
    pdf = build(characters, times_shown)
    assert len(pdf) < 32 * 1024
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    processes = _record_started_processes(monkeypatch)
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    with pytest.raises((EvidenceExtractionError, EvidenceProcessingUnavailableError)) as refusal:
        pdf_text_extraction.extract_pdf_text(pdf)
    outcome = _logged_category(caplog)
    assert outcome in possible_outcomes
    assert (type(refusal.value), str(refusal.value)) == _AMPLIFIER_OUTCOMES[outcome]
    _assert_cleaned_up(processes, threads, directories)
    assert _peak_memory_bytes(processes[0]) <= PDF_WORKER_MEMORY_LIMIT_BYTES


@pytest.mark.parametrize(
    "build", [build_pdf_with_amplifying_encoding, build_pdf_with_amplifying_to_unicode, build_pdf_with_amplifying_form]
)
def test_the_amplifying_fonts_read_normally_at_a_harmless_size(build):
    pdf = build(8, 3)
    text = pdf_text_extraction.extract_pdf_text(pdf)
    assert text == reference_text(pdf)
    assert text.count("A") == 8 * 3


def test_the_form_amplifier_is_refused_rather_than_returned_as_the_pages_own_text(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    pdf = build_pdf_with_amplifying_form(4000, 400_000)
    refusal = _refusal_of(pdf)
    assert str(refusal) == _OVER_LIMITS
    assert AMPLIFIED_FORM_PAGE_TEXT not in str(refusal)
    assert _logged_category(caplog) == "over-limits"


def test_a_form_pypdf_cannot_decode_is_still_skipped_as_today():
    pdf = build_pdf_with_undecodable_form(text_operations("page text"))
    assert pdf_text_extraction.extract_pdf_text(pdf) == reference_text(pdf) == "page text\n"


# ── Lifecycle: deadline, kill, reap ─────────────────────────────────────────


@pytest.mark.slow
def test_a_worker_past_the_deadline_is_killed_reaped_and_never_runs_on(monkeypatch, tmp_path):
    started_marker, survived_marker = tmp_path / "started", tmp_path / "survived"
    monkeypatch.setattr(pdf_text_extraction, "PDF_EXTRACTION_DEADLINE_SECONDS", _SHORT_DEADLINE_SECONDS)
    _use_probe(monkeypatch, "hang", str(started_marker), str(survived_marker), str(_PROBE_SLEEP_SECONDS))
    processes = _record_started_processes(monkeypatch)
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    # Larger than any pipe buffer, and the probe never reads it: the deadline
    # passes while the writer thread is still blocked writing the document.
    unread_body = b"%PDF-1.4" + b" " * _UNREAD_BODY_BYTES
    began = time.monotonic()
    refusal = _refusal_of(unread_body)
    elapsed = time.monotonic() - began
    assert str(refusal) == _TIMED_OUT
    assert _SHORT_DEADLINE_SECONDS <= elapsed < _SHORT_DEADLINE_SECONDS + _KILL_ALLOWANCE_SECONDS
    assert elapsed < _PROBE_SLEEP_SECONDS
    _assert_cleaned_up(processes, threads, directories)
    assert started_marker.exists()
    _sleep_until(began + _PROBE_SLEEP_SECONDS + _SURVIVAL_MARGIN_SECONDS)
    assert not survived_marker.exists()


@pytest.mark.slow
def test_an_exception_while_waiting_in_a_thread_still_kills_and_reaps_the_worker(monkeypatch, tmp_path):
    started_marker, survived_marker = tmp_path / "started", tmp_path / "survived"
    _use_probe(monkeypatch, "hang", str(started_marker), str(survived_marker), str(_PROBE_SLEEP_SECONDS))
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    real_start = pdf_text_extraction._start_worker
    processes = []

    def start_then_fail_while_waiting(command, working_directory):
        """Start the probe, then make the first wait fail once the probe is running."""
        process = real_start(command, working_directory)
        processes.append(process)
        real_wait = process.wait

        def wait_failing_once(*args, **kwargs):
            """Fail the first wait like an unexpected error; later waits are real."""
            process.wait = real_wait
            _wait_for(started_marker)
            raise RuntimeError("injected failure while waiting for the worker")

        process.wait = wait_failing_once
        return process

    monkeypatch.setattr(pdf_text_extraction, "_start_worker", start_then_fail_while_waiting)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(pdf_text_extraction.extract_pdf_text, b"%PDF-1.4")
        with pytest.raises(RuntimeError, match="injected failure"):
            waiting.result(timeout=_MARKER_WAIT_SECONDS)
    killed_at = time.monotonic()
    _assert_cleaned_up(processes, threads, directories)
    _sleep_until(killed_at + _PROBE_SLEEP_SECONDS + _SURVIVAL_MARGIN_SECONDS)
    assert not survived_marker.exists()


# ── Lifecycle: bounded output and complete cleanup (R3) ─────────────────────


def test_output_past_the_cap_is_unavailable_and_the_flooding_worker_is_killed_at_once(monkeypatch, caplog):
    monkeypatch.setattr(pdf_text_extraction, "PDF_WORKER_MAX_OUTPUT_BYTES", _SMALL_OUTPUT_CAP_BYTES)
    monkeypatch.setattr(pdf_text_extraction, "PDF_EXTRACTION_DEADLINE_SECONDS", _ROOMY_DEADLINE_SECONDS)
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    _use_probe(monkeypatch, "flood")
    processes = _record_started_processes(monkeypatch)
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    began = time.monotonic()
    _unavailable_from()
    assert time.monotonic() - began < _OVERFLOW_ALLOWANCE_SECONDS
    assert _logged_category(caplog) == "output-overflow"
    _assert_cleaned_up(processes, threads, directories)


def test_output_exactly_at_the_cap_is_accepted_and_one_byte_more_is_not(monkeypatch):
    _use_probe(monkeypatch, "exact-output", str(_EXACT_OUTPUT_BYTES))
    monkeypatch.setattr(pdf_text_extraction, "PDF_WORKER_MAX_OUTPUT_BYTES", _EXACT_OUTPUT_BYTES)
    text = pdf_text_extraction.extract_pdf_text(b"%PDF-1.4")
    header_bytes = len(b"ready windows-job-object 999.0.0\n") + len(b"text windows-job-object 999.0.0\n")
    assert text == "a" * (_EXACT_OUTPUT_BYTES - header_bytes)
    monkeypatch.setattr(pdf_text_extraction, "PDF_WORKER_MAX_OUTPUT_BYTES", _EXACT_OUTPUT_BYTES - 1)
    _unavailable_from()


def test_the_output_cap_holds_both_header_lines_and_the_largest_text_at_its_widest_encoding():
    assert PDF_WORKER_MAX_OUTPUT_BYTES == (
        UTF8_MAX_BYTES_PER_CHARACTER * PDF_MAX_EXTRACTED_TEXT_CHARACTERS + 2 * PDF_WORKER_HEADER_MAX_BYTES
    )
    assert len("\U0001f512".encode("utf-8")) == UTF8_MAX_BYTES_PER_CHARACTER


def test_a_worker_that_answers_without_reading_the_document_is_believed_and_the_writer_ends(monkeypatch):
    _use_probe(monkeypatch, "answer-without-reading")
    processes = _record_started_processes(monkeypatch)
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    refusal = _refusal_of(b"%PDF-1.4" + b" " * _UNREAD_BODY_BYTES)
    assert str(refusal) == _ENCRYPTED
    _assert_cleaned_up(processes, threads, directories)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        pytest.param("text-under-job-object", "probe text", id="text"),
        pytest.param("refusal-under-rlimit", EvidenceExtractionError, id="refusal"),
        pytest.param("unavailable", EvidenceProcessingUnavailableError, id="unavailable"),
        pytest.param("garbage-token", EvidenceProcessingUnavailableError, id="malformed"),
        pytest.param("exit-nonzero", EvidenceProcessingUnavailableError, id="nonzero-exit"),
        pytest.param("text-without-ceiling", EvidenceProcessingUnavailableError, id="untrusted"),
        pytest.param("flood", EvidenceProcessingUnavailableError, id="overflow"),
        pytest.param("die-before-ready", EvidenceProcessingUnavailableError, id="died-before-ready"),
        pytest.param("die-after-ready", EvidenceExtractionError, id="died-after-ready"),
        pytest.param("terminated-after-ready", EvidenceExtractionError, id="terminated-after-ready"),
    ],
)
def test_after_every_outcome_the_worker_is_reaped_its_pipes_closed_and_no_pipe_thread_alive(
    monkeypatch, mode, expected
):
    monkeypatch.setattr(pdf_text_extraction, "PDF_WORKER_MAX_OUTPUT_BYTES", _SMALL_OUTPUT_CAP_BYTES)
    _use_probe(monkeypatch, mode)
    processes = _record_started_processes(monkeypatch)
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    if isinstance(expected, str):
        assert pdf_text_extraction.extract_pdf_text(b"%PDF-1.4") == expected
    else:
        with pytest.raises(expected):
            pdf_text_extraction.extract_pdf_text(b"%PDF-1.4")
    _assert_cleaned_up(processes, threads, directories)


def test_after_a_real_extraction_the_worker_is_reaped_its_pipes_closed_and_no_pipe_thread_alive(monkeypatch):
    processes = _record_started_processes(monkeypatch)
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    assert pdf_text_extraction.extract_pdf_text(build_pdf([text_operations("cleaned up")])) == "cleaned up"
    _assert_cleaned_up(processes, threads, directories)


# ── Lifecycle: the kill path ends the worker's whole process group (SEC-G6-01)


class _StandInWorker:
    """A worker process stand-in that records, in order, what the kill path does to it."""

    def __init__(self, events: list, running: bool = True) -> None:
        """Start running (or already exited) with a fixed pid, logging into events."""
        self.pid = _FAKE_WORKER_PID
        self.returncode = None if running else 0
        self._events = events

    def poll(self):
        """Log the poll and return the exit status, None while running."""
        self._events.append("poll")
        return self.returncode

    def kill(self) -> None:
        """Log the kill and end the stand-in."""
        self._events.append("kill")
        self.returncode = _FAKE_KILLED_STATUS

    def wait(self, timeout=None):
        """Log the wait and return the exit status."""
        self._events.append("wait")
        return self.returncode


def _record_group_kills(monkeypatch, events: list, failure: type[OSError] | None = None):
    """Replace os.killpg with a recorder (raising failure, if given) and return the signal it must receive.

    os.killpg and signal.SIGKILL exist only on POSIX; on Windows both are
    supplied here, so the POSIX branch runs on any host.
    """
    group_kill_signal = getattr(signal, "SIGKILL", None)
    if group_kill_signal is None:
        group_kill_signal = _STAND_IN_SIGKILL
        monkeypatch.setattr(signal, "SIGKILL", group_kill_signal, raising=False)

    def recording_killpg(group_id, signal_number):
        """Log the group kill, then fail as asked."""
        events.append(("killpg", group_id, signal_number))
        if failure is not None:
            raise failure("injected group-kill failure")

    monkeypatch.setattr(os, "killpg", recording_killpg, raising=False)
    return group_kill_signal


@pytest.mark.parametrize("platform", ["linux", "darwin", "freebsd14"])
def test_on_posix_the_kill_path_kills_the_workers_process_group_then_the_worker_then_reaps_it(monkeypatch, platform):
    events = []
    group_kill_signal = _record_group_kills(monkeypatch, events)
    pdf_text_extraction._kill_and_reap(_StandInWorker(events), platform=platform)
    assert events == ["poll", ("killpg", _FAKE_WORKER_PID, group_kill_signal), "kill", "wait"]


def test_on_windows_the_kill_path_kills_only_the_worker_whose_job_object_holds_no_other_process(monkeypatch):
    events = []
    _record_group_kills(monkeypatch, events)
    pdf_text_extraction._kill_and_reap(_StandInWorker(events), platform="win32")
    assert events == ["poll", "kill", "wait"]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_a_worker_that_has_already_exited_is_only_reaped_and_its_group_never_signalled(monkeypatch, platform):
    events = []
    _record_group_kills(monkeypatch, events)
    pdf_text_extraction._kill_and_reap(_StandInWorker(events, running=False), platform=platform)
    assert events == ["poll", "wait"]


@pytest.mark.parametrize("failure", [ProcessLookupError, PermissionError, OSError])
def test_a_group_kill_that_fails_still_lets_the_worker_be_killed_and_reaped(monkeypatch, failure):
    events = []
    group_kill_signal = _record_group_kills(monkeypatch, events, failure)
    worker = _StandInWorker(events)
    pdf_text_extraction._kill_and_reap(worker, platform="linux")
    assert events == ["poll", ("killpg", _FAKE_WORKER_PID, group_kill_signal), "kill", "wait"]
    assert worker.returncode == _FAKE_KILLED_STATUS


class _FailingOutput:
    """A stdout whose every read fails as a broken pipe would."""

    def read(self, size: int) -> bytes:
        """Fail the read."""
        raise OSError("injected read failure")


@pytest.mark.parametrize(
    ("read_fails", "category"),
    [pytest.param(False, "output-overflow", id="overflow"), pytest.param(True, "output-read-failed", id="read-failure")],
)
def test_a_reader_that_gives_up_kills_the_worker_through_the_same_kill_path(monkeypatch, read_fails, category):
    monkeypatch.setattr(pdf_text_extraction, "PDF_WORKER_MAX_OUTPUT_BYTES", _SMALL_OUTPUT_CAP_BYTES)
    killed = []
    monkeypatch.setattr(pdf_text_extraction, "_kill_if_running", killed.append)
    worker = _StandInWorker([])
    worker.stdout = _FailingOutput() if read_fails else io.BytesIO(b"r" * (_SMALL_OUTPUT_CAP_BYTES + 1))
    pipes = pdf_text_extraction._WorkerPipes(worker, b"")
    pipes._collect_result()
    assert pipes.failure == category
    assert killed == [worker]


def test_the_worker_is_started_as_the_leader_of_a_new_session(monkeypatch):
    launches = []

    def recording_popen(*args, **kwargs):
        """Record the launch's keyword arguments and start nothing."""
        launches.append(kwargs)
        raise OSError("nothing is started in this test")

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    with pytest.raises(OSError):
        pdf_text_extraction._start_worker(["no-such-program"], str(Path.cwd()))
    assert launches[0]["start_new_session"] is True


# ── Lifecycle: the environment and directory the worker receives (R4, SEC-G6-02)


def test_the_worker_receives_no_credentials_and_runs_in_its_own_empty_private_directory(monkeypatch):
    for name, value in _CREDENTIAL_SENTINELS.items():
        monkeypatch.setenv(name, value)
    directories = _record_private_directories(monkeypatch)
    _use_probe(monkeypatch, "environment")
    report = json.loads(pdf_text_extraction.extract_pdf_text(b"%PDF-1.4"))
    child_environment = report["environment"]
    for name, value in _CREDENTIAL_SENTINELS.items():
        assert name not in child_environment
        assert value not in json.dumps(child_environment)
    for name in child_environment:
        assert not _has_forbidden_fragment(name), name
    unexpected = set(child_environment) - set(pdf_text_extraction.WORKER_ENVIRONMENT_ALLOWLIST)
    assert unexpected <= _PLATFORM_ADDED_NAMES
    assert child_environment["PATH"] == os.environ["PATH"]
    if _ON_WINDOWS:
        assert child_environment["SYSTEMROOT"] == os.environ["SYSTEMROOT"]
    [directory] = directories
    assert _same_path(report["cwd"], directory)
    assert report["cwd_listing"] == []
    assert _same_path(directory.parent, tempfile.gettempdir())
    assert directory.name.startswith(_PRIVATE_DIRECTORY_PREFIX)
    if not _ON_WINDOWS:
        assert directories.modes == [_OWNER_ONLY_MODE]
    assert not directory.exists()


def test_every_worker_gets_a_directory_of_its_own(monkeypatch):
    directories = _record_private_directories(monkeypatch)
    _use_probe(monkeypatch, "environment")
    first = json.loads(pdf_text_extraction.extract_pdf_text(b"%PDF-1.4"))["cwd"]
    second = json.loads(pdf_text_extraction.extract_pdf_text(b"%PDF-1.4"))["cwd"]
    assert len(directories) == 2
    assert directories[0] != directories[1]
    assert _same_path(first, directories[0])
    assert _same_path(second, directories[1])
    assert not [directory for directory in directories if directory.exists()]


def test_a_real_extraction_works_with_credentials_in_the_api_and_none_in_the_worker(monkeypatch):
    for name, value in _CREDENTIAL_SENTINELS.items():
        monkeypatch.setenv(name, value)
    directories = _record_private_directories(monkeypatch)
    launches = []

    class _RecordingPopen(subprocess.Popen):
        """subprocess.Popen, recording the keyword arguments each launch used."""

        def __init__(self, *args, **kwargs):
            """Record the launch's keyword arguments, then start the process unchanged."""
            launches.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)
    pdf = build_pdf([text_operations("Board approved ISMS policy")])
    expected = reference_text(pdf)
    assert pdf_text_extraction.extract_pdf_text(pdf) == expected
    assert len(launches) == 1
    assert launches[0]["cwd"] == str(directories[0])
    assert not _same_path(directories[0], _REPOSITORY_ROOT)
    assert set(launches[0]["env"]).isdisjoint(_CREDENTIAL_SENTINELS)
    assert launches[0]["stderr"] == subprocess.DEVNULL
    assert launches[0]["start_new_session"] is True
    assert not directories[0].exists()


def test_worker_environment_is_an_allowlist_of_present_names():
    parent = {"PATH": "/usr/bin", "HOME": "/home/kerno", "DATABASE_URL": "x", "KERNO_JWT_SECRET": "y"}
    assert pdf_text_extraction.worker_environment(parent) == {"PATH": "/usr/bin", "HOME": "/home/kerno"}


@pytest.mark.parametrize(
    ("search_path", "kept"),
    [
        pytest.param(".", None, id="dot"),
        pytest.param("", None, id="empty"),
        pytest.param("..", None, id="parent"),
        pytest.param(os.pathsep.join(_RELATIVE_ENTRIES), None, id="only-relative"),
        pytest.param(os.pathsep + _ABSOLUTE_ENTRY, _ABSOLUTE_ENTRY, id="leading-empty-entry"),
        pytest.param(_ABSOLUTE_ENTRY + os.pathsep, _ABSOLUTE_ENTRY, id="trailing-empty-entry"),
        pytest.param(
            os.pathsep.join([_ABSOLUTE_ENTRY, *_RELATIVE_ENTRIES, "", _OTHER_ABSOLUTE_ENTRY]),
            os.pathsep.join([_ABSOLUTE_ENTRY, _OTHER_ABSOLUTE_ENTRY]),
            id="mixed-keeps-absolute-in-order",
        ),
        pytest.param(
            os.pathsep.join([_ABSOLUTE_ENTRY, _OTHER_ABSOLUTE_ENTRY]),
            os.pathsep.join([_ABSOLUTE_ENTRY, _OTHER_ABSOLUTE_ENTRY]),
            id="all-absolute",
        ),
    ],
)
def test_pythonpath_keeps_only_its_absolute_entries_and_is_left_out_when_none_remain(search_path, kept):
    environment = pdf_text_extraction.worker_environment({"PATH": "/usr/bin", "PYTHONPATH": search_path})
    assert environment.get("PYTHONPATH") == kept
    assert environment["PATH"] == "/usr/bin"


@pytest.mark.parametrize("name", ["PYTHONHOME", "PYTHONUSERBASE"])
@pytest.mark.parametrize(
    ("value", "passes"),
    [(_ABSOLUTE_ENTRY, True), (".", False), ("", False), ("..", False), ("relative/base", False), ("C:relative", False)],
)
def test_pythonhome_and_pythonuserbase_pass_only_when_absolute(name, value, passes):
    environment = pdf_text_extraction.worker_environment({name: value})
    assert environment == ({name: value} if passes else {})


def _plant_module(directory: Path, marker: Path) -> None:
    """Write a sitecustomize.py into directory that leaves marker behind if any interpreter imports it."""
    source = _PLANTED_MODULE_SOURCE.format(marker=str(marker))
    (directory / _PLANTED_MODULE).write_text(source, encoding="utf-8")


def _share_a_temporary_directory(monkeypatch, shared: Path) -> None:
    """Make shared this process's temporary directory, and the one the worker inherits, as on a shared host."""
    shared.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(shared))


# A PYTHONPATH that is wholly empty is ignored by CPython itself; an empty
# entry beside another is not, and resolves to the working directory.
@pytest.mark.parametrize(
    "search_path",
    [
        pytest.param(".", id="dot"),
        pytest.param("..", id="parent"),
        pytest.param(os.pathsep + _ABSOLUTE_ENTRY, id="leading-empty-entry"),
        pytest.param(_ABSOLUTE_ENTRY + os.pathsep, id="trailing-empty-entry"),
        pytest.param(os.pathsep.join([".", ".."]), id="dot-and-parent"),
    ],
)
def test_a_module_planted_in_the_shared_or_private_directory_never_runs_in_the_real_worker(
    monkeypatch, tmp_path, search_path
):
    shared = tmp_path / "shared-temporary-directory"
    _share_a_temporary_directory(monkeypatch, shared)
    shared_marker, private_marker = tmp_path / "shared-module-ran", tmp_path / "private-module-ran"
    _plant_module(shared, shared_marker)

    def plant_in_private_directory(directory: Path) -> None:
        """Plant the module in the worker's own directory too, before the worker starts there."""
        _plant_module(directory, private_marker)

    directories = _record_private_directories(monkeypatch, plant=plant_in_private_directory)
    monkeypatch.setenv("PYTHONPATH", search_path)
    pdf = build_pdf([text_operations("planted modules never run")])
    assert pdf_text_extraction.extract_pdf_text(pdf) == reference_text(pdf)
    assert _same_path(directories[0].parent, shared)
    assert not shared_marker.exists()
    assert not private_marker.exists()
    # Control: the same search path, passed unfiltered to an interpreter that
    # starts with -P in a directory planted the same way, does import a plant.
    control_directory = shared / "control"
    control_directory.mkdir()
    _plant_module(control_directory, private_marker)
    unfiltered = {**pdf_text_extraction.worker_environment(), "PYTHONPATH": search_path}
    subprocess.run(
        [sys.executable, "-P", "-c", "pass"],
        cwd=control_directory,
        env=unfiltered,
        check=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )
    assert shared_marker.exists() or private_marker.exists()


def test_a_private_directory_that_cannot_be_made_is_unavailable_and_starts_nothing(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    processes = _record_started_processes(monkeypatch)

    def no_directory():
        """Fail as a full or unwritable temporary directory would."""
        raise OSError("injected: no space left for a directory")

    monkeypatch.setattr(pdf_text_extraction, "_create_private_directory", no_directory)
    _unavailable_from()
    assert _logged_category(caplog) == "working-directory-failed"
    assert processes == []


def test_a_private_directory_that_cannot_be_removed_is_logged_and_the_result_stands(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    directories = _record_private_directories(monkeypatch)
    real_rmtree = shutil.rmtree

    def refusing_rmtree(path, *args, **kwargs):
        """Fail as a directory something still holds open would."""
        raise PermissionError("injected: directory in use")

    monkeypatch.setattr(shutil, "rmtree", refusing_rmtree)
    pdf = build_pdf([text_operations("kept despite the cleanup")])
    assert pdf_text_extraction.extract_pdf_text(pdf) == "kept despite the cleanup"
    monkeypatch.setattr(shutil, "rmtree", real_rmtree)
    lines = _logged_lines(caplog)
    assert lines == ["PDF text extraction cleanup: category=working-directory-not-removed"]
    assert str(directories[0]) not in caplog.text
    shutil.rmtree(directories[0])


# ── Lifecycle: anything but a trusted, well-formed result is "unavailable" ──


def test_a_worker_that_cannot_start_is_unavailable(monkeypatch, tmp_path, caplog):
    missing_interpreter = tmp_path / "no-such-python.exe"

    def missing_command(limits=None):
        """Return an argv whose executable does not exist."""
        return [str(missing_interpreter), "worker.py"]

    monkeypatch.setattr(pdf_text_extraction, "worker_command", missing_command)
    directories = _record_private_directories(monkeypatch)
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    _unavailable_from()
    assert "category=launch-failed" in caplog.text
    assert len(directories) == 1
    assert not directories[0].exists()


def test_an_unknown_interpreter_path_is_unavailable(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    _unavailable_from()


@pytest.mark.parametrize(
    ("mode", "category"),
    [
        ("exit-nonzero", "worker-failed"),
        ("garbage-token", "malformed-result"),
        ("undecodable-text", "malformed-result"),
        ("no-separator", "malformed-result"),
        ("silent", "malformed-result"),
        ("trailing-payload", "malformed-result"),
        ("two-field-header", "malformed-result"),
        ("four-field-header", "malformed-result"),
        ("double-space-header", "malformed-result"),
        ("oversized-header", "malformed-result"),
        ("unavailable", "worker-unavailable"),
        ("unavailable-before-any-ceiling", "worker-unavailable"),
        ("text-without-ceiling", "no-ceiling"),
        ("refusal-without-ceiling", "no-ceiling"),
        ("text-under-unknown-ceiling", "no-ceiling"),
        ("text-from-old-parser", "parser-version"),
        ("refusal-from-old-parser", "parser-version"),
        ("text-with-unparsable-version", "parser-version"),
        ("verdict-without-ready", "malformed-result"),
        ("refusal-without-ready", "malformed-result"),
        ("die-before-ready", "worker-failed"),
        ("terminated-before-ready", "worker-failed"),
        ("partial-result-then-exit-zero", "malformed-result"),
        ("ready-then-other-ceiling", "malformed-result"),
        ("ready-then-other-version", "malformed-result"),
        ("ready-then-unavailable", "worker-unavailable"),
        ("ready-twice", "malformed-result"),
        ("untrusted-ready-then-die", "no-ceiling"),
        ("old-parser-ready-then-die", "parser-version"),
    ],
)
def test_anything_but_a_trusted_well_formed_result_is_unavailable(monkeypatch, caplog, mode, category):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    _use_probe(monkeypatch, mode)
    processes = _record_started_processes(monkeypatch)
    _unavailable_from()
    assert _logged_category(caplog) == category
    assert _logged_record(caplog).levelno == logging.ERROR
    assert processes[0].returncode is not None


# ── The ready line: a worker that dies after it died on the document ────────


@pytest.mark.parametrize(
    ("mode", "exit_status"),
    [
        pytest.param("die-after-ready", NONZERO_EXIT_STATUS, id="abnormal-exit"),
        pytest.param("exit-after-ready", 0, id="exit-zero-with-nothing-more"),
        pytest.param("terminated-after-ready", TERMINATED_EXIT_STATUS, id="ended-by-the-operating-system"),
        pytest.param("partial-result-then-die", NONZERO_EXIT_STATUS, id="abnormal-exit-mid-result-line"),
    ],
)
def test_a_worker_that_dies_after_its_ready_line_is_refused_as_over_the_limits(
    monkeypatch, caplog, mode, exit_status
):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    _use_probe(monkeypatch, mode)
    processes = _record_started_processes(monkeypatch)
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    refusal = _refusal_of(b"%PDF-1.4")
    assert str(refusal) == _OVER_LIMITS
    record = _logged_record(caplog)
    assert record.levelno == logging.WARNING
    assert _logged_category(caplog) == "died-while-parsing"
    assert f"exit_status={exit_status} ceiling=windows-job-object parser_version=999.0.0" in record.getMessage()
    assert processes[0].returncode == exit_status
    _assert_cleaned_up(processes, threads, directories)


@pytest.mark.slow
def test_a_worker_past_the_deadline_after_its_ready_line_is_still_the_timeout_refusal(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    started_marker, survived_marker = tmp_path / "started", tmp_path / "survived"
    monkeypatch.setattr(pdf_text_extraction, "PDF_EXTRACTION_DEADLINE_SECONDS", _SHORT_DEADLINE_SECONDS)
    _use_probe(
        monkeypatch, "ready-then-hang", str(started_marker), str(survived_marker), str(_PROBE_SLEEP_SECONDS)
    )
    processes = _record_started_processes(monkeypatch)
    threads = _record_pipe_threads(monkeypatch)
    directories = _record_private_directories(monkeypatch)
    began = time.monotonic()
    assert str(_refusal_of(b"%PDF-1.4")) == _TIMED_OUT
    assert time.monotonic() - began < _SHORT_DEADLINE_SECONDS + _KILL_ALLOWANCE_SECONDS
    assert _logged_category(caplog) == "timeout"
    _assert_cleaned_up(processes, threads, directories)
    assert started_marker.exists()
    _sleep_until(began + _PROBE_SLEEP_SECONDS + _SURVIVAL_MARGIN_SECONDS)
    assert not survived_marker.exists()


def test_an_untrusted_verdict_is_logged_with_the_ceiling_and_version_it_reported(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    _use_probe(monkeypatch, "refusal-from-old-parser")
    _unavailable_from()
    assert "category=parser-version" in caplog.text
    assert "ceiling=windows-job-object parser_version=0.0.1" in caplog.text


@pytest.mark.parametrize("mode", ["text-under-job-object", "text-under-rlimit"])
def test_text_under_either_enforced_ceiling_from_a_recent_pypdf_is_accepted(monkeypatch, mode):
    _use_probe(monkeypatch, mode)
    assert pdf_text_extraction.extract_pdf_text(b"%PDF-1.4") == "probe text"


def test_a_refusal_under_an_enforced_ceiling_from_a_recent_pypdf_is_believed(monkeypatch):
    _use_probe(monkeypatch, "refusal-under-rlimit")
    assert str(_refusal_of(b"%PDF-1.4")) == _ENCRYPTED


def test_text_with_lone_surrogates_round_trips_from_the_worker(monkeypatch):
    _use_probe(monkeypatch, "surrogate")
    assert pdf_text_extraction.extract_pdf_text(b"%PDF-1.4") == _SURROGATE_TEXT


# ── The command is built from the constants at call time ────────────────────


def test_worker_command_runs_this_interpreter_in_safe_path_mode_with_current_limits(monkeypatch):
    monkeypatch.setattr(pdf_text_extraction, "PDF_MAX_PAGES", 7)
    command = pdf_text_extraction.worker_command()
    assert command[:3] == [sys.executable, "-P", str(pdf_text_extraction.WORKER_PATH)]
    limits = json.loads(command[3])
    assert limits == pdf_text_extraction.worker_limits()
    assert limits["max_pages"] == 7
    assert limits["parser_minimum_version"] == PDF_PARSER_MINIMUM_VERSION


def test_a_real_worker_given_a_minimum_above_its_pypdf_is_unavailable(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    monkeypatch.setattr(pdf_text_extraction, "PDF_PARSER_MINIMUM_VERSION", "99.0")
    _unavailable_from(build_pdf([text_operations("never parsed")]))
    assert _logged_category(caplog) == "worker-unavailable"
    assert f"ceiling={_PLATFORM_CEILING} parser_version={pypdf.__version__}" in caplog.text


# ── Isolation: PDF safeguards live only in the worker ───────────────────────


def test_extraction_never_changes_this_process_pypdf():
    hooks = PYPDF_STREAM_LIMIT_ATTRIBUTES + (PYPDF_DECODER_ATTRIBUTE,)
    before = {name: getattr(pypdf.filters, name) for name in hooks}
    pdf = build_pdf([text_operations("isolated")])
    assert pdf_text_extraction.extract_pdf_text(pdf) == "isolated"
    with pytest.raises(EvidenceExtractionError):
        pdf_text_extraction.extract_pdf_text(build_pdf([None] * 501))
    for name in hooks:
        assert getattr(pypdf.filters, name) is before[name]


def test_the_api_side_of_evidence_intake_never_imports_pypdf():
    program = "import sys, src.services.evidence_intake; print('pypdf' in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=_REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


# ── The real worker, read from outside while it parses (Windows) ────────────


class _ReadyWatchingOutput:
    """The worker's stdout as the reader thread reads it, calling on_ready once, the moment the ready line arrives.

    read(size) returns whatever the pipe holds (read1) rather than waiting
    for size bytes, so the ready line is seen as soon as the worker flushes
    it; the reader still receives every byte, in order, and b"" at the end.
    """

    def __init__(self, stream, on_ready) -> None:
        """Wrap the worker's stdout; on_ready is called once, with the ready line."""
        self._stream = stream
        self._on_ready = on_ready
        self._head = bytearray()
        self._announced = False

    @property
    def closed(self) -> bool:
        """Return whether the wrapped stdout is closed."""
        return self._stream.closed

    def close(self) -> None:
        """Close the wrapped stdout."""
        self._stream.close()

    def read(self, size: int) -> bytes:
        """Return what the pipe holds, up to size bytes, after calling on_ready if this completes the first line."""
        chunk = self._stream.read1(size)
        if not self._announced:
            self._head += chunk
            line, separator, _rest = self._head.partition(b"\n")
            if separator:
                self._announced = True
                self._on_ready(bytes(line))
        return chunk


def _read_from_outside_at_ready(monkeypatch) -> dict:
    """Make the next worker be read from outside, through Windows, the moment its ready line reaches this process.

    The reading runs in the reader thread before the ready line is handed on,
    while the worker parses. It keeps the process view, the directory the
    worker was given, whether that is the current directory in the view, and
    whether the worker was still running once read. A failure is kept rather
    than raised, so the reader thread still drains the worker.
    """
    seen: dict = {}
    real_start = pdf_text_extraction._start_worker

    def start_and_watch(command, working_directory):
        """Start the worker the real way, then watch its stdout for the ready line."""
        process = real_start(command, working_directory)
        handle = int(process._handle)

        def read_at_ready(ready_line: bytes) -> None:
            """Read the worker from outside and keep what was read, or why it could not be."""
            seen["ready_line"] = ready_line
            try:
                seen["view"] = outside_view.read_process(handle)
                seen["running_once_read"] = outside_view.is_running(handle)
                seen["in_its_directory"] = os.path.samefile(seen["view"].current_directory, working_directory)
            except Exception as exc:
                seen["failure"] = exc

        seen["working_directory"] = Path(working_directory)
        process.stdout = _ReadyWatchingOutput(process.stdout, read_at_ready)
        return process

    monkeypatch.setattr(pdf_text_extraction, "_start_worker", start_and_watch)
    return seen


def _view_read_while_running(seen: dict) -> outside_view.ProcessView:
    """Return the view read at the ready line, failing unless it was read from the running worker in its own directory."""
    assert "failure" not in seen, seen.get("failure")
    assert seen["ready_line"].startswith(b"ready ")
    assert seen["running_once_read"] is True
    assert seen["in_its_directory"] is True
    return seen["view"]


def _slow_pdf() -> bytes:
    """Return the legitimate document slow enough that the worker is still parsing when it is read from outside."""
    line = _SLOW_LINE_PHRASE * _SLOW_LINE_REPEATS
    pages = [
        text_operations(*[f"{page:03d} {line}"] * _SLOW_DOCUMENT_LINES_PER_PAGE) for page in range(_SLOW_DOCUMENT_PAGES)
    ]
    return build_pdf(pages)


def _assert_whole_slow_text(text: str) -> None:
    """Assert the slow document's text came back whole: its phrase as often as every line of every page shows it."""
    shown = _SLOW_DOCUMENT_PAGES * _SLOW_DOCUMENT_LINES_PER_PAGE * _SLOW_LINE_REPEATS
    assert text.count(_SLOW_LINE_PHRASE.strip()) == shown


def _env_example_names() -> set[str]:
    """Return every variable .env.example assigns: the name opening each uncommented NAME=value line."""
    names = set()
    for line in _ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        assignment = _ENV_EXAMPLE_ASSIGNMENT.match(line)
        if assignment:
            names.add(assignment.group(1))
    return names


def _names_in(view: outside_view.ProcessView) -> set[str]:
    """Return the variable names in a process view, upper-cased as Windows compares them."""
    return {name.upper() for name in view.environment}


# Assignments a .env file above the worker file, beside it, and in the
# worker's working directory would make: where a .env loader would look.
_DOTENV_ABOVE = {"DATABASE_URL": "postgresql://kerno-dotenv-above@127.0.0.1:1/none", "KERNO_DOTENV_ABOVE": "dotenv-above"}
_DOTENV_BESIDE = {"KERNO_JWT_SECRET": "kerno-dotenv-beside-secret", "KERNO_DOTENV_BESIDE": "dotenv-beside"}
_DOTENV_WORKING_DIRECTORY = {"MISTRAL_API_KEY": "kerno-dotenv-cwd-key", "KERNO_DOTENV_CWD": "dotenv-working-directory"}
_DOTENV_ASSIGNMENTS = {**_DOTENV_ABOVE, **_DOTENV_BESIDE, **_DOTENV_WORKING_DIRECTORY}


def _write_dotenv(path: Path, assignments: dict[str, str]) -> None:
    """Write assignments to path as a .env file, one NAME=value line each."""
    lines = [f"{name}={value}\n" for name, value in assignments.items()]
    path.write_text("".join(lines), encoding="utf-8")


def _plant_dotenv_files(monkeypatch, above: Path, beside: Path) -> list[Path]:
    """Write a .env above and beside the worker file and into each private directory made; return those directories."""
    beside.mkdir(parents=True)
    _write_dotenv(above / ".env", _DOTENV_ABOVE)
    _write_dotenv(beside / ".env", _DOTENV_BESIDE)

    def plant_in_working_directory(directory: Path) -> None:
        """Put a .env in the worker's own directory before the worker starts there."""
        _write_dotenv(directory / ".env", _DOTENV_WORKING_DIRECTORY)

    return _record_private_directories(monkeypatch, plant=plant_in_working_directory)


if _ON_WINDOWS:

    @pytest.mark.slow
    def test_read_from_outside_while_it_parses_the_real_worker_holds_no_credential_and_no_name_from_env_example(
        monkeypatch,
    ):
        example_names = _env_example_names()
        assert {"DATABASE_URL", "KERNO_MIGRATION_DATABASE_URL", "KERNO_JWT_SECRET", "MISTRAL_API_KEY"} <= example_names
        assert example_names.isdisjoint(pdf_text_extraction.WORKER_ENVIRONMENT_ALLOWLIST)
        sentinels = {name: f"kerno-sentinel-{name.lower()}" for name in example_names} | _CREDENTIAL_SENTINELS
        for name, value in sentinels.items():
            monkeypatch.setenv(name, value)
        seen = _read_from_outside_at_ready(monkeypatch)
        _assert_whole_slow_text(pdf_text_extraction.extract_pdf_text(_slow_pdf()))
        view = _view_read_while_running(seen)
        child_names = _names_in(view)
        assert {"PATH", "SYSTEMROOT"} <= child_names
        assert child_names.isdisjoint(sentinels)
        for value in sentinels.values():
            assert value not in view.environment_block
        assert child_names - set(pdf_text_extraction.WORKER_ENVIRONMENT_ALLOWLIST) <= _PLATFORM_ADDED_NAMES
        assert str(pdf_text_extraction.WORKER_PATH) in view.command_line
        assert seen["working_directory"].name.startswith(_PRIVATE_DIRECTORY_PREFIX)
        assert not seen["working_directory"].exists()

    @pytest.mark.slow
    def test_no_dotenv_file_in_beside_or_above_the_workers_directories_reaches_the_real_worker(monkeypatch, tmp_path):
        above, beside = tmp_path / "above", tmp_path / "above" / "beside"
        directories = _plant_dotenv_files(monkeypatch, above, beside)
        worker_copy = beside / pdf_text_extraction.WORKER_PATH.name
        shutil.copyfile(pdf_text_extraction.WORKER_PATH, worker_copy)
        monkeypatch.setattr(pdf_text_extraction, "WORKER_PATH", worker_copy)
        seen = _read_from_outside_at_ready(monkeypatch)
        _assert_whole_slow_text(pdf_text_extraction.extract_pdf_text(_slow_pdf()))
        view = _view_read_while_running(seen)
        assert str(worker_copy) in view.command_line
        assert _names_in(view).isdisjoint(_DOTENV_ASSIGNMENTS)
        for value in _DOTENV_ASSIGNMENTS.values():
            assert value not in view.environment_block
        assert [directory.parent for directory in directories] == [Path(tempfile.gettempdir())]
        assert not directories[0].exists()

    @pytest.mark.slow
    def test_read_from_outside_a_worker_that_loaded_the_same_dotenv_files_shows_every_value_it_loaded(
        monkeypatch, tmp_path
    ):
        above, beside = tmp_path / "above", tmp_path / "above" / "beside"
        _plant_dotenv_files(monkeypatch, above, beside)
        hold = str(_DOTENV_PROBE_HOLD_SECONDS)
        _use_probe(monkeypatch, "load-dotenv", hold, ".env", str(above / ".env"), str(beside / ".env"))
        seen = _read_from_outside_at_ready(monkeypatch)
        assert pdf_text_extraction.extract_pdf_text(b"%PDF-1.4") == "loaded"
        view = _view_read_while_running(seen)
        for name, value in _DOTENV_ASSIGNMENTS.items():
            assert view.environment[name] == value


# ── The real ceiling, shown on its own (Windows) ────────────────────────────


class _CommitWatcher:
    """Watches the first recorded worker's committed memory from a thread, killing it once it passes a threshold."""

    def __init__(self, processes: list, threshold: int) -> None:
        """Prepare, without starting, a watch on the worker processes will hold."""
        self._processes = processes
        self._threshold = threshold
        self.highest_seen = 0
        self.killed_at: int | None = None
        self._thread = threading.Thread(target=self._watch, name="commit-watcher", daemon=True)

    def start(self) -> None:
        """Start watching."""
        self._thread.start()

    def join(self) -> None:
        """Wait for the watch to end, failing the test if it never does."""
        self._thread.join(_WATCH_BOUND_SECONDS)
        assert not self._thread.is_alive()

    def _watch(self) -> None:
        """Wait for the worker to start, then poll its committed memory until it exits or passes the threshold."""
        give_up_at = time.monotonic() + _WATCH_BOUND_SECONDS
        while not self._processes and time.monotonic() < give_up_at:
            time.sleep(_WATCH_POLL_SECONDS)
        process = self._processes[0]
        handle = int(process._handle)
        while outside_view.is_running(handle) and time.monotonic() < give_up_at:
            current = outside_view.committed_memory(handle).current
            self.highest_seen = max(self.highest_seen, current)
            if current > self._threshold:
                self.killed_at = current
                process.kill()
                return
            time.sleep(_WATCH_POLL_SECONDS)


def _growing_pdf() -> bytes:
    """Return the document whose memory grows gradually: a ToUnicode map showing 4,000,000 characters 400 times."""
    return build_pdf_with_amplifying_to_unicode(_GROWING_MAPPED_CHARACTERS, _GROWING_TIMES_SHOWN)


def _watched_harness_run(monkeypatch, caplog, mode: str) -> tuple[EvidenceExtractionError, str, _CommitWatcher, int]:
    """Run the growing document through extract_pdf_text in the counterfactual harness, watched.

    Returns the refusal, its logged category, the watcher, and the worker's
    peak committed memory read from its handle after it was reaped.
    """
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
    limits_json = json.dumps(pdf_text_extraction.worker_limits())

    def harness_command(limits=None):
        """Return the harness's argv, carrying the API's own worker limits."""
        return [sys.executable, "-P", str(_UNCAPPED_PROBE_PATH), mode, str(pdf_text_extraction.WORKER_PATH), limits_json]

    monkeypatch.setattr(pdf_text_extraction, "worker_command", harness_command)
    processes = _record_started_processes(monkeypatch)
    watcher = _CommitWatcher(processes, _PAST_THE_CEILING_THRESHOLD_BYTES)
    watcher.start()
    refusal = _refusal_of(_growing_pdf())
    watcher.join()
    peak = outside_view.committed_memory(int(processes[0]._handle)).peak
    return refusal, _logged_category(caplog), watcher, peak


if _ON_WINDOWS:

    def test_under_the_real_ceiling_memory_is_refused_within_one_step_of_512_mib_and_never_past_it(tmp_path):
        command = [
            sys.executable,
            "-P",
            str(_PROBE_PATH),
            "allocate-until-refused",
            str(pdf_text_extraction.WORKER_PATH),
            json.dumps(pdf_text_extraction.worker_limits()),
            str(_ALLOCATION_STEP_BYTES),
        ]
        with subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=pdf_text_extraction.worker_environment(),
            cwd=tmp_path,
        ) as process:
            stdout, stderr = process.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
        report = json.loads(stdout)
        peak = outside_view.committed_memory(int(process._handle)).peak
        assert (process.returncode, stderr) == (0, b"")
        assert (report["ceiling"], report["outcome"], report["step_bytes"]) == (
            "windows-job-object", "MemoryError", _ALLOCATION_STEP_BYTES,
        )
        assert report["committed_before"] + report["bytes_reached"] <= peak <= PDF_WORKER_MEMORY_LIMIT_BYTES
        assert PDF_WORKER_MEMORY_LIMIT_BYTES - peak < _ALLOCATION_STEP_BYTES + _ALLOCATION_BOOKKEEPING_BYTES

    @pytest.mark.slow
    def test_a_document_whose_memory_grows_gradually_is_refused_close_to_the_real_ceiling_and_never_above_it(
        monkeypatch, caplog
    ):
        caplog.set_level(logging.INFO, logger=_LOGGER_NAME)
        processes = _record_started_processes(monkeypatch)
        threads = _record_pipe_threads(monkeypatch)
        directories = _record_private_directories(monkeypatch)
        refusal = _refusal_of(_growing_pdf())
        assert str(refusal) == _OVER_LIMITS
        assert refusal.__cause__ is None
        assert refusal.__context__ is None
        assert _logged_category(caplog) in {"over-limits", "died-while-parsing"}
        _assert_cleaned_up(processes, threads, directories)
        peak = outside_view.committed_memory(int(processes[0]._handle)).peak
        assert PDF_WORKER_MEMORY_LIMIT_BYTES - _CLOSE_TO_CEILING_BYTES <= peak <= PDF_WORKER_MEMORY_LIMIT_BYTES

    @pytest.mark.slow
    def test_in_the_counterfactual_harness_the_real_ceiling_still_stops_the_document_under_512_mib(monkeypatch, caplog):
        refusal, category, watcher, peak = _watched_harness_run(monkeypatch, caplog, "capped")
        assert str(refusal) == _OVER_LIMITS
        assert category in {"over-limits", "died-while-parsing"}
        assert watcher.killed_at is None
        assert watcher.highest_seen <= peak <= PDF_WORKER_MEMORY_LIMIT_BYTES
        assert peak >= PDF_WORKER_MEMORY_LIMIT_BYTES - _CLOSE_TO_CEILING_BYTES

    @pytest.mark.slow
    def test_with_only_its_ceiling_installation_replaced_the_same_worker_grows_past_512_mib_on_the_same_document(
        monkeypatch, caplog
    ):
        refusal, category, watcher, peak = _watched_harness_run(monkeypatch, caplog, "uncapped")
        assert watcher.killed_at is not None, f"never passed the threshold; highest {watcher.highest_seen}"
        assert watcher.killed_at > _PAST_THE_CEILING_THRESHOLD_BYTES > PDF_WORKER_MEMORY_LIMIT_BYTES
        assert peak >= watcher.killed_at
        assert (str(refusal), category) == (_OVER_LIMITS, "died-while-parsing")
