"""
test_pdf_text_worker.py — the PDF worker's own decisions, proven against real PDFs.

What:  Runs src/services/pdf_text_worker.py's logic in this process against
       PDFs built with pypdf: limit validation; the order of a run (OS
       ceiling installed and verified, then pypdf imported, version-checked,
       hooked, observed and self-tested, then the ready line written and
       flushed, then the upload read and parsed); a failed ceiling step or a
       failed parser check answering "unavailable" with nothing read or
       parsed and no ready line; the process-wide observer that refuses a
       document whenever a MemoryError or LimitReachedError is raised, shown
       at every site where pypdf 6.14.2 catches and ignores one (each with a
       control run whose observer is switched off, which returns text), its
       self-tests, and the worker failing closed without sys.monitoring or a
       free tool id; JBIG2 streams starting no program; the page cap; the
       text budget, charging a Form XObject's text once at any depth; the
       per-stream limit held after decoding too (a Flate-then-ASCII85
       stream) and the cumulative decoded budget, each shown stopping work
       early; encryption, malformed input, MemoryError; the ready and result
       lines and the private result channel; and that the joined text is
       exactly today's formula. The OS calls are faked in this process (the
       real ceiling would cap the test run itself), on both platforms'
       paths whatever the host, and exercised for real in child processes:
       the real worker reports its platform's ceiling, refuses a Form
       XObject amplifier pypdf would have skipped, keeps quiet about a form
       pypdf merely cannot decode, runs no program with jbig2dec on its
       PATH, answers "unavailable" without sys.monitoring, and refuses a
       planted, swallowed failure all the way through the API's own
       extraction (422); a probe that installs the ceiling shows it read
       back from outside, an allocation past it failing and, on Windows, a
       child process refused.
Why:   The worker is the only place pypdf runs. Every limit it claims must be
       shown to bind on a real document, and every safeguard it cannot put
       in place must fail closed. Each test restores every pypdf global it
       changes, pypdf's logger, and every sys.monitoring tool id a run
       claims. No test here is ever skipped: one that needs a real child of
       one platform's own ceiling is defined only on that platform (the
       real rlimit ones are exercised on a POSIX host such as WSL).
How:   pytest tests/unit/services/test_pdf_text_worker.py -v
       Only the API-path test imports src.services.pdf_text_extraction (and
       with it config); the rest of the worker side stays portable.
"""

from __future__ import annotations

import ast
import contextlib
import ctypes
import functools
import io
import json
import logging
import os
import subprocess
import sys
import zlib
from pathlib import Path

import pypdf
import pypdf._page
import pypdf._utils
import pypdf.errors
import pypdf.filters
import pypdf.generic
import pytest

from src.exceptions import EvidenceExtractionError
from src.services import pdf_text_extraction, pdf_text_worker
from src.services.pdf_text_worker import (
    CEILING_NONE,
    CEILING_POSIX_RLIMIT,
    CEILING_WINDOWS_JOB_OBJECT,
    OBSERVER_TOOL_NAME,
    PARSER_VERSION_UNKNOWN,
    PYPDF_DECODER_ATTRIBUTE,
    PYPDF_ERROR_ATTRIBUTES,
    PYPDF_EXTERNAL_DECODER_ATTRIBUTE,
    PYPDF_STREAM_ATTRIBUTES,
    PYPDF_STREAM_LIMIT_ATTRIBUTES,
    RESULT_ENCRYPTED,
    RESULT_OVER_LIMITS,
    RESULT_TEXT,
    RESULT_TOO_MANY_PAGES,
    RESULT_UNAVAILABLE,
    RESULT_UNREADABLE,
    ResultHeader,
    WorkerRefusal,
    WorkerResult,
)
from tests.unit.services._pdf_documents import (
    AMPLIFIED_FORM_PAGE_TEXT,
    FLATE_THEN_ASCII85,
    build_pdf,
    build_pdf_with_amplifying_encoding,
    build_pdf_with_amplifying_form,
    build_pdf_with_base_encoding,
    build_pdf_with_deflated_page,
    build_pdf_with_encoded_page,
    build_pdf_with_form,
    build_pdf_with_jbig2_form,
    build_pdf_with_jbig2_page,
    build_pdf_with_nested_forms,
    build_pdf_with_nested_page_dictionary,
    build_pdf_with_undecodable_form,
    build_pdf_with_unencoded_font,
    build_pdf_without_pages_marking_its_catalog,
    deflated_text_with_padding,
    flate_then_ascii85,
    padded,
    reference_text,
    text_operations,
    zero_padded,
)
from tests.unit.services.pdf_worker_probes import swallow_sites

_REAL_INSTALL_CEILING = pdf_text_worker.install_ceiling
_WORKER_PATH = Path(pdf_text_worker.__file__).resolve()
_PROBES = Path(__file__).resolve().parent / "pdf_worker_probes"
_PROBE_PATH = _PROBES / "probe_worker.py"
_REAL_WORKER_PROBE_PATH = _PROBES / "probe_real_worker.py"
_MEBIBYTE = 1024 * 1024
_CHILD_TIMEOUT_SECONDS = 120
_ON_WINDOWS = sys.platform == "win32"
_WINDOWS = "win32"
_POSIX = "linux"
_PLATFORM_CEILING = CEILING_WINDOWS_JOB_OBJECT if _ON_WINDOWS else CEILING_POSIX_RLIMIT
_INSTALLED_PYPDF = pypdf.__version__
_PYPDF_FILTER_HOOKS = PYPDF_STREAM_LIMIT_ATTRIBUTES + (PYPDF_DECODER_ATTRIBUTE, PYPDF_EXTERNAL_DECODER_ATTRIBUTE)
# The Job Object flags (JOB_OBJECT_LIMIT_PROCESS_MEMORY | _PROCESS_TIME |
# _ACTIVE_PROCESS | _KILL_ON_JOB_CLOSE), the one process the job may hold,
# the extended-limit information class and the 100-ns CPU-time units, written
# out here so a change in the worker fails a test.
_JOB_LIMIT_FLAGS = 0x100 | 0x2 | 0x8 | 0x2000
_JOB_ACTIVE_PROCESS_LIMIT = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_TIME_UNITS_PER_SECOND = 10_000_000
# ERROR_NOT_ENOUGH_QUOTA: what CreateProcess answers in a job already holding
# its ActiveProcessLimit.
_WINDOWS_NOT_ENOUGH_QUOTA = 1816

_MONITORING = sys.monitoring
_MONITORING_TOOL_IDS = range(pdf_text_worker._MONITORING_TOOL_ID_COUNT)
_OCCUPYING_TOOL_NAME = "kerno-test-occupant"

_GENEROUS_LIMITS = {
    "max_input_bytes": 10 * _MEBIBYTE,
    "max_pages": 500,
    "max_stream_output_bytes": 16 * _MEBIBYTE,
    "max_decoded_bytes": 64 * _MEBIBYTE,
    "max_text_characters": 10 * _MEBIBYTE,
    "memory_limit_bytes": 512 * _MEBIBYTE,
    "cpu_limit_seconds": 31,
    "parser_minimum_version": "6.14.2",
}
_UNAVAILABLE_BEFORE_ANY_CEILING = WorkerResult(RESULT_UNAVAILABLE, CEILING_NONE, PARSER_VERSION_UNKNOWN, "")
_UNAVAILABLE_AFTER_THE_CEILING = WorkerResult(RESULT_UNAVAILABLE, _PLATFORM_CEILING, _INSTALLED_PYPDF, "")
# Every run decodes the parser self-test's two streams through the same
# decoder before the document: the short one, then the oversized one that
# pypdf's per-stream limit stops.
_SELF_TEST_DECODES = [len(pdf_text_worker._SELF_TEST_CONTENT), pypdf.errors.LimitReachedError]
_PYPDF_LOGGER = logging.getLogger("pypdf")
_PAGE_THEN_SKIPPED_FORM = "page text\n"
_EXTRACTION_LOGGER_NAME = pdf_text_extraction.__name__
# Held here so a test that deletes pypdf.PageObject cannot hide it from the
# snapshot below.
_PAGE_CLASS = pypdf.PageObject
# Recorded for a pypdf attribute that is missing when the snapshot is taken.
_ABSENT = object()


def _worker_patched_attributes() -> list[tuple[object, str, object]]:
    """Return every pypdf attribute a worker run may replace, with its current value or _ABSENT."""
    owners = [(pypdf.filters, name) for name in _PYPDF_FILTER_HOOKS]
    owners.append((_PAGE_CLASS, "extract_xform_text"))
    return [(owner, name, getattr(owner, name, _ABSENT)) for owner, name in owners]


def _restore_attributes(attributes: list[tuple[object, str, object]]) -> None:
    """Put each attribute back to its recorded value, deleting one that was absent and now exists."""
    for owner, name, value in attributes:
        if value is not _ABSENT:
            setattr(owner, name, value)
        elif hasattr(owner, name):
            delattr(owner, name)


def _worker_observer_tool_ids() -> list[int]:
    """Return the sys.monitoring tool ids a worker run's observer holds in this process."""
    return [tool_id for tool_id in _MONITORING_TOOL_IDS if _MONITORING.get_tool(tool_id) == OBSERVER_TOOL_NAME]


def _release_worker_observers() -> None:
    """Switch off and free every tool id a worker run's observer claimed in this process."""
    for tool_id in _worker_observer_tool_ids():
        _MONITORING.set_events(tool_id, _MONITORING.events.NO_EVENTS)
        _MONITORING.register_callback(tool_id, _MONITORING.events.RAISE, None)
        _MONITORING.free_tool_id(tool_id)


@contextlib.contextmanager
def _worker_side_effects_undone():
    """Put back afterwards what a worker run changes in this process: pypdf's hooks, its logger, the observer.

    A real worker runs once per process, so a run leaves these in place; in
    this process a second run must not wrap the first run's decoder or form
    hook, nor inherit its observer.
    """
    attributes_before = _worker_patched_attributes()
    handlers_before = list(_PYPDF_LOGGER.handlers)
    level_before = _PYPDF_LOGGER.level
    try:
        yield
    finally:
        _restore_attributes(attributes_before)
        for handler in list(_PYPDF_LOGGER.handlers):
            if handler not in handlers_before:
                _PYPDF_LOGGER.removeHandler(handler)
        _PYPDF_LOGGER.setLevel(level_before)
        _release_worker_observers()


@pytest.fixture(autouse=True)
def ceiling_calls(monkeypatch):
    """Undo every worker side effect afterwards, and record instead of installing OS ceilings."""
    calls = []

    def record_ceiling(limits, platform=None):
        """Record the limits the worker would install; never touch this process's real limits."""
        calls.append(("ceiling", dict(limits)))
        return _PLATFORM_CEILING

    monkeypatch.setattr(pdf_text_worker, "install_ceiling", record_ceiling)
    with _worker_side_effects_undone():
        yield calls


