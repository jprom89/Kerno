"""
pdf_text_worker.py

What:  The child process that reads an uploaded PDF's text with pypdf. Before
       it reads the upload or imports pypdf it puts itself under an
       operating-system memory and CPU ceiling and confirms the ceiling is in
       force (on Windows a Job Object that also holds the worker as its only
       process, so it can start no other; RLIMIT_AS and RLIMIT_CPU on POSIX).
       Before it reads the upload it checks pypdf's version and every name it
       relies on, stops pypdf from ever starting an external decoder
       program, and registers a process-wide observer (sys.monitoring) that
       marks the document over the limits whenever a MemoryError or pypdf
       LimitReachedError is raised, whether or not anything catches it. Two
       self-tests prove that pypdf's per-stream limit is read at decode time,
       that every decode passes through the worker's decoded-byte budget, and
       that the observer sees a limit failure and a swallowed MemoryError.
       Only then does it announce that it is ready, read the document under
       page, per-stream, decoded-byte and text budgets (a Form XObject's text
       charged once, however deeply nested), and write back exactly one
       result.
Why:   pypdf parses untrusted documents and does not bound every
       amplification itself: a font's ToUnicode map can turn one text
       fragment into gigabytes, which only an OS ceiling stops, and pypdf
       catches and ignores exceptions at many sites (a Form XObject's
       handler, matrix and font-size parsing, glyph lookups, dictionary
       reads), so a memory or limit failure there would otherwise return a
       silently truncated document as a success. If the ceiling or a parser
       protection cannot be put in place and confirmed, the worker answers
       RESULT_UNAVAILABLE and parses nothing, never a silent skip
       (PILOT-READY-004, G6, amendments R1 and R2). That includes an
       interpreter without sys.monitoring (Python before 3.12): this file
       stays parseable by Python 3.10 so such an interpreter answers
       "unavailable" instead of crashing. The ready line lets the parent tell
       a worker that died while parsing the document (the document's doing)
       from one that failed before it could parse anything (a server fault).
       As a separate process it can also be killed at a deadline by
       src/services/pdf_text_extraction.py and kept away from the API's
       credentials: this file imports only the standard library at module
       level, imports pypdf only after the ceiling is verified, and never
       imports config, src or anything that reads .env.
How:   Started as [python, "-P", <this file>, <limits JSON>] with the PDF on
       stdin, in a neutral working directory. Once every safeguard is in
       place, and before it reads stdin, it writes and flushes the ready line
       "ready <ceiling> <pypdf version>". It then writes one result line,
       "<outcome> <ceiling> <pypdf version>", followed by the text only for
       RESULT_TEXT, and exits 0. A refusal decided before readiness is
       written as the result line alone. Tests:
       pytest tests/unit/services/test_pdf_text_worker.py
"""

from __future__ import annotations

import ctypes
import io
import json
import logging
import re
import sys
import zlib
from typing import Any, BinaryIO, Callable, NamedTuple

RESULT_TEXT = "text"
RESULT_UNREADABLE = "unreadable"
RESULT_ENCRYPTED = "encrypted"
RESULT_TOO_MANY_PAGES = "too-many-pages"
RESULT_OVER_LIMITS = "over-limits"
RESULT_UNAVAILABLE = "unavailable"
RESULT_TOKENS = frozenset(
    {
        RESULT_TEXT,
        RESULT_UNREADABLE,
        RESULT_ENCRYPTED,
        RESULT_TOO_MANY_PAGES,
        RESULT_OVER_LIMITS,
        RESULT_UNAVAILABLE,
    }
)

# The first field of the line the worker writes, and flushes, once its
# ceiling is verified and pypdf is imported, version-checked, hooked and
# self-tested, and before it reads the upload: "ready <ceiling> <pypdf
# version>". Not a result token. A worker that exits without a complete
# result after this line died on the document itself.
READY_TOKEN = "ready"

# A header's middle field names the operating-system ceiling in force when
# the worker decided. CEILING_NONE appears only on a RESULT_UNAVAILABLE
# written before any ceiling existed; the parent trusts no other outcome
# unless the ceiling is one of ENFORCED_CEILINGS.
CEILING_POSIX_RLIMIT = "posix-rlimit"
CEILING_WINDOWS_JOB_OBJECT = "windows-job-object"
CEILING_NONE = "none"
ENFORCED_CEILINGS = frozenset({CEILING_POSIX_RLIMIT, CEILING_WINDOWS_JOB_OBJECT})
# The header's last field before pypdf's version is known.
PARSER_VERSION_UNKNOWN = "none"

RESULT_SEPARATOR = b"\n"
HEADER_FIELD_SEPARATOR = " "
TOKEN_ENCODING = "ascii"
# surrogatepass on both sides of the pipe, so any str pypdf returns — lone
# surrogates included — reaches the parent exactly as the worker produced it.
TEXT_ENCODING = "utf-8"
TEXT_ENCODING_ERRORS = "surrogatepass"
# Today's normalisation, unchanged: page texts joined by one newline.
PAGE_SEPARATOR = "\n"

# Every header field is a short token of these characters, so a header can
# be logged as it stands and never carries a space, a newline or a control
# character.
_HEADER_FIELD_PATTERN = re.compile(r"[0-9A-Za-z.+_-]+")
# A version is its leading dotted numbers, optionally followed by a
# pre-release, post-release or local suffix; only the numbers are compared.
_VERSION_PATTERN = re.compile(r"(\d+(?:\.\d+)*)[0-9A-Za-z.+_-]*")
_VERSION_PART_SEPARATOR = "."