def _limits(**overrides) -> dict:
    """Return generous worker limits with the given entries replaced."""
    limits = dict(_GENEROUS_LIMITS)
    limits.update(overrides)
    return limits


def _ready_line(ceiling: str = _PLATFORM_CEILING, version: str = _INSTALLED_PYPDF) -> bytes:
    """Return the ready line the worker announces once every safeguard is in place."""
    return f"ready {ceiling} {version}\n".encode()


def _run_announcing(arguments: list[str], input_stream) -> tuple[WorkerResult, bytes]:
    """Run the worker's logic in this process and return its result and everything it wrote before deciding.

    Whatever the run changes in this process (pypdf's hooks, any spy a test
    installed included, its logger, its observer) is put back afterwards.
    """
    channel = io.BytesIO()
    with _worker_side_effects_undone():
        result = pdf_text_worker.run_worker(arguments, input_stream, channel)
    return result, channel.getvalue()


def _run_before_ready(arguments: list[str], input_stream) -> WorkerResult:
    """Run the worker's logic in this process and return its result, asserting no ready line was written."""
    result, announced = _run_announcing(arguments, input_stream)
    assert announced == b""
    return result


def _run_result(pdf: bytes, **overrides) -> WorkerResult:
    """Run the worker's logic in this process on pdf under generous limits plus overrides.

    Asserts the run announced exactly one ready line, naming the ceiling and
    version its result reports, before it decided.
    """
    result, announced = _run_announcing([json.dumps(_limits(**overrides))], io.BytesIO(pdf))
    assert announced == _ready_line(result.ceiling, result.parser_version)
    return result


def _run(pdf: bytes, **overrides) -> tuple[str, str]:
    """Return the (outcome, text) of one in-process run."""
    result = _run_result(pdf, **overrides)
    return result.outcome, result.text


def _spy_on_page_extraction(monkeypatch) -> list[int]:
    """Record one entry per page.extract_text call, still running pypdf's real extraction."""
    original = pypdf.PageObject.extract_text
    calls = []

    def recording_extract_text(page, *args, **kwargs):
        """Record the call, then extract as pypdf would."""
        calls.append(len(calls))
        return original(page, *args, **kwargs)

    monkeypatch.setattr(pypdf.PageObject, "extract_text", recording_extract_text)
    return calls


def _spy_on_stream_decoding(monkeypatch) -> list:
    """Record each decoded stream's size, or the exception pypdf's own decoder raised."""
    original = pypdf.filters.decode_stream_data
    outcomes = []

    def recording_decode(stream):
        """Decode with pypdf's real decoder and record what happened."""
        try:
            data = original(stream)
        except Exception as exc:
            outcomes.append(type(exc))
            raise
        outcomes.append(len(data))
        return data

    monkeypatch.setattr(pypdf.filters, "decode_stream_data", recording_decode)
    return outcomes


def _capture_stream_limits_during_extraction(monkeypatch) -> list[dict]:
    """Record pypdf's per-stream limit globals as each page's text is extracted, still extracting it."""
    original = pypdf.PageObject.extract_text
    seen = []

    def capturing_extract_text(page, *args, **kwargs):
        """Record the limits in force now, then extract as pypdf would."""
        seen.append({name: getattr(pypdf.filters, name) for name in PYPDF_STREAM_LIMIT_ATTRIBUTES})
        return original(page, *args, **kwargs)

    monkeypatch.setattr(pypdf.PageObject, "extract_text", capturing_extract_text)
    return seen


def _replace_page_extraction(monkeypatch, replacement) -> None:
    """Make every page.extract_text call run replacement instead of pypdf's extraction."""
    monkeypatch.setattr(pypdf.PageObject, "extract_text", replacement)


class _UnreadableInput:
    """An input stream that fails the test if the worker ever reads it."""

    def read(self, size=-1):
        """Fail: the document must not be read before every safeguard is in place."""
        raise AssertionError("the document was read")


def _forbid_parsing(monkeypatch) -> None:
    """Fail the test if the worker imports pypdf or constructs a PdfReader."""

    def must_not_import():
        """Fail: pypdf must not be imported without a verified ceiling."""
        raise AssertionError("pypdf was imported")

    def must_not_parse(*args, **kwargs):
        """Fail: nothing may be parsed once a safeguard has failed."""
        raise AssertionError("a document was parsed")

    monkeypatch.setattr(pdf_text_worker, "import_parser", must_not_import)
    monkeypatch.setattr(pypdf, "PdfReader", must_not_parse)


def _forbid_reading_and_parsing_the_document(monkeypatch) -> _UnreadableInput:
    """Fail the test if the worker reads the document or constructs a PdfReader; return the input to pass."""

    def must_not_parse(*args, **kwargs):
        """Fail: nothing may be parsed once a parser check has failed."""
        raise AssertionError("a document was parsed")

    monkeypatch.setattr(pypdf, "PdfReader", must_not_parse)
    return _UnreadableInput()


# ── Limits arrive as one JSON object ────────────────────────────────────────


def _without(key: str) -> dict:
    """Return generous limits with one key removed."""
    limits = _limits()
    del limits[key]
    return limits


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param([json.dumps(_without("max_pages"))], id="missing-key"),
        pytest.param([json.dumps(_without("parser_minimum_version"))], id="missing-parser-minimum"),
        pytest.param([json.dumps(_limits(unexpected=1))], id="unknown-key"),
        pytest.param([json.dumps(_limits(max_pages="500"))], id="string"),
        pytest.param([json.dumps(_limits(max_pages=500.0))], id="float"),
        pytest.param([json.dumps(_limits(max_pages=True))], id="bool"),
        pytest.param([json.dumps(_limits(max_pages=None))], id="null"),
        pytest.param([json.dumps(_limits(max_decoded_bytes=0))], id="zero"),
        pytest.param([json.dumps(_limits(cpu_limit_seconds=-1))], id="negative"),
        pytest.param([json.dumps(_limits(parser_minimum_version=6.14))], id="parser-minimum-not-a-string"),
        pytest.param([json.dumps(_limits(parser_minimum_version="six"))], id="parser-minimum-unparsable"),
        pytest.param([json.dumps(_limits(parser_minimum_version=""))], id="parser-minimum-empty"),
        pytest.param(["{not json"], id="not-json"),
        pytest.param([json.dumps([1, 2])], id="not-an-object"),
        pytest.param([], id="no-argument"),
        pytest.param([json.dumps(_limits()), "extra"], id="two-arguments"),
    ],
)
def test_invalid_limits_are_unavailable_before_any_ceiling_read_or_parse(arguments, ceiling_calls, monkeypatch):
    _forbid_parsing(monkeypatch)
    assert _run_before_ready(arguments, _UnreadableInput()) == _UNAVAILABLE_BEFORE_ANY_CEILING
    assert ceiling_calls == []


def test_input_beyond_max_input_bytes_is_over_limits_and_at_the_limit_is_read():
    pdf = build_pdf([text_operations("sized")])
    assert _run(pdf, max_input_bytes=len(pdf) - 1) == (RESULT_OVER_LIMITS, "")
    assert _run(pdf, max_input_bytes=len(pdf)) == (RESULT_TEXT, "sized")


# ── Fakes for the operating system's ceiling calls ──────────────────────────

_FAKE_JOB = 0x4A0B
_FAKE_CURRENT_PROCESS = 0x7FFF


class _FakeKernel32:
    """kernel32's Job Object calls, logged in order; one named step can fail or the read-back can lie."""

    def __init__(self, log: list, failing_step: str | None = None, misreport=None) -> None:
        """Log into log; failing_step names the call that returns failure; misreport edits the read-back."""
        self.log = log
        self.failing_step = failing_step
        self.misreport = misreport
        self.requested = None
        self.assigned = None

    def _succeeds(self, name: str) -> bool:
        """Log the call and return whether it is allowed to succeed."""
        self.log.append(name)
        return name != self.failing_step

    def CreateJobObjectW(self, attributes, name):
        """Return a job handle, or NULL for the failing step."""
        return _FAKE_JOB if self._succeeds("CreateJobObjectW") else None

    def SetInformationJobObject(self, job, information_class, information, size):
        """Keep a copy of the limits as Windows would, unless this is the failing step."""
        self.requested = (job, information_class, bytes(information._obj), size)
        return self._succeeds("SetInformationJobObject")

    def GetCurrentProcess(self):
        """Return the pseudo-handle of the current process."""
        return _FAKE_CURRENT_PROCESS

    def AssignProcessToJobObject(self, job, process):
        """Record the assignment, unless this is the failing step."""
        self.assigned = (job, process)
        return self._succeeds("AssignProcessToJobObject")

    def QueryInformationJobObject(self, job, information_class, information, size, returned_length):
        """Read back the limits that were set, edited by misreport, unless this is the failing step."""
        if not self._succeeds("QueryInformationJobObject"):
            return 0
        seen = information._obj
        ctypes.memmove(ctypes.addressof(seen), self.requested[2], ctypes.sizeof(seen))
        if self.misreport is not None:
            self.misreport(seen)
        return 1


def _without_flag(flag: int):
    """Return a misreport that reads the job back with one limit flag cleared."""

    def clear_the_flag(seen) -> None:
        """Read back the job as if this flag were not set."""
        seen.BasicLimitInformation.LimitFlags &= ~flag

    return clear_the_flag


def _with_a_higher_memory_limit(seen) -> None:
    """Read back a memory limit one byte above the one requested."""
    seen.ProcessMemoryLimit += 1


def _with_no_memory_limit(seen) -> None:
    """Read back a memory limit of zero."""
    seen.ProcessMemoryLimit = 0


def _with_a_higher_time_limit(seen) -> None:
    """Read back a CPU-time limit above the one requested."""
    seen.BasicLimitInformation.PerProcessUserTimeLimit += 1


def _with_no_time_limit(seen) -> None:
    """Read back a CPU-time limit of zero."""
    seen.BasicLimitInformation.PerProcessUserTimeLimit = 0


def _with_a_second_process_allowed(seen) -> None:
    """Read back an active-process limit of two."""
    seen.BasicLimitInformation.ActiveProcessLimit += 1


def _with_no_process_limit(seen) -> None:
    """Read back an active-process limit of zero."""
    seen.BasicLimitInformation.ActiveProcessLimit = 0


class _FakeResource:
    """A stand-in for the POSIX resource module that logs and records what is set."""

    RLIM_INFINITY = -1
    RLIMIT_AS = "address-space"
    RLIMIT_CPU = "cpu"

    def __init__(self, current: dict, refuse: bool = False, ignore: bool = False, log: list | None = None) -> None:
        """Start from the given (soft, hard) limits; refuse fails setrlimit; ignore makes it a silent no-op."""
        self.current = dict(current)
        self.refuse = refuse
        self.ignore = ignore
        self.log = [] if log is None else log

    def getrlimit(self, which):
        """Return the current (soft, hard) pair."""
        self.log.append(f"getrlimit {which}")
        return self.current[which]

    def setrlimit(self, which, limits):
        """Record the new pair, fail as a kernel refusal would, or silently keep the old pair."""
        self.log.append(f"setrlimit {which}")
        if self.refuse:
            raise ValueError("not allowed to change this limit")
        if not self.ignore:
            self.current[which] = limits