MAX_INPUT_BYTES_KEY = "max_input_bytes"
MAX_PAGES_KEY = "max_pages"
MAX_STREAM_OUTPUT_BYTES_KEY = "max_stream_output_bytes"
MAX_DECODED_BYTES_KEY = "max_decoded_bytes"
MAX_TEXT_CHARACTERS_KEY = "max_text_characters"
MEMORY_LIMIT_BYTES_KEY = "memory_limit_bytes"
CPU_LIMIT_SECONDS_KEY = "cpu_limit_seconds"
PARSER_MINIMUM_VERSION_KEY = "parser_minimum_version"
INTEGER_LIMIT_KEYS = frozenset(
    {
        MAX_INPUT_BYTES_KEY,
        MAX_PAGES_KEY,
        MAX_STREAM_OUTPUT_BYTES_KEY,
        MAX_DECODED_BYTES_KEY,
        MAX_TEXT_CHARACTERS_KEY,
        MEMORY_LIMIT_BYTES_KEY,
        CPU_LIMIT_SECONDS_KEY,
    }
)
LIMIT_KEYS = INTEGER_LIMIT_KEYS | {PARSER_MINIMUM_VERSION_KEY}

# pypdf.filters globals that bound one stream, read by pypdf at call time. Each
# must already exist: if a pypdf release renames one, setting it would create
# an unused attribute and leave the real limit at its default, so a missing
# name fails closed instead.
PYPDF_STREAM_LIMIT_ATTRIBUTES = (
    "ZLIB_MAX_OUTPUT_LENGTH",
    "LZW_MAX_OUTPUT_LENGTH",
    "RUN_LENGTH_MAX_OUTPUT_LENGTH",
    "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
    "JBIG2_MAX_OUTPUT_LENGTH",
    "FLATE_MAX_BUFFER_SIZE",
    "MAX_DECLARED_STREAM_LENGTH",
)
# EncodedStreamObject.get_data imports this from pypdf.filters at call time and
# is its only caller, so replacing it here sees every stream pypdf decodes.
PYPDF_DECODER_ATTRIBUTE = "decode_stream_data"
# pypdf decodes JBIG2 by running the external jbig2dec program it found on
# PATH at import. Text extraction never needs JBIG2 image data and the worker
# must start no program, so this is set to None before anything is decoded;
# pypdf then refuses a JBIG2 stream as it would any it cannot decode. The
# name must exist: a release that moved it would leave the program reachable.
PYPDF_EXTERNAL_DECODER_ATTRIBUTE = "JBIG2DEC_BINARY"
# pypdf extracts a Form XObject's text with PageObject.extract_xform_text,
# reporting the form's fragments to visitor_text as it goes, and then reports
# the whole text it returned to visitor_text once more. The worker wraps the
# method so that the re-report is not charged twice.
PYPDF_PAGE_ATTRIBUTE = "PageObject"
PYPDF_FORM_TEXT_ATTRIBUTE = "extract_xform_text"
# The exceptions the worker classifies, and what the self-test builds a
# stream from. pypdf.PdfReader is checked with them.
PYPDF_ERROR_ATTRIBUTES = ("LimitReachedError", "FileNotDecryptedError", "WrongPasswordError")
PYPDF_STREAM_ATTRIBUTES = ("EncodedStreamObject", "NameObject")
PYPDF_READER_ATTRIBUTE = "PdfReader"
_FLATE_FILTER_KEY = "/Filter"
_FLATE_FILTER_NAME = "/FlateDecode"
# Raised as pypdf's LimitReachedError when one stream decodes past the
# per-stream limit through a filter pypdf does not bound itself. It never
# leaves the worker.
_STREAM_OUTPUT_LIMIT_MESSAGE = "a decoded stream passed the per-stream limit"

# pypdf logs, on the "pypdf" logger, warnings that can quote the document.
# Nothing in the worker reads them; a handler that discards them keeps them
# off stderr (logging's last-resort handler) as well as out of the result.
PYPDF_LOGGER_NAME = "pypdf"

# The observer for failures pypdf swallows is registered under this name on
# the first sys.monitoring tool id no other tool holds.
OBSERVER_TOOL_NAME = "kerno-pdf-worker"
_MONITORING_ATTRIBUTE = "monitoring"
# A fixed CPython definition (PEP 669), not a tunable limit: sys.monitoring
# offers tool ids 0 to 5.
_MONITORING_TOOL_ID_COUNT = 6
_OBSERVER_SELF_TEST_MESSAGE = "Kerno PDF worker observer self-test"

# The parser self-test's fixed inputs, not tunable limits. pypdf's per-stream
# limit is one byte short of the oversized content, so the short content
# decodes well inside it and the oversized content cannot decode at all. The
# worker's own after-decode check is held at exactly the oversized length
# meanwhile, so only pypdf's limit can stop the oversized decode.
_SELF_TEST_CONTENT = b"Kerno PDF worker parser self-test."
_SELF_TEST_OVERSIZED_CONTENT = _SELF_TEST_CONTENT + _SELF_TEST_CONTENT
_SELF_TEST_STREAM_LIMIT_BYTES = len(_SELF_TEST_OVERSIZED_CONTENT) - 1
_SELF_TEST_AFTER_DECODE_LIMIT_BYTES = len(_SELF_TEST_OVERSIZED_CONTENT)

# Fixed Windows API definitions (winnt.h and jobapi2.h), not tunable limits:
# the JOBOBJECTINFOCLASS value of JOBOBJECT_EXTENDED_LIMIT_INFORMATION, the
# four limit flags the worker sets, the one process the job may hold (the
# worker itself, so it can start no other), and the 100-nanosecond units
# Windows counts CPU time in. The memory and CPU limits arrive from
# config/constants.py.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x2
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_REQUIRED_LIMIT_FLAGS = (
    _JOB_OBJECT_LIMIT_PROCESS_MEMORY
    | _JOB_OBJECT_LIMIT_PROCESS_TIME
    | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
)
_JOB_OBJECT_ACTIVE_PROCESS_LIMIT = 1
_WINDOWS_TIME_UNITS_PER_SECOND = 10_000_000
_KERNEL32_LIBRARY = "kernel32"
# The C types of the Windows API's HANDLE, BOOL, DWORD and JOBOBJECTINFOCLASS.
# HANDLE is pointer-sized, so a 64-bit handle is never truncated to a C int.
_WINDOWS_HANDLE = ctypes.c_void_p
_WINDOWS_BOOL = ctypes.c_int
_WINDOWS_DWORD = ctypes.c_uint32
_WINDOWS_INFORMATION_CLASS = ctypes.c_int

_EXIT_RESULT_WRITTEN = 0
_WINDOWS_PLATFORM = "win32"


class WorkerRefusal(Exception):
    """An outcome the worker decided itself, carried as its result token."""

    def __init__(self, token: str) -> None:
        """Record the result token this refusal stands for."""
        super().__init__(token)
        self.token = token


class WorkerResult(NamedTuple):
    """The one result: the outcome, the ceiling in force, pypdf's version, and the text."""

    outcome: str
    ceiling: str
    parser_version: str
    text: str


class ResultHeader(NamedTuple):
    """A header line as the parent reads it back: the ready line (outcome READY_TOKEN) or the result line."""

    outcome: str
    ceiling: str
    parser_version: str


class _JobBasicLimitInformation(ctypes.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION, laid out as winnt.h defines it."""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JobIoCounters(ctypes.Structure):
    """IO_COUNTERS, laid out as winnt.h defines it; the worker never reads it."""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION, laid out as winnt.h defines it."""

    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Budget:
    """A running total against a cap that, once passed, stays passed.

    The flag is sticky because pypdf catches and ignores some exceptions while
    extracting text: a refusal raised inside one of those handlers would
    otherwise be lost. The worker re-checks every budget after each page.
    """

    def __init__(self, limit: int) -> None:
        """Start an empty budget with the given cap."""
        self.limit = limit
        self.used = 0
        self.exceeded = False

    def raise_if_exceeded(self) -> None:
        """Raise the over-limits refusal if this budget has ever been passed."""
        if self.exceeded:
            raise WorkerRefusal(RESULT_OVER_LIMITS)

    def charge(self, amount: int) -> None:
        """Add an amount to the total and raise the over-limits refusal if it passes the cap."""
        self.used += amount
        if self.used > self.limit:
            self.exceeded = True
        self.raise_if_exceeded()


class DecodedBytesBudget(_Budget):
    """The document's budget of decoded stream bytes, and the size any one decoded stream may reach.

    Both are charged as each stream finishes decoding. pypdf stops its Flate,
    LZW and RunLength decoders as their output grows past the per-stream
    limit; the after-decode check holds every other filter, ASCII85 above all,
    to the same limit.
    """

    def __init__(self, limit: int, stream_limit: int, limit_error: type[BaseException]) -> None:
        """Start the budget; limit_error is pypdf's per-stream LimitReachedError."""
        super().__init__(limit)
        self.stream_limit = stream_limit
        self.limit_error = limit_error

    def reset(self, limit: int, stream_limit: int) -> None:
        """Empty the budget and give it new caps; used around the parser self-test."""
        self.limit = limit
        self.stream_limit = stream_limit
        self.used = 0
        self.exceeded = False

    def guard(self, decode: Callable[[Any], bytes]) -> Callable[[Any], bytes]:
        """Return a stream decoder that refuses once the budget is spent and charges every output.

        pypdf's own per-stream limit, or running out of memory, while decoding
        marks the budget exceeded before the error propagates, so the document
        is refused even if pypdf swallows that error further up.
        """

        def decode_within_budget(stream: Any) -> bytes:
            """Decode one stream with pypdf's decoder, then hold its output to both limits."""
            self.raise_if_exceeded()
            try:
                data = decode(stream)
            except (self.limit_error, MemoryError):
                self.exceeded = True
                raise
            self.charge_stream(len(data))
            return data

        return decode_within_budget

    def charge_stream(self, size: int) -> None:
        """Charge one decoded stream; past the per-stream limit, mark the budget exceeded and raise LimitReachedError."""
        if size > self.stream_limit:
            self.exceeded = True
            raise self.limit_error(_STREAM_OUTPUT_LIMIT_MESSAGE)
        self.charge(size)


class CharacterBudget(_Budget):
    """A budget of extracted text characters, cumulative across pages, charging each character once."""

    def __init__(self, limit: int) -> None:
        """Start an empty budget with the given cap and no form text awaiting its re-report."""
        super().__init__(limit)
        self._form_text_to_skip: str | None = None

    def count_fragment(
        self,
        fragment: str,
        transform_matrix: Any,
        text_matrix: Any,
        font_resource: Any,
        font_size: Any,
    ) -> None:
        """pypdf's visitor_text callback: charge one fragment, so extraction stops mid-page.

        A Form XObject's text was charged fragment by fragment while it was
        extracted, and pypdf re-reports it whole in the call straight after
        extract_xform_text returns it. Only that call, recognised as the very
        string object just returned, goes uncharged; any call clears the
        expectation, so a form at any depth is charged exactly once.
        """
        form_text = self._form_text_to_skip
        self._form_text_to_skip = None
        if form_text is not None and fragment is form_text:
            return
        self.charge(len(fragment))

    def guard_form_text(self, extract_xform_text: Callable[..., str]) -> Callable[..., str]:
        """Return a PageObject.extract_xform_text that tells this budget which re-report to skip."""

        def extract_xform_text_charged_once(page: Any, *arguments: Any, **keyword_arguments: Any) -> str:
            """Extract a form's text as pypdf does, then expect pypdf to re-report exactly that string."""
            form_text = extract_xform_text(page, *arguments, **keyword_arguments)
            self._form_text_to_skip = form_text
            return form_text

        return extract_xform_text_charged_once


class SwallowedFailureObserver:
    """Marks the document over the limits whenever a MemoryError or LimitReachedError is raised, caught or not.

    Its callback is registered for sys.monitoring's RAISE event, which fires
    for every exception raised anywhere in this process, in each frame it
    passes through, before any handler can swallow it. The callback only
    makes an isinstance check against a tuple built in advance and stores
    into a one-slot list made in advance, so it allocates nothing while
    memory may be exhausted. The mark stays set, like a budget's flag, and
    is checked after each page.
    """

    def __init__(self, tool_id: int, limit_error: type[BaseException]) -> None:
        """Prepare the mark and the callback for the given tool id; limit_error is pypdf's LimitReachedError."""
        self.tool_id = tool_id
        self._mark = [False]
        self.callback = _raise_callback((MemoryError, limit_error), self._mark)

    @property
    def exceeded(self) -> bool:
        """Return whether an observed failure has been raised since the last reset."""
        return self._mark[0]

    def reset(self) -> None:
        """Clear the mark; used after the self-tests."""
        self._mark[0] = False


def _raise_callback(observed: tuple[type[BaseException], ...], mark: list[bool]) -> Callable[[Any, int, Any], None]:
    """Return the RAISE callback that sets mark[0] when the raised exception is one of observed."""

    def observe_raise(code: Any, instruction_offset: int, exception: Any) -> None:
        """sys.monitoring RAISE callback: mark an observed failure and do nothing else."""
        if isinstance(exception, observed):
            mark[0] = True

    return observe_raise


class _DocumentBudgets:
    """The three budgets one document is read under, and the observer for failures pypdf swallows."""

    def __init__(
        self, limits: dict[str, Any], limit_error: type[BaseException], observer: SwallowedFailureObserver
    ) -> None:
        """Create the decoded-byte, fragment and page-text budgets from the limits, beside the observer."""
        self.decoded = DecodedBytesBudget(
            limits[MAX_DECODED_BYTES_KEY], limits[MAX_STREAM_OUTPUT_BYTES_KEY], limit_error
        )
        self.fragments = CharacterBudget(limits[MAX_TEXT_CHARACTERS_KEY])
        self.page_text = CharacterBudget(limits[MAX_TEXT_CHARACTERS_KEY])
        self.observer = observer

    def any_exceeded(self) -> bool:
        """Return whether any budget has ever been passed or the observer has seen a memory or limit failure."""
        budget_passed = self.decoded.exceeded or self.fragments.exceeded or self.page_text.exceeded
        return budget_passed or self.observer.exceeded

    def raise_if_any_exceeded(self) -> None:
        """Raise the over-limits refusal if any budget has ever been passed or the observer has seen a failure."""
        if self.any_exceeded():
            raise WorkerRefusal(RESULT_OVER_LIMITS)


# ── Versions and the result header (shared with the parent) ─────────────────


def parse_version(text: str) -> tuple[int, ...] | None:
    """Return the leading dotted numbers of a version string, or None if it has none.

    The whole string must be a version made of _VERSION_PATTERN's characters,
    so a parsed version is also safe to write into the result header.
    """
    match = _VERSION_PATTERN.fullmatch(text)
    if match is None:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split(_VERSION_PART_SEPARATOR))
    except ValueError:
        return None