_UNLIMITED = _FakeResource.RLIM_INFINITY
_ALL_UNLIMITED = {"address-space": (_UNLIMITED, _UNLIMITED), "cpu": (_UNLIMITED, _UNLIMITED)}
_WINDOWS_STEPS = [
    "CreateJobObjectW",
    "SetInformationJobObject",
    "AssignProcessToJobObject",
    "QueryInformationJobObject",
]
_POSIX_STEPS = [
    "getrlimit address-space",
    "setrlimit address-space",
    "getrlimit cpu",
    "setrlimit cpu",
    "getrlimit address-space",
    "getrlimit cpu",
]
# Each platform's ceiling path, run through the real install_ceiling with the
# platform named explicitly and that platform's OS calls faked, on any host.
_FAKED_PLATFORMS = [
    pytest.param(_WINDOWS, _WINDOWS_STEPS, CEILING_WINDOWS_JOB_OBJECT, id="windows-job-object"),
    pytest.param(_POSIX, _POSIX_STEPS, CEILING_POSIX_RLIMIT, id="posix-rlimit"),
]


def _use_fake_operating_system(monkeypatch, log: list, platform: str, **failure) -> object:
    """Make the worker call the real install_ceiling for platform, with that platform's OS calls faked; return the fake."""

    def install_on_the_named_platform(limits, platform_override=None):
        """Install the ceiling as the worker would on platform."""
        return _REAL_INSTALL_CEILING(limits, platform=platform)

    monkeypatch.setattr(pdf_text_worker, "install_ceiling", install_on_the_named_platform)
    if platform == _WINDOWS:
        fake = _FakeKernel32(log, **failure)

        def load_fake_kernel32():
            """Return the fake in place of kernel32."""
            return fake

        monkeypatch.setattr(pdf_text_worker, "_load_kernel32", load_fake_kernel32)
        return fake
    fake = _FakeResource(_ALL_UNLIMITED, log=log, **failure)

    def import_fake_resource():
        """Return the fake in place of the resource module."""
        return fake

    monkeypatch.setattr(pdf_text_worker, "_import_resource_module", import_fake_resource)
    return fake


def _record_the_later_steps(monkeypatch, log: list) -> None:
    """Log the pypdf import, both self-tests and each PdfReader construction into log."""
    real_import = pdf_text_worker.import_parser
    real_self_test = pdf_text_worker.run_parser_self_test
    real_observer_self_test = pdf_text_worker.run_observer_self_test
    real_reader = pypdf.PdfReader

    def recording_import():
        """Log, then import pypdf as the worker would."""
        log.append("import pypdf")
        return real_import()

    def recording_self_test(parser, budget, observer):
        """Log, then run the real parser self-test."""
        log.append("parser self-test")
        return real_self_test(parser, budget, observer)

    def recording_observer_self_test(observer):
        """Log, then run the real observer self-test."""
        log.append("observer self-test")
        return real_observer_self_test(observer)

    def recording_reader(*args, **kwargs):
        """Log, then construct a real PdfReader."""
        log.append("parse")
        return real_reader(*args, **kwargs)

    monkeypatch.setattr(pdf_text_worker, "import_parser", recording_import)
    monkeypatch.setattr(pdf_text_worker, "run_parser_self_test", recording_self_test)
    monkeypatch.setattr(pdf_text_worker, "run_observer_self_test", recording_observer_self_test)
    monkeypatch.setattr(pypdf, "PdfReader", recording_reader)


class _RecordingInput(io.BytesIO):
    """An input stream that logs when the worker reads it."""

    def __init__(self, data: bytes, log: list) -> None:
        """Hold data and the shared log."""
        super().__init__(data)
        self.log = log

    def read(self, size=-1):
        """Log the read, then read."""
        self.log.append("read upload")
        return super().read(size)


class _RecordingChannel(io.BytesIO):
    """A result channel that logs what the worker writes to it and when it flushes."""

    def __init__(self, log: list) -> None:
        """Start empty, logging into the shared log."""
        super().__init__()
        self.log = log

    def write(self, data):
        """Log the bytes written, then keep them."""
        self.log.append(("write", bytes(data)))
        return super().write(data)

    def flush(self):
        """Log the flush."""
        self.log.append("flush")
        return super().flush()


# ── R1/R2 order: ceiling installed and verified, parser checked, ready, read ─


@pytest.mark.parametrize(("platform", "os_steps", "ceiling"), _FAKED_PLATFORMS)
def test_the_ceiling_is_verified_and_the_parser_checked_before_ready_is_announced_and_the_upload_read(
    monkeypatch, platform, os_steps, ceiling
):
    log = []
    _use_fake_operating_system(monkeypatch, log, platform)
    _record_the_later_steps(monkeypatch, log)
    pdf = build_pdf([text_operations("ordered")])
    channel = _RecordingChannel(log)
    result = pdf_text_worker.run_worker([json.dumps(_limits())], _RecordingInput(pdf, log), channel)
    assert result == WorkerResult(RESULT_TEXT, ceiling, _INSTALLED_PYPDF, "ordered")
    parser_steps = ["import pypdf", "parser self-test", "observer self-test"]
    announced = [("write", _ready_line(ceiling)), "flush"]
    assert log == os_steps + parser_steps + announced + ["read upload", "parse"]
    assert channel.getvalue() == _ready_line(ceiling)


@pytest.mark.parametrize(
    ("failure", "steps_taken"),
    [
        pytest.param({"failing_step": "CreateJobObjectW"}, 1, id="create-fails"),
        pytest.param({"failing_step": "SetInformationJobObject"}, 2, id="set-limits-fails"),
        pytest.param({"failing_step": "AssignProcessToJobObject"}, 3, id="assign-fails"),
        pytest.param({"failing_step": "QueryInformationJobObject"}, 4, id="read-back-fails"),
        pytest.param({"misreport": _without_flag(0x2)}, 4, id="time-flag-not-in-force"),
        pytest.param({"misreport": _without_flag(0x100)}, 4, id="memory-flag-not-in-force"),
        pytest.param({"misreport": _without_flag(0x8)}, 4, id="active-process-flag-not-in-force"),
        pytest.param({"misreport": _without_flag(0x2000)}, 4, id="kill-on-close-flag-not-in-force"),
        pytest.param({"misreport": _with_a_higher_memory_limit}, 4, id="memory-limit-higher"),
        pytest.param({"misreport": _with_no_memory_limit}, 4, id="memory-limit-zero"),
        pytest.param({"misreport": _with_a_higher_time_limit}, 4, id="time-limit-higher"),
        pytest.param({"misreport": _with_no_time_limit}, 4, id="time-limit-zero"),
        pytest.param({"misreport": _with_a_second_process_allowed}, 4, id="process-limit-higher"),
        pytest.param({"misreport": _with_no_process_limit}, 4, id="process-limit-zero"),
    ],
)
def test_a_job_object_step_that_fails_is_unavailable_with_nothing_read_or_parsed(monkeypatch, failure, steps_taken):
    log = []
    _use_fake_operating_system(monkeypatch, log, _WINDOWS, **failure)
    _forbid_parsing(monkeypatch)
    result = _run_before_ready([json.dumps(_limits())], _UnreadableInput())
    assert result == _UNAVAILABLE_BEFORE_ANY_CEILING
    assert log == _WINDOWS_STEPS[:steps_taken]


@pytest.mark.parametrize(
    ("failure", "steps_taken"),
    [
        pytest.param({"refuse": True}, 2, id="setrlimit-refused"),
        pytest.param({"ignore": True}, 5, id="soft-limit-left-unlimited"),
    ],
)
def test_an_rlimit_step_that_fails_is_unavailable_with_nothing_read_or_parsed(monkeypatch, failure, steps_taken):
    log = []
    _use_fake_operating_system(monkeypatch, log, _POSIX, **failure)
    _forbid_parsing(monkeypatch)
    result = _run_before_ready([json.dumps(_limits())], _UnreadableInput())
    assert result == _UNAVAILABLE_BEFORE_ANY_CEILING
    assert log == _POSIX_STEPS[:steps_taken]


def test_the_job_object_requests_every_ceiling_and_assigns_the_current_process():
    fake = _FakeKernel32([])
    pdf_text_worker.apply_job_object_ceiling(_limits(memory_limit_bytes=300 * _MEBIBYTE), fake)
    job, information_class, requested_bytes, size = fake.requested
    requested = pdf_text_worker._JobExtendedLimitInformation.from_buffer_copy(requested_bytes)
    assert (job, information_class) == (_FAKE_JOB, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION)
    assert size == ctypes.sizeof(requested)
    assert requested.BasicLimitInformation.LimitFlags == _JOB_LIMIT_FLAGS
    assert requested.BasicLimitInformation.ActiveProcessLimit == _JOB_ACTIVE_PROCESS_LIMIT
    assert requested.ProcessMemoryLimit == 300 * _MEBIBYTE
    assert requested.BasicLimitInformation.PerProcessUserTimeLimit == 31 * _WINDOWS_TIME_UNITS_PER_SECOND
    assert fake.assigned == (_FAKE_JOB, _FAKE_CURRENT_PROCESS)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"failing_step": "CreateJobObjectW"}, id="create-fails"),
        pytest.param({"failing_step": "QueryInformationJobObject"}, id="read-back-fails"),
        pytest.param({"misreport": _with_a_higher_memory_limit}, id="memory-limit-higher"),
        pytest.param({"misreport": _with_a_second_process_allowed}, id="process-limit-higher"),
    ],
)
def test_the_job_object_logic_fails_closed_on_every_platform(failure):
    with pytest.raises(WorkerRefusal) as refusal:
        pdf_text_worker.apply_job_object_ceiling(_limits(), _FakeKernel32([], **failure))
    assert refusal.value.token == RESULT_UNAVAILABLE


def test_kernel32_that_cannot_be_loaded_is_unavailable(monkeypatch):
    def no_library(*args, **kwargs):
        """Fail as loading a missing system library does."""
        raise OSError("kernel32 could not be loaded")

    monkeypatch.setattr(ctypes, "WinDLL", no_library, raising=False)
    with pytest.raises(WorkerRefusal) as refusal:
        pdf_text_worker._load_kernel32()
    assert refusal.value.token == RESULT_UNAVAILABLE


def test_a_platform_without_a_ceiling_mechanism_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "resource", None)
    with pytest.raises(WorkerRefusal) as refusal:
        _REAL_INSTALL_CEILING(_limits(), platform="emscripten")
    assert refusal.value.token == RESULT_UNAVAILABLE


# ── R2: pypdf's version, hooks and self-test are checked before parsing ─────


@pytest.mark.parametrize(
    ("installed", "minimum", "reported"),
    [
        pytest.param("6.14.1", "6.14.2", "6.14.1", id="below-the-minimum"),
        pytest.param("6.14", "6.14.2", "6.14", id="shorter-and-below"),
        pytest.param("5.99.99", "6.14.2", "5.99.99", id="older-major"),
        pytest.param(_INSTALLED_PYPDF, "99.0", _INSTALLED_PYPDF, id="minimum-above-the-installed-pypdf"),
        pytest.param("development", "6.14.2", PARSER_VERSION_UNKNOWN, id="unparsable"),
        pytest.param("6.14.2 beta", "6.14.2", PARSER_VERSION_UNKNOWN, id="not-a-header-token"),
        pytest.param(None, "6.14.2", PARSER_VERSION_UNKNOWN, id="missing"),
    ],
)
def test_a_pypdf_version_below_the_minimum_or_unknown_is_unavailable_before_reading(
    monkeypatch, installed, minimum, reported
):
    if installed is None:
        monkeypatch.delattr(pypdf, "__version__")
    else:
        monkeypatch.setattr(pypdf, "__version__", installed)
    unreadable = _forbid_reading_and_parsing_the_document(monkeypatch)
    arguments = [json.dumps(_limits(parser_minimum_version=minimum))]
    result = _run_before_ready(arguments, unreadable)
    assert result == WorkerResult(RESULT_UNAVAILABLE, _PLATFORM_CEILING, reported, "")


@pytest.mark.parametrize("installed", ["6.14.2", "6.14.2.post1", "6.15.0", "7.0"])
def test_a_pypdf_at_or_above_the_minimum_is_used_and_reported(monkeypatch, installed):
    monkeypatch.setattr(pypdf, "__version__", installed)
    result = _run_result(build_pdf([text_operations("recent enough")]))
    assert result == WorkerResult(RESULT_TEXT, _PLATFORM_CEILING, installed, "recent enough")


_REQUIRED_NAMES = (
    [("filters", name) for name in _PYPDF_FILTER_HOOKS]
    + [("errors", name) for name in PYPDF_ERROR_ATTRIBUTES]
    + [("generic", name) for name in PYPDF_STREAM_ATTRIBUTES]
    + [("pypdf", "PdfReader"), ("pypdf", "PageObject"), ("PageObject", "extract_xform_text")]
)


@pytest.mark.parametrize(("module_name", "missing"), _REQUIRED_NAMES)
def test_a_missing_pypdf_name_is_unavailable_and_changes_nothing(monkeypatch, module_name, missing):
    modules = {
        "filters": pypdf.filters,
        "errors": pypdf.errors,
        "generic": pypdf.generic,
        "pypdf": pypdf,
        "PageObject": pypdf.PageObject,
    }
    monkeypatch.delattr(modules[module_name], missing)
    attributes_before = _worker_patched_attributes()
    handlers_before = list(_PYPDF_LOGGER.handlers)
    channel = io.BytesIO()
    # Run directly, so whatever the run changed is still in place to inspect;
    # the fixture puts it all back afterwards.
    result = pdf_text_worker.run_worker([json.dumps(_limits())], _UnreadableInput(), channel)
    assert (result, channel.getvalue()) == (_UNAVAILABLE_AFTER_THE_CEILING, b"")
    assert _worker_patched_attributes() == attributes_before
    assert _PYPDF_LOGGER.handlers == handlers_before
    assert _worker_observer_tool_ids() == []