def version_at_least(found: str, minimum: str) -> bool:
    """Return whether found's leading numbers are at or above minimum's; unparsable is never enough."""
    found_parts = parse_version(found)
    minimum_parts = parse_version(minimum)
    if found_parts is None or minimum_parts is None:
        return False
    width = max(len(found_parts), len(minimum_parts))
    return _padded(found_parts, width) >= _padded(minimum_parts, width)


def _padded(parts: tuple[int, ...], width: int) -> tuple[int, ...]:
    """Return parts extended with zeros to width, so 6.14 compares as 6.14.0."""
    return parts + (0,) * (width - len(parts))


def parse_result_header(line: bytes) -> ResultHeader | None:
    """Return the header a result line carries, or None if it is not exactly one well-formed header.

    Three ASCII fields of header-safe characters separated by single spaces,
    the first a known outcome token. The parent decides separately whether
    the ceiling and version are ones it trusts.
    """
    header = _parse_header_fields(line)
    if header is None or header.outcome not in RESULT_TOKENS:
        return None
    return header


def parse_ready_line(line: bytes) -> ResultHeader | None:
    """Return the fields of a ready line, or None unless it is one well-formed header whose first field is READY_TOKEN."""
    header = _parse_header_fields(line)
    if header is None or header.outcome != READY_TOKEN:
        return None
    return header


def _parse_header_fields(line: bytes) -> ResultHeader | None:
    """Return a line's three fields, or None unless they are ASCII header-safe tokens separated by single spaces."""
    try:
        decoded = line.decode(TOKEN_ENCODING)
    except UnicodeDecodeError:
        return None
    fields = decoded.split(HEADER_FIELD_SEPARATOR)
    if len(fields) != len(ResultHeader._fields):
        return None
    for field in fields:
        if _HEADER_FIELD_PATTERN.fullmatch(field) is None:
            return None
    return ResultHeader(*fields)


def header_line(first_field: str, ceiling: str, parser_version: str) -> bytes:
    """Return one header line as the worker writes it: three fields joined by single spaces, then the separator."""
    fields = HEADER_FIELD_SEPARATOR.join((first_field, ceiling, parser_version))
    return fields.encode(TOKEN_ENCODING) + RESULT_SEPARATOR


# ── Limits ───────────────────────────────────────────────────────────────────