def test_pypdf_missing_entirely_is_unavailable_after_the_ceiling(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    result = _run_before_ready([json.dumps(_limits())], _UnreadableInput())
    assert result == WorkerResult(RESULT_UNAVAILABLE, _PLATFORM_CEILING, PARSER_VERSION_UNKNOWN, "")


def _decode_flate_ignoring_every_limit(data, decode_parms=None, **kwargs):
    """A Flate decoder from a pypdf that captured its limit at import time: it never stops."""
    return zlib.decompress(data)


def test_a_per_stream_limit_not_read_at_decode_time_is_unavailable_before_parsing(monkeypatch):
    monkeypatch.setattr(pypdf.filters.FlateDecode, "decode", staticmethod(_decode_flate_ignoring_every_limit))
    unreadable = _forbid_reading_and_parsing_the_document(monkeypatch)
    result = _run_before_ready([json.dumps(_limits())], unreadable)
    assert result == _UNAVAILABLE_AFTER_THE_CEILING


def test_streams_decoded_without_the_wrapped_decoder_are_unavailable_before_parsing(monkeypatch):
    decoder_captured_at_import = pypdf.filters.decode_stream_data

    def get_data_bypassing_the_hook(stream):
        """Decode with a decoder captured before the worker wrapped pypdf's, as a refactored pypdf might."""
        return decoder_captured_at_import(stream)

    monkeypatch.setattr(pypdf.generic.EncodedStreamObject, "get_data", get_data_bypassing_the_hook)
    unreadable = _forbid_reading_and_parsing_the_document(monkeypatch)
    result = _run_before_ready([json.dumps(_limits())], unreadable)
    assert result == _UNAVAILABLE_AFTER_THE_CEILING


def test_a_self_test_that_raises_is_unavailable_before_parsing(monkeypatch):
    def get_data_failing(stream):
        """Fail as a broken pypdf might."""
        raise KeyError("/Filter")

    monkeypatch.setattr(pypdf.generic.EncodedStreamObject, "get_data", get_data_failing)
    unreadable = _forbid_reading_and_parsing_the_document(monkeypatch)
    result = _run_before_ready([json.dumps(_limits())], unreadable)
    assert result == _UNAVAILABLE_AFTER_THE_CEILING


def test_after_the_self_test_the_real_limits_are_set_and_the_budget_starts_empty(monkeypatch):
    pdf = _four_pages_of(4000)
    expected = reference_text(pdf)
    limits_seen = _capture_stream_limits_during_extraction(monkeypatch)
    # 16,000 bytes of document streams fit a 16,000-byte budget exactly: the
    # self-test's decodes were not charged against the document.
    assert _run(pdf, max_decoded_bytes=16_000) == (RESULT_TEXT, expected)
    assert limits_seen == [dict.fromkeys(PYPDF_STREAM_LIMIT_ATTRIBUTES, 16 * _MEBIBYTE)] * 4


def test_after_its_self_tests_the_parser_is_bounded_observed_and_starts_clear():
    budgets = pdf_text_worker.prepare_parser(pypdf, _limits(), _INSTALLED_PYPDF)
    observer = budgets.observer
    assert _MONITORING.get_tool(observer.tool_id) == OBSERVER_TOOL_NAME
    assert _MONITORING.get_events(observer.tool_id) == _MONITORING.events.RAISE
    assert observer.exceeded is False
    assert budgets.any_exceeded() is False
    assert (budgets.decoded.used, budgets.decoded.limit, budgets.decoded.stream_limit) == (
        0,
        64 * _MEBIBYTE,
        16 * _MEBIBYTE,
    )
    assert pypdf.filters.JBIG2DEC_BINARY is None


# ── JBIG2: pypdf would start the jbig2dec program; the worker never lets it ─

# Any path will do: the spy refuses every start, and a stand-in that would
# run is written by the real-process test below.
_STAND_IN_JBIG2DEC = str(Path(sys.executable).resolve())


def _record_process_starts(monkeypatch) -> list:
    """Record every attempt to start a process through subprocess, and refuse each one."""
    attempts = []

    def refuse_to_start(*args, **kwargs):
        """Record the command, then fail as a refused process start would."""
        attempts.append(args[0] if args else kwargs.get("args"))
        raise OSError("this test starts no process")

    monkeypatch.setattr(subprocess, "run", refuse_to_start)
    monkeypatch.setattr(subprocess, "Popen", refuse_to_start)
    return attempts


def test_with_jbig2dec_installed_pypdf_itself_would_start_it_for_a_jbig2_page(monkeypatch):
    monkeypatch.setattr(pypdf.filters, "JBIG2DEC_BINARY", _STAND_IN_JBIG2DEC)
    attempts = _record_process_starts(monkeypatch)
    with pytest.raises(OSError):
        reference_text(build_pdf_with_jbig2_page())
    assert [command[0] for command in attempts] == [_STAND_IN_JBIG2DEC]


@pytest.mark.parametrize(
    ("pdf", "expected"),
    [
        pytest.param(build_pdf_with_jbig2_page(), (RESULT_UNREADABLE, ""), id="page-content-refused"),
        pytest.param(
            build_pdf_with_jbig2_form(text_operations("page text")),
            (RESULT_TEXT, _PAGE_THEN_SKIPPED_FORM),
            id="form-skipped-as-undecodable",
        ),
    ],
)
def test_a_jbig2_stream_starts_no_process_in_the_worker_and_is_refused_or_skipped(monkeypatch, pdf, expected):
    monkeypatch.setattr(pypdf.filters, "JBIG2DEC_BINARY", _STAND_IN_JBIG2DEC)
    attempts = _record_process_starts(monkeypatch)
    assert _run(pdf) == expected
    assert attempts == []


# ── The observer: failures raised anywhere, caught or not (owner point 1) ────


@contextlib.contextmanager
def _installed_observer():
    """Install the worker's observer in this process for the block, then release it."""
    observer = pdf_text_worker.install_failure_observer(pypdf.errors.LimitReachedError)
    try:
        yield observer
    finally:
        _release_worker_observers()


def _raise_and_catch(exception: BaseException) -> None:
    """Raise exception and swallow it where it was raised."""
    try:
        raise exception
    except BaseException:
        pass


class _NarrowerLimitError(pypdf.errors.LimitReachedError):
    """A subclass of pypdf's LimitReachedError, as a pypdf release might add."""


@pytest.mark.parametrize(
    ("raised", "marked"),
    [
        pytest.param(MemoryError(), True, id="memory-error"),
        pytest.param(pypdf.errors.LimitReachedError("limit"), True, id="limit-reached"),
        pytest.param(_NarrowerLimitError("narrower"), True, id="limit-reached-subclass"),
        pytest.param(NotImplementedError("filter"), False, id="not-implemented"),
        pytest.param(pypdf.errors.PdfReadError("bad"), False, id="pypdf-read-error"),
        pytest.param(KeyError("/Font"), False, id="key-error"),
        pytest.param(RecursionError(), False, id="recursion-error"),
        pytest.param(WorkerRefusal(RESULT_OVER_LIMITS), False, id="worker-refusal"),
    ],
)
def test_the_observer_marks_only_a_memory_or_limit_failure_raised_and_swallowed(raised, marked):
    with _installed_observer() as observer:
        _raise_and_catch(raised)
        seen = observer.exceeded
    assert seen is marked


def test_the_observer_marks_a_memory_error_the_interpreter_raises_itself():
    with _installed_observer() as observer:
        try:
            bytearray(sys.maxsize)
        except MemoryError:
            pass
        seen = observer.exceeded
    assert seen is True


def test_the_observer_stays_marked_after_harmless_exceptions_and_writes_nothing(capsys):
    with _installed_observer() as observer:
        _raise_and_catch(MemoryError())
        _raise_and_catch(ValueError("later, harmless"))
        seen = observer.exceeded
    assert seen is True
    assert capsys.readouterr() == ("", "")


def test_without_sys_monitoring_the_worker_is_unavailable_before_reading(monkeypatch):
    unreadable = _forbid_reading_and_parsing_the_document(monkeypatch)
    monkeypatch.delattr(sys, "monitoring")
    assert _run_before_ready([json.dumps(_limits())], unreadable) == _UNAVAILABLE_AFTER_THE_CEILING


def _claim_every_free_tool_id() -> list[int]:
    """Claim, for another tool, every sys.monitoring tool id no tool holds yet; return them."""
    claimed = []
    for tool_id in _MONITORING_TOOL_IDS:
        if _MONITORING.get_tool(tool_id) is None:
            _MONITORING.use_tool_id(tool_id, _OCCUPYING_TOOL_NAME)
            claimed.append(tool_id)
    return claimed


def test_with_every_monitoring_tool_id_taken_the_worker_is_unavailable_before_reading(monkeypatch):
    unreadable = _forbid_reading_and_parsing_the_document(monkeypatch)
    claimed = _claim_every_free_tool_id()
    try:
        result = _run_before_ready([json.dumps(_limits())], unreadable)
    finally:
        for tool_id in claimed:
            _MONITORING.free_tool_id(tool_id)
    assert claimed != []
    assert result == _UNAVAILABLE_AFTER_THE_CEILING


def _ignore_monitoring_call(*args, **kwargs) -> None:
    """A sys.monitoring call that does nothing, as a broken or intercepted interpreter might."""


def _events_never_switched_on(monkeypatch) -> None:
    """Make sys.monitoring.set_events do nothing, so the observer never receives an event."""
    monkeypatch.setattr(_MONITORING, "set_events", _ignore_monitoring_call)


def _callback_never_registered(monkeypatch) -> None:
    """Make sys.monitoring.register_callback do nothing, so events reach no callback."""
    monkeypatch.setattr(_MONITORING, "register_callback", _ignore_monitoring_call)


def _memory_error_self_test_raising_nothing(monkeypatch) -> None:
    """Make the observer self-test's deliberate MemoryError never happen, as if it went unseen."""
    monkeypatch.setattr(pdf_text_worker, "_raise_and_swallow_memory_error", _ignore_monitoring_call)


@pytest.mark.parametrize(
    "break_the_observer",
    [_events_never_switched_on, _callback_never_registered, _memory_error_self_test_raising_nothing],
)
def test_an_observer_that_does_not_see_its_self_test_failures_is_unavailable_before_reading(
    monkeypatch, break_the_observer
):
    break_the_observer(monkeypatch)
    unreadable = _forbid_reading_and_parsing_the_document(monkeypatch)
    assert _run_before_ready([json.dumps(_limits())], unreadable) == _UNAVAILABLE_AFTER_THE_CEILING


# ── Every site where pypdf 6.14.2 swallows a failure ────────────────────────

# One document per swallow_sites site, each reaching that site's try block.
_SITE_DOCUMENTS = {
    "line-break-check": build_pdf([text_operations("first line", "second line")]),
    "graphics-state-restore": build_pdf(
        [b"q " + text_operations("inside q") + b" Q " + text_operations("after Q")]
    ),
    "matrix-concatenation": build_pdf([b"1 0 0 1 0 0 cm " + text_operations("matrix text")]),
    "font-size": build_pdf([text_operations("font size text")]),
    "encoding-name": build_pdf([text_operations("encoding name text")]),
    "base-encoding": build_pdf_with_base_encoding(
        text_operations("base encoding text"), swallow_sites.BASE_ENCODING_NAME
    ),
    "glyph-name": build_pdf_with_amplifying_encoding(8, 3),
    # An even number of bytes, so pypdf's two-byte fallback decode succeeds.
    "string-decoding": build_pdf_with_unencoded_font(text_operations("even text 12")),
    "form-xobject": build_pdf_with_form(text_operations("page text"), text_operations("form text")),
    "dictionary-value": build_pdf_with_nested_page_dictionary(
        text_operations("dictionary text"), swallow_sites.DICTIONARY_MARKER
    ),
    "dictionary-value-limit": build_pdf_with_nested_page_dictionary(
        text_operations("dictionary text"), swallow_sites.DICTIONARY_MARKER
    ),
}


def _plant(monkeypatch, site: str) -> swallow_sites.PlantedFailure:
    """Plant the site's failure in this process's pypdf, undone when the test ends."""
    return swallow_sites.plant(site, functools.partial(monkeypatch.setattr, raising=False))


def _deafen_the_observer_once_ready(monkeypatch) -> None:
    """Make the worker switch its observer's events off straight after the ready line: a run without the observer."""
    real_announce = pdf_text_worker.announce_ready

    def announce_then_deafen(result_channel, ceiling, parser_version):
        """Announce as the worker does, then stop sys.monitoring delivering events to its observer."""
        real_announce(result_channel, ceiling, parser_version)
        for tool_id in _worker_observer_tool_ids():
            _MONITORING.set_events(tool_id, _MONITORING.events.NO_EVENTS)

    monkeypatch.setattr(pdf_text_worker, "announce_ready", announce_then_deafen)


def test_every_swallow_site_has_a_document_that_reaches_it():
    assert set(_SITE_DOCUMENTS) == set(swallow_sites.SITE_NAMES)


@pytest.mark.parametrize("site", swallow_sites.SITE_NAMES)
def test_a_failure_pypdf_swallows_at_any_site_refuses_the_document(monkeypatch, site):
    pdf = _SITE_DOCUMENTS[site]
    failure = _plant(monkeypatch, site)
    # pypdf alone swallows the failure and returns text as if nothing happened.
    swallowed = reference_text(pdf)
    fired_in_pypdf_alone = failure.raised
    with monkeypatch.context() as control:
        _deafen_the_observer_once_ready(control)
        without_the_observer = _run_result(pdf)
    fired_without_the_observer = failure.raised - fired_in_pypdf_alone
    assert fired_in_pypdf_alone > 0
    assert (without_the_observer.outcome, without_the_observer.text) == (RESULT_TEXT, swallowed)
    assert fired_without_the_observer > 0
    assert _run(pdf) == (RESULT_OVER_LIMITS, "")
    assert failure.raised > fired_in_pypdf_alone + fired_without_the_observer


def test_a_failure_swallowed_in_a_document_with_no_pages_still_refuses_it(monkeypatch):
    pdf = build_pdf_without_pages_marking_its_catalog(swallow_sites.DICTIONARY_MARKER)
    failure = _plant(monkeypatch, "dictionary-value")
    # No page follows the failure, so only the check after the last page can
    # see it: without the observer the worker would answer an empty text.
    assert reference_text(pdf) == ""
    assert failure.raised > 0
    with monkeypatch.context() as control:
        _deafen_the_observer_once_ready(control)
        assert _run(pdf) == (RESULT_TEXT, "")
    assert _run(pdf) == (RESULT_OVER_LIMITS, "")


def _report_nothing(message, *, source, **values):
    """A pypdf logger_warning that no longer logs, as a pypdf release might ship."""


def test_a_swallowed_failure_is_refused_with_pypdfs_reporting_silenced_and_logging_disabled(monkeypatch):
    monkeypatch.setattr(pypdf._page, "logger_warning", _report_nothing)
    monkeypatch.setattr(pypdf._utils, "logger_warning", _report_nothing)
    _plant(monkeypatch, "form-xobject")
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        outcome = _run(_SITE_DOCUMENTS["form-xobject"])
    finally:
        logging.disable(previous)
    assert outcome == (RESULT_OVER_LIMITS, "")


def _form_extraction_raising(raised: BaseException):
    """Return a PageObject.extract_xform_text that fails with raised, inside pypdf's own broad except."""

    def extract_xform_text_failing(page, *args, **kwargs):
        """Fail as extracting a hostile or broken form would."""
        raise raised

    return extract_xform_text_failing


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(MemoryError(), id="memory-error"),
        pytest.param(pypdf.errors.LimitReachedError("Maximum /ToUnicode size limit reached"), id="limit-reached"),
    ],
)
def test_a_memory_or_limit_failure_pypdf_swallows_in_a_form_refuses_the_document(monkeypatch, raised):
    pdf = build_pdf_with_form(text_operations("page text"), text_operations("form text"))
    monkeypatch.setattr(pypdf.PageObject, "extract_xform_text", _form_extraction_raising(raised))
    # pypdf logs the failure and would return the page without the form.
    assert reference_text(pdf) == _PAGE_THEN_SKIPPED_FORM
    assert _run(pdf) == (RESULT_OVER_LIMITS, "")


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(NotImplementedError("Unsupported filter"), id="unsupported-filter"),
        pytest.param(KeyError("/Subtype"), id="missing-key"),
        pytest.param(pypdf.errors.PdfReadError("damaged form"), id="read-error"),
    ],
)
def test_any_other_failure_pypdf_swallows_in_a_form_is_tolerated_as_today(monkeypatch, raised):
    pdf = build_pdf_with_form(text_operations("page text"), text_operations("form text"))
    monkeypatch.setattr(pypdf.PageObject, "extract_xform_text", _form_extraction_raising(raised))
    assert reference_text(pdf) == _PAGE_THEN_SKIPPED_FORM
    assert _run(pdf) == (RESULT_TEXT, _PAGE_THEN_SKIPPED_FORM)


def test_a_form_pypdf_cannot_decode_is_skipped_exactly_as_today():
    pdf = build_pdf_with_undecodable_form(text_operations("page text"))
    assert reference_text(pdf) == _PAGE_THEN_SKIPPED_FORM
    assert _run(pdf) == (RESULT_TEXT, _PAGE_THEN_SKIPPED_FORM)


def test_a_document_that_reads_cleanly_is_unaffected_by_warnings_pypdf_logs_without_a_failure(monkeypatch):
    original = pypdf.PageObject.extract_text

    def extract_after_a_harmless_warning(page, *args, **kwargs):
        """Log a warning with no exception through pypdf's own reporting, then extract."""
        pypdf._utils.logger_warning("Skipping broken line %(line)r", source="pypdf._cmap", line=b"<01>")
        return original(page, *args, **kwargs)

    _replace_page_extraction(monkeypatch, extract_after_a_harmless_warning)
    assert _run(build_pdf([text_operations("unaffected")])) == (RESULT_TEXT, "unaffected")


# ── The joined text is exactly today's formula ──────────────────────────────