def parse_limits(arguments: list[str]) -> dict[str, Any]:
    """Return the limits from the worker's single JSON argument.

    Anything but one JSON object holding exactly LIMIT_KEYS, the integer
    limits each a positive integer and the parser minimum a parsable version
    string, is refused as RESULT_UNAVAILABLE: the parent built it, so a
    mismatch is a server fault, and guessing a limit would fail open.
    """
    if len(arguments) != 1:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    try:
        limits = json.loads(arguments[0])
    except ValueError as exc:
        raise WorkerRefusal(RESULT_UNAVAILABLE) from exc
    if not isinstance(limits, dict) or set(limits) != LIMIT_KEYS:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    for key in INTEGER_LIMIT_KEYS:
        if not _is_positive_integer(limits[key]):
            raise WorkerRefusal(RESULT_UNAVAILABLE)
    minimum = limits[PARSER_MINIMUM_VERSION_KEY]
    if not isinstance(minimum, str) or parse_version(minimum) is None:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    return limits


def _is_positive_integer(value: Any) -> bool:
    """Return whether a JSON value is a positive int; JSON true is a Python int but no limit."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# ── The operating-system ceiling (R1) ───────────────────────────────────────


def install_ceiling(limits: dict[str, Any], platform: str | None = None) -> str:
    """Put this process under its memory and CPU ceiling, confirm it is in force, and name it.

    Windows: a Job Object (CEILING_WINDOWS_JOB_OBJECT). Elsewhere: RLIMIT_AS
    and RLIMIT_CPU (CEILING_POSIX_RLIMIT); a platform without the resource
    module has no ceiling to offer. Any missing API, failed call or
    verification that disagrees raises WorkerRefusal(RESULT_UNAVAILABLE).
    Runs before the upload is read and before pypdf is imported. platform
    defaults to sys.platform; tests name another.
    """
    chosen_platform = sys.platform if platform is None else platform
    if chosen_platform == _WINDOWS_PLATFORM:
        apply_job_object_ceiling(limits, _load_kernel32())
        return CEILING_WINDOWS_JOB_OBJECT
    apply_rlimit_ceiling(limits, _import_resource_module())
    return CEILING_POSIX_RLIMIT


def _load_kernel32() -> Any:
    """Return kernel32 with the five Job Object calls declared, or refuse as RESULT_UNAVAILABLE.

    Declaring every argument and result type stops ctypes from guessing C
    ints, which would truncate a 64-bit handle on the way in or out.
    """
    try:
        kernel32 = ctypes.WinDLL(_KERNEL32_LIBRARY, use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [_WINDOWS_HANDLE, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = _WINDOWS_HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            _WINDOWS_HANDLE, _WINDOWS_INFORMATION_CLASS, _WINDOWS_HANDLE, _WINDOWS_DWORD
        ]
        kernel32.SetInformationJobObject.restype = _WINDOWS_BOOL
        kernel32.AssignProcessToJobObject.argtypes = [_WINDOWS_HANDLE, _WINDOWS_HANDLE]
        kernel32.AssignProcessToJobObject.restype = _WINDOWS_BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = _WINDOWS_HANDLE
        kernel32.QueryInformationJobObject.argtypes = [
            _WINDOWS_HANDLE, _WINDOWS_INFORMATION_CLASS, _WINDOWS_HANDLE, _WINDOWS_DWORD, _WINDOWS_HANDLE
        ]
        kernel32.QueryInformationJobObject.restype = _WINDOWS_BOOL
    except (AttributeError, OSError) as exc:
        raise WorkerRefusal(RESULT_UNAVAILABLE) from exc
    return kernel32


def apply_job_object_ceiling(limits: dict[str, Any], kernel32: Any) -> None:
    """Assign this process to a new Job Object capping its memory, CPU time and processes, then verify.

    The job is new and empty, so it nests under any job the process is
    already in (Windows 8 and later). It may hold one process, this one, so
    any attempt to start another fails; and it is closed with the process,
    which ends anything still in it. Its handle is therefore kept open for
    the life of the process: closing it would end the worker. Any call that
    fails raises WorkerRefusal(RESULT_UNAVAILABLE); so does a verification
    that disagrees.
    """
    requested = _requested_job_limits(limits)
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    information_set = kernel32.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(requested), ctypes.sizeof(requested)
    )
    if not information_set:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    verify_job_object_ceiling(limits, kernel32)


def _requested_job_limits(limits: dict[str, Any]) -> _JobExtendedLimitInformation:
    """Return the extended limits naming the memory, CPU-time and process ceilings and the four flags that enforce them."""
    requested = _JobExtendedLimitInformation()
    basic = requested.BasicLimitInformation
    basic.LimitFlags = _JOB_OBJECT_REQUIRED_LIMIT_FLAGS
    basic.PerProcessUserTimeLimit = limits[CPU_LIMIT_SECONDS_KEY] * _WINDOWS_TIME_UNITS_PER_SECOND
    basic.ActiveProcessLimit = _JOB_OBJECT_ACTIVE_PROCESS_LIMIT
    requested.ProcessMemoryLimit = limits[MEMORY_LIMIT_BYTES_KEY]
    return requested


def verify_job_object_ceiling(limits: dict[str, Any], kernel32: Any) -> None:
    """Read back this process's own job and refuse unless every ceiling is in force at or below its limit.

    QueryInformationJobObject with a NULL job reads the calling process's
    immediate job, which is the one just assigned. All four flags must be
    set, and the memory, CPU-time and process limits must each be non-zero
    and no higher than requested.
    """
    seen = _JobExtendedLimitInformation()
    queried = kernel32.QueryInformationJobObject(
        None, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(seen), ctypes.sizeof(seen), None
    )
    if not queried:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    basic = seen.BasicLimitInformation
    flags_in_force = (basic.LimitFlags & _JOB_OBJECT_REQUIRED_LIMIT_FLAGS) == _JOB_OBJECT_REQUIRED_LIMIT_FLAGS
    memory_in_force = 0 < seen.ProcessMemoryLimit <= limits[MEMORY_LIMIT_BYTES_KEY]
    wanted_time = limits[CPU_LIMIT_SECONDS_KEY] * _WINDOWS_TIME_UNITS_PER_SECOND
    time_in_force = 0 < basic.PerProcessUserTimeLimit <= wanted_time
    processes_in_force = 0 < basic.ActiveProcessLimit <= _JOB_OBJECT_ACTIVE_PROCESS_LIMIT
    if not (flags_in_force and memory_in_force and time_in_force and processes_in_force):
        raise WorkerRefusal(RESULT_UNAVAILABLE)


def apply_rlimit_ceiling(limits: dict[str, Any], resource_module: Any) -> None:
    """Lower this process's address-space and CPU limits to the limits, then verify the soft limits.

    Never raises an existing lower hard limit. A missing name, a refused
    setrlimit or a soft limit left above the wanted one (or unlimited) is
    RESULT_UNAVAILABLE: reading a hostile PDF without the ceiling would fail
    open. Tests pass a fake resource_module.
    """
    try:
        wanted = (
            (resource_module.RLIMIT_AS, limits[MEMORY_LIMIT_BYTES_KEY]),
            (resource_module.RLIMIT_CPU, limits[CPU_LIMIT_SECONDS_KEY]),
        )
        for which, ceiling in wanted:
            _lower_ceiling(resource_module, which, ceiling)
        for which, ceiling in wanted:
            _verify_soft_limit(resource_module, which, ceiling)
    except (AttributeError, ValueError, OSError) as exc:
        raise WorkerRefusal(RESULT_UNAVAILABLE) from exc


def _import_resource_module() -> Any:
    """Import the POSIX resource module, refusing as RESULT_UNAVAILABLE if it is missing."""
    try:
        import resource
    except ImportError as exc:
        raise WorkerRefusal(RESULT_UNAVAILABLE) from exc
    return resource


def _lower_ceiling(resource_module: Any, which: int, wanted: int) -> None:
    """Set one resource's soft and hard limits to wanted, never raising an existing lower limit."""
    soft, hard = resource_module.getrlimit(which)
    unlimited = resource_module.RLIM_INFINITY
    new_hard = _tighter(hard, wanted, unlimited)
    new_soft = _tighter(soft, new_hard, unlimited)
    resource_module.setrlimit(which, (new_soft, new_hard))


def _tighter(current: int, wanted: int, unlimited: int) -> int:
    """Return the lower of an existing limit and the wanted one; unlimited is above both."""
    if current == unlimited:
        return wanted
    return min(current, wanted)


def _verify_soft_limit(resource_module: Any, which: int, wanted: int) -> None:
    """Refuse unless the resource's soft limit, the one enforced, is set and no higher than wanted."""
    soft, _hard = resource_module.getrlimit(which)
    if soft == resource_module.RLIM_INFINITY or soft > wanted:
        raise WorkerRefusal(RESULT_UNAVAILABLE)


# ── The parser: version, hooks, observer and self-tests (R2) ────────────────


def import_parser() -> Any:
    """Import pypdf and the submodules the worker relies on; missing or failing to import is RESULT_UNAVAILABLE.

    Any exception counts, not only ImportError: a pypdf that cannot run on
    this interpreter fails while importing with whatever its first
    incompatibility raises, and nothing can be parsed either way.
    """
    try:
        import pypdf
        import pypdf.errors
        import pypdf.filters
        import pypdf.generic
    except Exception as exc:
        raise WorkerRefusal(RESULT_UNAVAILABLE) from exc
    return pypdf


def reported_parser_version(pypdf: Any) -> str:
    """Return pypdf.__version__ when it is a parsable version string, else refuse as RESULT_UNAVAILABLE."""
    version = getattr(pypdf, "__version__", None)
    if not isinstance(version, str) or parse_version(version) is None:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    return version


def prepare_parser(pypdf: Any, limits: dict[str, Any], parser_version: str) -> _DocumentBudgets:
    """Confirm pypdf can be bounded as the worker requires, then bound it for the upload.

    In order: the version is at least the limits' minimum; every name exists;
    the external JBIG2 decoder is disabled; pypdf's logging is quietened; the
    observer is registered; every decode and every form's text are routed
    through the document's budgets; both self-tests pass; then the real
    per-stream limits are set and the decoded-byte budget emptied to its real
    caps. Any failure is RESULT_UNAVAILABLE, before the upload is read.
    """
    if not version_at_least(parser_version, limits[PARSER_MINIMUM_VERSION_KEY]):
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    require_parser_hooks(pypdf)
    disable_external_decoders(pypdf.filters)
    quiet_parser_logging()
    observer = install_failure_observer(pypdf.errors.LimitReachedError)
    budgets = _DocumentBudgets(limits, pypdf.errors.LimitReachedError, observer)
    route_parser_through_budgets(pypdf, budgets)
    run_parser_self_test(pypdf, budgets.decoded, observer)
    run_observer_self_test(observer)
    set_stream_limits(pypdf.filters, limits[MAX_STREAM_OUTPUT_BYTES_KEY])
    budgets.decoded.reset(limits[MAX_DECODED_BYTES_KEY], limits[MAX_STREAM_OUTPUT_BYTES_KEY])
    return budgets


def require_parser_hooks(pypdf: Any) -> None:
    """Refuse as RESULT_UNAVAILABLE, changing nothing, unless every name the worker relies on exists."""
    filter_names = PYPDF_STREAM_LIMIT_ATTRIBUTES + (PYPDF_DECODER_ATTRIBUTE, PYPDF_EXTERNAL_DECODER_ATTRIBUTE)
    required = (
        (pypdf.filters, filter_names),
        (pypdf.errors, PYPDF_ERROR_ATTRIBUTES),
        (pypdf.generic, PYPDF_STREAM_ATTRIBUTES),
        (pypdf, (PYPDF_READER_ATTRIBUTE, PYPDF_PAGE_ATTRIBUTE)),
    )
    for module, names in required:
        for name in names:
            if not hasattr(module, name):
                raise WorkerRefusal(RESULT_UNAVAILABLE)
    if not hasattr(getattr(pypdf, PYPDF_PAGE_ATTRIBUTE), PYPDF_FORM_TEXT_ATTRIBUTE):
        raise WorkerRefusal(RESULT_UNAVAILABLE)