@pytest.mark.parametrize(
    "pdf",
    [
        pytest.param(build_pdf([text_operations("Board approved ISMS policy")]), id="one-page"),
        pytest.param(
            build_pdf(
                [
                    text_operations("Access review", "completed 2026-Q1"),
                    None,
                    text_operations("Zürich café € 5 (draft)"),
                    text_operations(""),
                ]
            ),
            id="multi-page-empty-page-non-ascii",
        ),
        pytest.param(build_pdf([text_operations("uncompressed")], compress=False), id="no-filter"),
        pytest.param(build_pdf([None, None]), id="no-text-at-all"),
    ],
)
def test_joined_text_equals_todays_formula(pdf):
    expected = reference_text(pdf)
    assert _run(pdf) == (RESULT_TEXT, expected)


# ── Page cap ────────────────────────────────────────────────────────────────


def test_page_cap_refuses_before_any_page_text_is_extracted(monkeypatch):
    extraction_calls = _spy_on_page_extraction(monkeypatch)
    pdf = build_pdf([text_operations("one"), text_operations("two"), text_operations("three")])
    assert _run(pdf, max_pages=2) == (RESULT_TOO_MANY_PAGES, "")
    assert extraction_calls == []


def test_a_document_exactly_at_the_page_cap_is_read(monkeypatch):
    extraction_calls = _spy_on_page_extraction(monkeypatch)
    pdf = build_pdf([text_operations("one"), text_operations("two")])
    assert _run(pdf, max_pages=2) == (RESULT_TEXT, "one\ntwo")
    assert len(extraction_calls) == 2


# ── Text budget ─────────────────────────────────────────────────────────────


def test_text_budget_stops_extraction_before_a_later_page(monkeypatch):
    extraction_calls = _spy_on_page_extraction(monkeypatch)
    pdf = build_pdf([text_operations("a" * 100), text_operations("b" * 100), text_operations("c" * 100)])
    assert _run(pdf, max_text_characters=150) == (RESULT_OVER_LIMITS, "")
    assert len(extraction_calls) == 2


def test_text_budget_stops_mid_page_when_pypdf_reports_a_fragment(monkeypatch):
    fragments_seen = []

    def extract_reporting_fragments(page, *args, visitor_text=None, **kwargs):
        """Report three fragments as pypdf would, recording each one that is reached."""
        for fragment in ("x" * 60, "y" * 60, "z" * 60):
            fragments_seen.append(fragment[0])
            visitor_text(fragment, None, None, None, None)
        return "x" * 60 + "y" * 60 + "z" * 60

    _replace_page_extraction(monkeypatch, extract_reporting_fragments)
    assert _run(build_pdf([text_operations("ignored")]), max_text_characters=100) == (
        RESULT_OVER_LIMITS,
        "",
    )
    assert fragments_seen == ["x", "y"]


def test_returned_page_text_is_charged_even_when_no_fragment_was_reported(monkeypatch):
    def extract_without_fragments(page, *args, **kwargs):
        """Return 75 characters per page without ever calling the visitor."""
        return "t" * 75

    _replace_page_extraction(monkeypatch, extract_without_fragments)
    two_pages = build_pdf([text_operations("p1"), text_operations("p2")])
    # 75 + one separator + 75 = 151 characters of joined text.
    assert _run(two_pages, max_text_characters=150) == (RESULT_OVER_LIMITS, "")
    assert _run(two_pages, max_text_characters=151) == (RESULT_TEXT, "t" * 75 + "\n" + "t" * 75)


def test_a_fragment_refusal_pypdf_swallows_still_refuses_the_document(monkeypatch):
    def extract_swallowing_refusal(page, *args, visitor_text=None, **kwargs):
        """Call the visitor inside a broad except, as some pypdf code paths do."""
        try:
            visitor_text("w" * 500, None, None, None, None)
        except Exception:
            pass
        return "short"

    _replace_page_extraction(monkeypatch, extract_swallowing_refusal)
    assert _run(build_pdf([text_operations("p")]), max_text_characters=100) == (RESULT_OVER_LIMITS, "")


_FORM_LINE = "F" * 1000
# The same 1,000 characters drawn on the page, from a form, and from a form
# inside a form, and text at every depth at once: pypdf reports a form's text
# in fragments and then whole, at each depth, and each character must still
# be charged once.
_TEXT_AT_EVERY_DEPTH = [
    pytest.param(build_pdf([text_operations(_FORM_LINE)]), id="page-text"),
    pytest.param(build_pdf_with_form(b"", text_operations(_FORM_LINE)), id="form"),
    pytest.param(build_pdf_with_nested_forms(b"", b"", text_operations(_FORM_LINE)), id="form-in-a-form"),
    pytest.param(
        build_pdf_with_nested_forms(text_operations("page"), text_operations("outer"), text_operations(_FORM_LINE)),
        id="text-at-every-depth",
    ),
]


@pytest.mark.parametrize("pdf", _TEXT_AT_EVERY_DEPTH)
def test_text_at_the_cap_is_read_at_any_form_depth_and_one_character_more_is_refused(pdf):
    expected = reference_text(pdf)
    assert _FORM_LINE in expected
    assert _run(pdf, max_text_characters=len(expected)) == (RESULT_TEXT, expected)
    assert _run(pdf, max_text_characters=len(expected) - 1) == (RESULT_OVER_LIMITS, "")


@pytest.mark.parametrize("pdf", _TEXT_AT_EVERY_DEPTH)
def test_every_character_is_charged_to_the_fragment_budget_exactly_once(pdf):
    expected = reference_text(pdf)
    limits = _limits()
    budgets = pdf_text_worker.prepare_parser(pypdf, limits, _INSTALLED_PYPDF)
    assert pdf_text_worker.extract_document(pypdf, pdf, limits, budgets) == expected
    assert budgets.fragments.used == len(expected)


# ── Decoded-bytes budgets ───────────────────────────────────────────────────


def _four_pages_of(decoded_size: int) -> bytes:
    """Return four compressed pages whose content streams each decode to decoded_size bytes."""
    return build_pdf([padded(text_operations(f"page {number}"), decoded_size) for number in range(4)])


def test_cumulative_decoded_budget_trips_when_every_stream_is_under_the_per_stream_limit(monkeypatch):
    decoded = _spy_on_stream_decoding(monkeypatch)
    pdf = _four_pages_of(4000)
    assert _run(pdf, max_stream_output_bytes=8192, max_decoded_bytes=10_000) == (RESULT_OVER_LIMITS, "")
    # Charged as each stream finishes: the third passes 10,000 and the fourth never decodes.
    assert decoded == _SELF_TEST_DECODES + [4000, 4000, 4000]


def test_the_same_document_reads_when_the_cumulative_budget_allows_it(monkeypatch):
    pdf = _four_pages_of(4000)
    expected = reference_text(pdf)
    decoded = _spy_on_stream_decoding(monkeypatch)
    token, text = _run(pdf, max_stream_output_bytes=8192, max_decoded_bytes=16_000)
    assert (token, text) == (RESULT_TEXT, expected)
    assert decoded == _SELF_TEST_DECODES + [4000, 4000, 4000, 4000]


def test_per_stream_limit_stops_decompression_with_pypdfs_limit_error(monkeypatch):
    decoded = _spy_on_stream_decoding(monkeypatch)
    pdf = build_pdf([padded(text_operations("too long"), 8000)])
    assert _run(pdf, max_stream_output_bytes=4096) == (RESULT_OVER_LIMITS, "")
    assert decoded == _SELF_TEST_DECODES + [pypdf.errors.LimitReachedError]


def test_a_per_stream_limit_pypdf_swallows_inside_a_form_still_refuses_the_document():
    pdf = build_pdf_with_form(text_operations("page text"), padded(text_operations("form text"), 8000))
    assert reference_text(pdf) == "page text\nform text"
    # pypdf logs the form's LimitReachedError and returns "page text\n"; the
    # worker must refuse rather than store a silently truncated document.
    assert _run(pdf, max_stream_output_bytes=4096) == (RESULT_OVER_LIMITS, "")


def test_per_stream_limit_is_written_into_every_pypdf_limit_global(monkeypatch):
    limits_seen = _capture_stream_limits_during_extraction(monkeypatch)
    pdf = build_pdf([text_operations("limits")])
    assert _run(pdf, max_stream_output_bytes=12_345)[0] == RESULT_TEXT
    assert limits_seen == [dict.fromkeys(PYPDF_STREAM_LIMIT_ATTRIBUTES, 12_345)]


_ASCII85_STREAM_LIMIT = 64 * 1024


def _flate_then_ascii85_page(decoded_size: int) -> bytes:
    """Return a page whose one content stream, Flate then ASCII85, decodes to decoded_size bytes, mostly zeros."""
    content = zero_padded(text_operations("ascii85 text"), decoded_size)
    return build_pdf_with_encoded_page(flate_then_ascii85(content), FLATE_THEN_ASCII85)


@pytest.mark.parametrize(
    ("decoded_size", "expected_outcome"),
    [
        pytest.param(_ASCII85_STREAM_LIMIT - 1, RESULT_TEXT, id="one-byte-under"),
        pytest.param(_ASCII85_STREAM_LIMIT, RESULT_TEXT, id="exactly-at"),
        pytest.param(_ASCII85_STREAM_LIMIT + 1, RESULT_OVER_LIMITS, id="one-byte-over"),
    ],
)
def test_an_ascii85_stream_is_held_to_the_per_stream_limit_after_pypdf_decodes_it(
    monkeypatch, decoded_size, expected_outcome
):
    pdf = _flate_then_ascii85_page(decoded_size)
    expected_text = reference_text(pdf) if expected_outcome == RESULT_TEXT else ""
    decoded = _spy_on_stream_decoding(monkeypatch)
    assert _run(pdf, max_stream_output_bytes=_ASCII85_STREAM_LIMIT) == (expected_outcome, expected_text)
    # pypdf decoded the whole stream without stopping (its Flate stage is a
    # quarter of the size): only the worker's after-decode check can refuse it.
    assert decoded == _SELF_TEST_DECODES + [decoded_size]


# ── Encryption and unreadable input ─────────────────────────────────────────


def test_pdf_with_a_user_password_is_encrypted():
    pdf = build_pdf([text_operations("confidential board minutes")], user_password="open-sesame")
    assert _run(pdf) == (RESULT_ENCRYPTED, "")


def test_pdf_encrypted_with_an_empty_user_password_still_extracts():
    pdf = build_pdf([text_operations("readable without a password")], user_password="")
    assert _run(pdf) == (RESULT_TEXT, "readable without a password")


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"%PDF-1.4 this is not a real pdf", id="junk"),
        pytest.param(b"", id="empty"),
        pytest.param(b"\x89PNG\r\n\x1a\n", id="png-bytes"),
    ],
)
def test_malformed_bytes_are_unreadable(content):
    assert _run(content) == (RESULT_UNREADABLE, "")