def disable_external_decoders(filters: Any) -> None:
    """Forget the jbig2dec program pypdf found, so a JBIG2 stream is refused and no program is ever started."""
    setattr(filters, PYPDF_EXTERNAL_DECODER_ATTRIBUTE, None)


def quiet_parser_logging() -> None:
    """Discard pypdf's log records: nothing reads them, and they may quote the document."""
    logging.getLogger(PYPDF_LOGGER_NAME).addHandler(logging.NullHandler())


def route_parser_through_budgets(pypdf: Any, budgets: _DocumentBudgets) -> None:
    """Route every stream decode through the decoded-byte budget and every form's text through the fragment budget."""
    original_decoder = getattr(pypdf.filters, PYPDF_DECODER_ATTRIBUTE)
    setattr(pypdf.filters, PYPDF_DECODER_ATTRIBUTE, budgets.decoded.guard(original_decoder))
    page_class = getattr(pypdf, PYPDF_PAGE_ATTRIBUTE)
    original_form_text = getattr(page_class, PYPDF_FORM_TEXT_ATTRIBUTE)
    setattr(page_class, PYPDF_FORM_TEXT_ATTRIBUTE, budgets.fragments.guard_form_text(original_form_text))


def install_failure_observer(limit_error: type[BaseException]) -> SwallowedFailureObserver:
    """Register the observer for every RAISE event in this process, or refuse as RESULT_UNAVAILABLE.

    Without sys.monitoring (Python before 3.12), or with every tool id
    already held by another tool, a failure pypdf swallows could truncate a
    document silently, so the worker parses nothing rather than read without
    the observer.
    """
    monitoring = getattr(sys, _MONITORING_ATTRIBUTE, None)
    if monitoring is None:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    try:
        tool_id = _claim_free_tool_id(monitoring)
        observer = SwallowedFailureObserver(tool_id, limit_error)
        monitoring.register_callback(tool_id, monitoring.events.RAISE, observer.callback)
        monitoring.set_events(tool_id, monitoring.events.RAISE)
    except (AttributeError, TypeError, ValueError) as exc:
        raise WorkerRefusal(RESULT_UNAVAILABLE) from exc
    return observer


def _claim_free_tool_id(monitoring: Any) -> int:
    """Claim the first sys.monitoring tool id no other tool holds, or refuse as RESULT_UNAVAILABLE."""
    for tool_id in range(_MONITORING_TOOL_ID_COUNT):
        try:
            monitoring.use_tool_id(tool_id, OBSERVER_TOOL_NAME)
        except ValueError:
            continue
        return tool_id
    raise WorkerRefusal(RESULT_UNAVAILABLE)


def set_stream_limits(filters: Any, limit: int) -> None:
    """Write one per-stream output limit into every pypdf.filters limit global."""
    for name in PYPDF_STREAM_LIMIT_ATTRIBUTES:
        setattr(filters, name, limit)


def run_parser_self_test(pypdf: Any, decoded_budget: DecodedBytesBudget, observer: SwallowedFailureObserver) -> None:
    """Prove the parser protections bind before any upload is parsed; refuse as RESULT_UNAVAILABLE if not.

    With pypdf's per-stream limit set tiny, a small Flate stream must decode
    through the decoded-byte budget (so EncodedStreamObject.get_data uses the
    wrapped decoder) without the observer noticing anything, and an
    oversized one must raise LimitReachedError from get_data (so pypdf reads
    the limit at decode time), marking both the budget and the observer. Any
    other outcome, any other exception included, means the limits would not
    hold.
    """
    set_stream_limits(pypdf.filters, _SELF_TEST_STREAM_LIMIT_BYTES)
    decoded_budget.reset(_SELF_TEST_STREAM_LIMIT_BYTES, _SELF_TEST_AFTER_DECODE_LIMIT_BYTES)
    observer.reset()
    try:
        passed = _self_test_decodes(pypdf, decoded_budget, observer)
    except Exception as exc:
        raise WorkerRefusal(RESULT_UNAVAILABLE) from exc
    if not passed:
        raise WorkerRefusal(RESULT_UNAVAILABLE)


def _self_test_decodes(pypdf: Any, decoded_budget: DecodedBytesBudget, observer: SwallowedFailureObserver) -> bool:
    """Return whether the in-limit decode was charged exactly and unobserved, and the oversized one hit the limit."""
    in_limit = _flate_stream(pypdf.generic, _SELF_TEST_CONTENT)
    if in_limit.get_data() != _SELF_TEST_CONTENT:
        return False
    if decoded_budget.used != len(_SELF_TEST_CONTENT) or decoded_budget.exceeded or observer.exceeded:
        return False
    oversized = _flate_stream(pypdf.generic, _SELF_TEST_OVERSIZED_CONTENT)
    try:
        oversized.get_data()
    except pypdf.errors.LimitReachedError:
        return decoded_budget.exceeded and observer.exceeded
    return False


def _flate_stream(generic: Any, content: bytes) -> Any:
    """Return a fresh, never-decoded FlateDecode stream object holding content compressed."""
    stream = generic.EncodedStreamObject()
    stream[generic.NameObject(_FLATE_FILTER_KEY)] = generic.NameObject(_FLATE_FILTER_NAME)
    stream._data = zlib.compress(content)
    return stream