@pytest.mark.parametrize(
    ("raised", "token"),
    [
        pytest.param(MemoryError(), RESULT_OVER_LIMITS, id="memory-error"),
        pytest.param(pypdf.errors.LimitReachedError("limit"), RESULT_OVER_LIMITS, id="limit-reached"),
        pytest.param(pypdf.errors.WrongPasswordError("wrong"), RESULT_ENCRYPTED, id="wrong-password"),
        pytest.param(RecursionError(), RESULT_UNREADABLE, id="recursion-error"),
        pytest.param(pypdf.errors.DependencyError("missing"), RESULT_UNREADABLE, id="dependency-error"),
        pytest.param(KeyError("/Font"), RESULT_UNREADABLE, id="other-exception"),
    ],
)
def test_exceptions_while_extracting_map_to_their_tokens(monkeypatch, raised, token):
    def extract_raising(page, *args, **kwargs):
        """Raise the parametrised exception from inside page extraction."""
        raise raised

    _replace_page_extraction(monkeypatch, extract_raising)
    assert _run(build_pdf([text_operations("p")])) == (token, "")


# ── The ready line, the result line and the channel ────────────────────────


def test_main_writes_the_ready_line_then_one_result_and_keeps_anything_pypdf_prints_out_of_them(monkeypatch):
    pdf = build_pdf([text_operations("clean result")])
    original = pypdf.PageObject.extract_text

    def extract_and_print(page, *args, **kwargs):
        """Print to stdout as a noisy library might, then extract."""
        print("pypdf noise")
        return original(page, *args, **kwargs)

    _replace_page_extraction(monkeypatch, extract_and_print)
    result, diagnostics = io.BytesIO(), io.BytesIO()
    # Held here: main() drops sys.stdout, and a collected wrapper closes its buffer.
    stdout = io.TextIOWrapper(result, write_through=True)
    stderr = io.TextIOWrapper(diagnostics, write_through=True)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(pdf)))
    monkeypatch.setattr(sys, "argv", ["pdf_text_worker.py", json.dumps(_limits())])
    assert pdf_text_worker.main() == 0
    expected_header = f"text {_PLATFORM_CEILING} {_INSTALLED_PYPDF}\n".encode()
    assert result.getvalue() == _ready_line() + expected_header + b"clean result"
    assert b"pypdf noise" in diagnostics.getvalue()


def test_text_round_trips_through_the_result_encoding_including_lone_surrogates():
    text = "Zürich € \U0001f512 lone \ud800 surrogate"
    channel = io.BytesIO()
    pdf_text_worker.write_result(channel, WorkerResult(RESULT_TEXT, _PLATFORM_CEILING, "6.14.2", text))
    head, _, payload = channel.getvalue().partition(b"\n")
    assert head == f"text {_PLATFORM_CEILING} 6.14.2".encode()
    assert payload.decode("utf-8", "surrogatepass") == text


def test_refusal_results_carry_only_the_header():
    channel = io.BytesIO()
    result = WorkerResult(RESULT_ENCRYPTED, CEILING_POSIX_RLIMIT, "6.14.2", "must not be written")
    pdf_text_worker.write_result(channel, result)
    assert channel.getvalue() == b"encrypted posix-rlimit 6.14.2\n"


def test_every_header_the_worker_writes_parses_back_to_the_same_fields():
    for outcome in pdf_text_worker.RESULT_TOKENS:
        for ceiling in (CEILING_POSIX_RLIMIT, CEILING_WINDOWS_JOB_OBJECT, CEILING_NONE):
            channel = io.BytesIO()
            pdf_text_worker.write_result(channel, WorkerResult(outcome, ceiling, PARSER_VERSION_UNKNOWN, ""))
            head = channel.getvalue().partition(b"\n")[0]
            assert pdf_text_worker.parse_result_header(head) == ResultHeader(outcome, ceiling, "none")
            assert pdf_text_worker.parse_ready_line(head) is None


def test_the_ready_line_parses_back_as_a_ready_line_and_never_as_a_result():
    channel = io.BytesIO()
    pdf_text_worker.announce_ready(channel, CEILING_WINDOWS_JOB_OBJECT, "6.14.2")
    assert channel.getvalue() == b"ready windows-job-object 6.14.2\n"
    head = channel.getvalue().partition(b"\n")[0]
    assert pdf_text_worker.parse_ready_line(head) == ResultHeader("ready", CEILING_WINDOWS_JOB_OBJECT, "6.14.2")
    assert pdf_text_worker.parse_result_header(head) is None
    assert "ready" not in pdf_text_worker.RESULT_TOKENS


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(b"text windows-job-object", id="two-fields"),
        pytest.param(b"text windows-job-object 6.14.2 extra", id="four-fields"),
        pytest.param(b"text  windows-job-object 6.14.2", id="empty-field"),
        pytest.param(b"maybe windows-job-object 6.14.2", id="unknown-outcome"),
        pytest.param(b"text windows-job-object 6.14.2\r", id="carriage-return"),
        pytest.param("text windows-job-object 6.14.2é".encode(), id="not-ascii"),
        pytest.param(b"text windows\x1b-job-object 6.14.2", id="control-character"),
        pytest.param(b"", id="empty"),
    ],
)
def test_a_malformed_header_line_does_not_parse(line):
    assert pdf_text_worker.parse_result_header(line) is None
    assert pdf_text_worker.parse_ready_line(line.replace(b"text", b"ready", 1)) is None


@pytest.mark.parametrize(
    ("found", "minimum", "expected"),
    [
        ("6.14.2", "6.14.2", True),
        ("6.14.10", "6.14.2", True),
        ("6.15", "6.14.2", True),
        ("10.0.0", "6.14.2", True),
        ("6.14.2.post1", "6.14.2", True),
        ("6.14.1", "6.14.2", False),
        ("6.9.99", "6.14.2", False),
        ("6.14", "6.14.2", False),
        ("none", "6.14.2", False),
        ("6.14.2", "unknown", False),
        ("", "6.14.2", False),
    ],
)
def test_versions_compare_by_their_leading_numbers(found, minimum, expected):
    assert pdf_text_worker.version_at_least(found, minimum) is expected


# ── The worker imports nothing that could read credentials (R4) ─────────────


def _imported_modules(nodes) -> set[str]:
    """Return the top-level module names imported anywhere among nodes."""
    imported = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    return imported


def test_worker_module_imports_only_the_standard_library_and_pypdf_and_pypdf_only_inside_functions():
    tree = ast.parse(_WORKER_PATH.read_text(encoding="utf-8"))
    everywhere = _imported_modules(ast.walk(tree))
    at_module_level = _imported_modules(tree.body)
    assert everywhere - {"pypdf"} <= set(sys.stdlib_module_names)
    assert everywhere.isdisjoint({"config", "src", "dotenv", "tests"})
    assert "pypdf" in everywhere
    assert "pypdf" not in at_module_level
    assert at_module_level - {"__future__"} <= set(sys.stdlib_module_names)


def test_worker_module_parses_as_python_3_10_so_an_older_interpreter_answers_unavailable_instead_of_crashing():
    ast.parse(_WORKER_PATH.read_text(encoding="utf-8"), feature_version=(3, 10))


# ── OS ceilings: the POSIX rule, with a fake resource module ────────────────


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        pytest.param(
            _ALL_UNLIMITED,
            {"address-space": (512 * _MEBIBYTE, 512 * _MEBIBYTE), "cpu": (31, 31)},
            id="unlimited-is-lowered",
        ),
        pytest.param(
            {"address-space": (_MEBIBYTE * 900, _MEBIBYTE * 1000), "cpu": (60, 120)},
            {"address-space": (512 * _MEBIBYTE, 512 * _MEBIBYTE), "cpu": (31, 31)},
            id="higher-limits-are-lowered",
        ),
        pytest.param(
            {"address-space": (100 * _MEBIBYTE, 200 * _MEBIBYTE), "cpu": (_UNLIMITED, 10)},
            {"address-space": (100 * _MEBIBYTE, 200 * _MEBIBYTE), "cpu": (10, 10)},
            id="lower-hard-limits-are-never-raised",
        ),
        pytest.param(
            {"address-space": (100 * _MEBIBYTE, _UNLIMITED), "cpu": (5, _UNLIMITED)},
            {"address-space": (100 * _MEBIBYTE, 512 * _MEBIBYTE), "cpu": (5, 31)},
            id="lower-soft-limits-are-kept",
        ),
    ],
)
def test_ceilings_lower_both_limits_and_never_raise_an_existing_lower_one(current, expected):
    fake = _FakeResource(current)
    pdf_text_worker.apply_rlimit_ceiling(_limits(), fake)
    assert fake.current == expected


@pytest.mark.parametrize(
    "fake",
    [
        pytest.param(_FakeResource(_ALL_UNLIMITED, refuse=True), id="setrlimit-refused"),
        pytest.param(_FakeResource(_ALL_UNLIMITED, ignore=True), id="soft-limit-left-unlimited"),
        pytest.param(
            _FakeResource({"address-space": (900 * _MEBIBYTE, 900 * _MEBIBYTE), "cpu": (31, 31)}, ignore=True),
            id="soft-limit-left-higher",
        ),
    ],
)
def test_an_rlimit_ceiling_that_is_not_in_force_is_unavailable(fake):
    with pytest.raises(WorkerRefusal) as refusal:
        pdf_text_worker.apply_rlimit_ceiling(_limits(), fake)
    assert refusal.value.token == RESULT_UNAVAILABLE


def test_a_resource_module_without_the_address_space_limit_is_unavailable():
    class _NoAddressSpace:
        """A resource module from a platform that lacks RLIMIT_AS."""

        RLIM_INFINITY = -1
        RLIMIT_CPU = "cpu"

    with pytest.raises(WorkerRefusal) as refusal:
        pdf_text_worker.apply_rlimit_ceiling(_limits(), _NoAddressSpace())
    assert refusal.value.token == RESULT_UNAVAILABLE


def test_a_missing_resource_module_off_windows_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "resource", None)
    with pytest.raises(WorkerRefusal) as refusal:
        pdf_text_worker._import_resource_module()
    assert refusal.value.token == RESULT_UNAVAILABLE


# ── OS ceilings: real child processes ───────────────────────────────────────

_PROBE_CEILING_BYTES = 256 * _MEBIBYTE


def _probe_ceilings(*extra: str) -> dict:
    """Run the ceilings probe in a child process under a 256 MiB ceiling and return its report."""
    limits = json.dumps(_limits(memory_limit_bytes=_PROBE_CEILING_BYTES))
    completed = subprocess.run(
        [sys.executable, str(_PROBE_PATH), "ceilings", str(_WORKER_PATH), limits, *extra],
        capture_output=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=True,
    )
    return json.loads(completed.stdout)


def test_a_real_process_under_the_worker_ceiling_cannot_allocate_past_it():
    report = _probe_ceilings()
    assert report["ceiling"] == _PLATFORM_CEILING
    assert report["past_the_ceiling"] == "MemoryError"
    assert report["small"] == "allocated"


def _expected_ceiling(current: int, wanted: int, unlimited: int) -> int:
    """Return the hard limit the worker's rule should leave, given this process's own."""
    if current == unlimited:
        return wanted
    return min(current, wanted)