def run_observer_self_test(observer: SwallowedFailureObserver) -> None:
    """Prove the observer sees a MemoryError raised and swallowed in this process; refuse as RESULT_UNAVAILABLE if not.

    The parser self-test has already shown it sees pypdf's LimitReachedError.
    A MemoryError caught where it is raised, as pypdf's broad handlers would,
    must mark it too. The mark is then cleared for the upload.
    """
    observer.reset()
    _raise_and_swallow_memory_error()
    if not observer.exceeded:
        raise WorkerRefusal(RESULT_UNAVAILABLE)
    observer.reset()


def _raise_and_swallow_memory_error() -> None:
    """Raise a MemoryError and swallow it at once, as pypdf's "except Exception: pass" handlers do."""
    try:
        raise MemoryError(_OBSERVER_SELF_TEST_MESSAGE)
    except MemoryError:
        pass


# ── The document ─────────────────────────────────────────────────────────────


def read_input(input_stream: BinaryIO, max_input_bytes: int) -> bytes:
    """Read the whole document, refusing as RESULT_OVER_LIMITS past max_input_bytes."""
    data = input_stream.read(max_input_bytes + 1)
    if len(data) > max_input_bytes:
        raise WorkerRefusal(RESULT_OVER_LIMITS)
    return data


def extract_document(pypdf: Any, data: bytes, limits: dict[str, Any], budgets: _DocumentBudgets) -> str:
    """Return the joined text of every page, or raise WorkerRefusal carrying the outcome's token."""
    try:
        text = _read_pages(pypdf, data, limits, budgets)
    except Exception as exc:
        raise WorkerRefusal(_classify_failure(exc, pypdf.errors, budgets)) from exc
    budgets.raise_if_any_exceeded()
    return text


def _read_pages(pypdf: Any, data: bytes, limits: dict[str, Any], budgets: _DocumentBudgets) -> str:
    """Count the pages, then extract each page's text under the budgets and join them as today."""
    reader = pypdf.PdfReader(io.BytesIO(data))
    if len(reader.pages) > limits[MAX_PAGES_KEY]:
        raise WorkerRefusal(RESULT_TOO_MANY_PAGES)
    page_texts: list[str] = []
    for page in reader.pages:
        page_text = page.extract_text(visitor_text=budgets.fragments.count_fragment) or ""
        budgets.raise_if_any_exceeded()
        if page_texts:
            budgets.page_text.charge(len(PAGE_SEPARATOR))
        budgets.page_text.charge(len(page_text))
        page_texts.append(page_text)
    return PAGE_SEPARATOR.join(page_texts)


def _classify_failure(exc: Exception, errors: Any, budgets: _DocumentBudgets) -> str:
    """Return the result token for an exception raised while reading the document.

    A passed budget or an observed failure wins over whatever exception
    surfaced, because pypdf may have caught the real cause and raised
    something else later (a LimitReachedError re-raised as PdfReadError).
    """
    if budgets.any_exceeded():
        return RESULT_OVER_LIMITS
    if isinstance(exc, WorkerRefusal):
        return exc.token
    if isinstance(exc, (errors.LimitReachedError, MemoryError)):
        return RESULT_OVER_LIMITS
    if isinstance(exc, (errors.FileNotDecryptedError, errors.WrongPasswordError)):
        return RESULT_ENCRYPTED
    return RESULT_UNREADABLE


# ── One run ──────────────────────────────────────────────────────────────────


def run_worker(arguments: list[str], input_stream: BinaryIO, result_channel: BinaryIO) -> WorkerResult:
    """Return the one result for the document on input_stream, read under the argv limits.

    The order is the guarantee: limits, then the OS ceiling installed and
    verified, then pypdf imported, version-checked, hooked, observed and
    self-tested, then the ready line written to result_channel, and only then
    the upload read and parsed. A refusal at any step is the result,
    reporting whatever ceiling and pypdf version were known by then; one
    decided before the ready line leaves result_channel untouched.
    """
    ceiling = CEILING_NONE
    parser_version = PARSER_VERSION_UNKNOWN
    try:
        limits = parse_limits(arguments)
        ceiling = install_ceiling(limits)
        pypdf = import_parser()
        parser_version = reported_parser_version(pypdf)
        budgets = prepare_parser(pypdf, limits, parser_version)
        announce_ready(result_channel, ceiling, parser_version)
        data = read_input(input_stream, limits[MAX_INPUT_BYTES_KEY])
        text = extract_document(pypdf, data, limits, budgets)
    except WorkerRefusal as refusal:
        return WorkerResult(refusal.token, ceiling, parser_version, "")
    return WorkerResult(RESULT_TEXT, ceiling, parser_version, text)


def announce_ready(result_channel: BinaryIO, ceiling: str, parser_version: str) -> None:
    """Write and flush the ready line: every safeguard is in place and the document is read next."""
    result_channel.write(header_line(READY_TOKEN, ceiling, parser_version))
    result_channel.flush()


def write_result(result_channel: BinaryIO, result: WorkerResult) -> None:
    """Write the one result: the result line, then the text for RESULT_TEXT only, and flush."""
    result_channel.write(header_line(result.outcome, result.ceiling, result.parser_version))
    if result.outcome == RESULT_TEXT:
        result_channel.write(result.text.encode(TEXT_ENCODING, TEXT_ENCODING_ERRORS))
    result_channel.flush()


def main() -> int:
    """Read the document on stdin under the argv limits and write the ready line and the one result to stdout.

    sys.stdout is pointed at stderr before pypdf is imported, so nothing pypdf
    prints can enter the result; the parent discards stderr.
    """
    result_channel = sys.stdout.buffer
    sys.stdout = sys.stderr
    result = run_worker(sys.argv[1:], sys.stdin.buffer, result_channel)
    write_result(result_channel, result)
    return _EXIT_RESULT_WRITTEN


if __name__ == "__main__":
    sys.exit(main())