if _ON_WINDOWS:

    def test_the_real_job_object_is_read_back_from_outside_the_worker_with_every_limit():
        report = _probe_ceilings()
        assert report["queried"] is True
        assert (report["limit_flags"] & _JOB_LIMIT_FLAGS) == _JOB_LIMIT_FLAGS
        assert report["process_memory_limit"] == _PROBE_CEILING_BYTES
        assert report["per_process_user_time_limit"] == 31 * _WINDOWS_TIME_UNITS_PER_SECOND

    def test_under_the_real_job_object_the_worker_cannot_start_a_child_process():
        limits = json.dumps(_limits(memory_limit_bytes=_PROBE_CEILING_BYTES))
        completed = subprocess.run(
            [sys.executable, "-P", str(_REAL_WORKER_PROBE_PATH), "job-children", str(_WORKER_PATH), limits],
            capture_output=True,
            timeout=_CHILD_TIMEOUT_SECONDS,
            check=True,
        )
        report = json.loads(completed.stdout)
        assert report["before_the_ceiling"] == {"outcome": "started", "returncode": 0}
        assert report["ceiling"] == CEILING_WINDOWS_JOB_OBJECT
        assert (report["limit_flags"] & _JOB_LIMIT_FLAGS) == _JOB_LIMIT_FLAGS
        assert report["active_process_limit"] == _JOB_ACTIVE_PROCESS_LIMIT
        assert report["under_the_ceiling"]["outcome"] == "refused"
        assert report["under_the_ceiling"]["winerror"] == _WINDOWS_NOT_ENOUGH_QUOTA

else:

    def test_real_rlimits_are_applied_to_the_worker_process():
        import resource

        report = _probe_ceilings()
        unlimited = report["unlimited"]
        memory_hard = _expected_ceiling(resource.getrlimit(resource.RLIMIT_AS)[1], _PROBE_CEILING_BYTES, unlimited)
        cpu_hard = _expected_ceiling(resource.getrlimit(resource.RLIMIT_CPU)[1], 31, unlimited)
        assert report["address_space"][1] == memory_hard
        assert report["cpu"][1] == cpu_hard
        assert report["address_space"][0] <= memory_hard
        assert report["cpu"][0] <= cpu_hard

    def test_real_ceilings_never_raise_an_existing_lower_hard_limit():
        lowered = 192 * _MEBIBYTE
        report = _probe_ceilings(str(lowered))
        assert report["address_space"] == [lowered, lowered]


def _real_worker_run(pdf: bytes, environment: dict | None = None, **overrides) -> subprocess.CompletedProcess:
    """Run the worker file in a real child process under generous limits plus overrides; it must exit 0."""
    completed = subprocess.run(
        [sys.executable, str(_WORKER_PATH), json.dumps(_limits(**overrides))],
        input=pdf,
        capture_output=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        env=environment,
    )
    assert completed.returncode == 0
    return completed


def _run_real_worker(pdf: bytes, **overrides) -> bytes:
    """Run the worker file in a real child process and return its stdout."""
    return _real_worker_run(pdf, **overrides).stdout


def test_the_real_worker_reports_its_platform_ceiling_and_pypdf_version():
    output = _run_real_worker(build_pdf([text_operations("verified")]))
    assert output == _ready_line() + f"text {_PLATFORM_CEILING} {_INSTALLED_PYPDF}\n".encode() + b"verified"


def test_the_real_worker_answers_unavailable_without_a_ready_line_when_pypdf_is_older_than_the_minimum():
    output = _run_real_worker(build_pdf([text_operations("too new a floor")]), parser_minimum_version="99.0")
    assert output == f"unavailable {_PLATFORM_CEILING} {_INSTALLED_PYPDF}\n".encode()


def test_the_real_worker_without_sys_monitoring_answers_unavailable_without_a_ready_line():
    completed = subprocess.run(
        [
            sys.executable,
            "-P",
            str(_REAL_WORKER_PROBE_PATH),
            "without-monitoring",
            str(_WORKER_PATH),
            json.dumps(_limits()),
        ],
        input=build_pdf([text_operations("never parsed")]),
        capture_output=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
    )
    assert completed.returncode == 0
    assert completed.stdout == f"unavailable {_PLATFORM_CEILING} {_INSTALLED_PYPDF}\n".encode()


# What a pypdf that cannot run on its interpreter raises as it is imported
# (seen with pypdf 6.14.2 on Python 3.8: typing has no _SpecialGenericAlias).
_INCOMPATIBLE_PYPDF = 'raise AttributeError("module \'typing\' has no attribute \'_SpecialGenericAlias\'")\n'


def test_the_real_worker_answers_unavailable_when_pypdf_fails_to_import_on_its_interpreter(tmp_path):
    package = tmp_path / "pypdf"
    package.mkdir()
    (package / "__init__.py").write_text(_INCOMPATIBLE_PYPDF, encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(tmp_path)
    completed = _real_worker_run(build_pdf([text_operations("never parsed")]), environment=environment)
    assert completed.stdout == f"unavailable {_PLATFORM_CEILING} {PARSER_VERSION_UNKNOWN}\n".encode()


def test_real_memory_ceiling_turns_an_oversized_allocation_into_over_limits():
    bomb = build_pdf_with_deflated_page(
        deflated_text_with_padding(text_operations("bomb"), 384 * _MEBIBYTE)
    )
    roomy = {"max_stream_output_bytes": 1024 * _MEBIBYTE, "max_decoded_bytes": 2048 * _MEBIBYTE}
    small_ceiling = 256 * _MEBIBYTE
    header = f" {_PLATFORM_CEILING} {_INSTALLED_PYPDF}\n".encode()
    assert _run_real_worker(bomb, memory_limit_bytes=small_ceiling, **roomy) == (
        _ready_line() + b"over-limits" + header
    )
    ordinary = build_pdf([text_operations("fits under the ceiling")])
    assert _run_real_worker(ordinary, memory_limit_bytes=small_ceiling, **roomy) == (
        _ready_line() + b"text" + header + b"fits under the ceiling"
    )


def test_the_real_worker_refuses_a_form_amplifier_pypdf_would_have_skipped_and_writes_nothing_else():
    pdf = build_pdf_with_amplifying_form(4000, 400_000)
    # In this process pypdf is unbounded; at a harmless size the form's text
    # follows the page's, which is what a skipped form would lose.
    assert reference_text(build_pdf_with_amplifying_form(8, 3)).startswith(AMPLIFIED_FORM_PAGE_TEXT + "\n/AAAAAAAA")
    completed = _real_worker_run(pdf)
    assert completed.stdout == _ready_line() + f"over-limits {_PLATFORM_CEILING} {_INSTALLED_PYPDF}\n".encode()
    assert completed.stderr == b""


def test_the_real_worker_skips_a_form_pypdf_cannot_decode_as_today_and_writes_nothing_else():
    pdf = build_pdf_with_undecodable_form(text_operations("page text"))
    assert reference_text(pdf) == _PAGE_THEN_SKIPPED_FORM
    completed = _real_worker_run(pdf)
    expected_header = f"text {_PLATFORM_CEILING} {_INSTALLED_PYPDF}\n".encode()
    assert completed.stdout == _ready_line() + expected_header + _PAGE_THEN_SKIPPED_FORM.encode()
    # Without the worker's discarding handler, pypdf's "Impossible to decode
    # XFormObject" warning would reach stderr through logging's last-resort
    # handler; the worker takes the record and writes nothing.
    assert completed.stderr == b""


# A stand-in for jbig2dec that leaves a marker beside itself whenever it runs.
_STAND_IN_MARKER = "jbig2dec-ran.txt"
_WINDOWS_STAND_IN = ("jbig2dec.bat", f'@echo off\r\necho ran> "%~dp0{_STAND_IN_MARKER}"\r\n')
_POSIX_STAND_IN = ("jbig2dec", f'#!/bin/sh\necho ran > "$(dirname "$0")/{_STAND_IN_MARKER}"\n')
_STAND_IN_PERMISSIONS = 0o755


def _write_jbig2dec_stand_in(directory: Path) -> Path:
    """Write an executable jbig2dec stand-in into directory and return its path."""
    name, script = _WINDOWS_STAND_IN if _ON_WINDOWS else _POSIX_STAND_IN
    stand_in = directory / name
    stand_in.write_text(script, encoding="ascii", newline="")
    stand_in.chmod(_STAND_IN_PERMISSIONS)
    return stand_in


def test_the_real_worker_with_jbig2dec_on_its_path_starts_no_program_for_a_jbig2_stream(tmp_path):
    stand_in = _write_jbig2dec_stand_in(tmp_path)
    marker = tmp_path / _STAND_IN_MARKER
    environment = dict(os.environ)
    environment["PATH"] = str(tmp_path) + os.pathsep + environment.get("PATH", "")
    # Controls: the stand-in runs when started, and pypdf's own lookup finds it.
    subprocess.run([str(stand_in)], check=True, timeout=_CHILD_TIMEOUT_SECONDS, env=environment)
    assert marker.exists()
    marker.unlink()
    found = subprocess.run(
        [sys.executable, "-c", "import shutil; print(shutil.which('jbig2dec'))"],
        capture_output=True,
        text=True,
        check=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        env=environment,
    )
    assert Path(found.stdout.strip()).resolve() == stand_in.resolve()
    completed = _real_worker_run(build_pdf_with_jbig2_page(), environment=environment)
    assert completed.stdout == _ready_line() + f"unreadable {_PLATFORM_CEILING} {_INSTALLED_PYPDF}\n".encode()
    assert not marker.exists()


def _use_the_planted_real_worker(monkeypatch, site: str, observer_mode: str) -> None:
    """Make the API's extract_pdf_text start the real worker, under its real limits, with site's failure planted."""

    def planted_worker_command(limits=None):
        """Return the probe's argv, carrying the API's own worker limits."""
        return [
            sys.executable,
            "-P",
            str(_REAL_WORKER_PROBE_PATH),
            "planted",
            site,
            observer_mode,
            str(_WORKER_PATH),
            json.dumps(pdf_text_extraction.worker_limits()),
        ]

    monkeypatch.setattr(pdf_text_extraction, "worker_command", planted_worker_command)


def test_through_the_api_a_real_worker_refuses_a_failure_pypdf_swallows_with_the_over_limits_422(monkeypatch, caplog):
    site = "matrix-concatenation"
    pdf = _SITE_DOCUMENTS[site]
    caplog.set_level(logging.INFO, logger=_EXTRACTION_LOGGER_NAME)
    with monkeypatch.context() as control:
        _use_the_planted_real_worker(control, site, "deafened")
        assert pdf_text_extraction.extract_pdf_text(pdf) == "matrix text"
    _use_the_planted_real_worker(monkeypatch, site, "live")
    with pytest.raises(EvidenceExtractionError) as refusal:
        pdf_text_extraction.extract_pdf_text(pdf)
    assert str(refusal.value) == pdf_text_extraction.PDF_OVER_LIMITS_DETAIL
    assert "category=over-limits" in caplog.text
