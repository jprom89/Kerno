"""
probe_worker.py — stands in for the PDF worker so the parent's process handling can be proven.

What:  Behaves as one deliberately chosen worker would, selected by its first
       argument. Like the real worker, every mode that produces a result
       first writes and flushes a ready line ("ready <ceiling> <version>")
       before it reads stdin. "hang" writes a started marker, sleeps without
       reading stdin, then writes a survived marker (which must never appear
       if the parent killed it); "ready-then-hang" does the same after a
       ready line; "environment" returns its environment, its working
       directory and what that directory holds as the text result;
       "load-dotenv" loads the .env files it is given into os.environ, as a
       .env loader would, then announces readiness and holds before its
       result, so the parent can see the loaded values from outside;
       "flood" writes a valid ready line and
       header and then output for ever; "exact-output" writes exactly the
       number of bytes it is given; "answer-without-reading" refuses the
       document without reading stdin; "terminated-before-ready" and
       "terminated-after-ready" have the operating system end the process
       abruptly, as a crash or an enforced ceiling would; the raw modes write
       one exact, usually malformed, untrusted or truncated output and exit
       with a chosen status; "ceilings" loads the real worker module from a
       path, installs its OS ceiling on this process and reports what is in
       force and whether an allocation past it fails; "allocate-until-refused"
       installs that real ceiling and then commits memory in fixed steps
       until the ceiling refuses one, reporting how far it got.
Why:   Timeouts, kills, reaping, bounded output, the credential-free
       environment, the ready line, the parent's refusal to trust a broken
       or unverified result, and where exactly the real ceiling refuses
       memory can only be shown with a real child process that misbehaves on
       purpose. Standard library only (outside_view.py beside it is loaded
       from its path); it cannot import config, because the parent starts it
       in a private, empty working directory.
How:   Started only by tests/unit/services/test_pdf_text_extraction.py,
       test_pdf_text_worker.py and tests/unit/api/test_evidence_upload_admission.py
       as [python, <this file>, <mode>, *arguments], with -P before the
       file for "allocate-until-refused". The file name does not match
       python_files, so pytest never collects it.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

NONZERO_EXIT_STATUS = 3
_SUCCESS_EXIT = 0
_SURROGATE_TEXT = "lone \ud800 surrogate"
_FLOOD_CHUNK = b"f" * (64 * 1024)
_MEBIBYTE = 1024 * 1024
_PAST_THE_CEILING_BYTES = 128 * _MEBIBYTE
_SMALL_ALLOCATION_BYTES = 16 * _MEBIBYTE
_WINDOWS_PLATFORM = "win32"
_OUTSIDE_VIEW_PATH = Path(__file__).resolve().with_name("outside_view.py")
# The exit status Windows gives a process ended by an access violation
# (STATUS_ACCESS_VIOLATION), which is how the real worker was seen to die on
# a ToUnicode amplifier. Elsewhere the probe is killed by SIGKILL, as an
# out-of-memory killer or an exhausted RLIMIT_CPU would end it.
ACCESS_VIOLATION_EXIT_STATUS = 0xC0000005
TERMINATED_EXIT_STATUS = ACCESS_VIOLATION_EXIT_STATUS if sys.platform == _WINDOWS_PLATFORM else -signal.SIGKILL

# A pypdf version far above any minimum the API could require, and one far
# below, so no probe needs to know PDF_PARSER_MINIMUM_VERSION.
_NEW_PARSER = b"999.0.0"
_OLD_PARSER = b"0.0.1"
_JOB_OBJECT = b"windows-job-object"
_RLIMIT = b"posix-rlimit"
_READY = b"ready"


def _header(outcome: bytes, ceiling: bytes = _JOB_OBJECT, version: bytes = _NEW_PARSER) -> bytes:
    """Return one header line in the worker's format: a ready line or a result line."""
    return outcome + b" " + ceiling + b" " + version + b"\n"


def _ready(ceiling: bytes = _JOB_OBJECT, version: bytes = _NEW_PARSER) -> bytes:
    """Return the ready line the real worker writes before it reads the document."""
    return _header(_READY, ceiling, version)


def _announced(result: bytes, ceiling: bytes = _JOB_OBJECT, version: bytes = _NEW_PARSER) -> bytes:
    """Return result preceded by a ready line naming the same ceiling and version, as the real worker writes it."""
    return _ready(ceiling, version) + result


# mode -> (exact stdout bytes, exit status). Every mode that answers with a
# result announces readiness first, unless its name says it does not; the
# "unavailable" modes stand for a worker refusing before it was ready.
_RAW_RESULTS = {
    "exit-nonzero": (_announced(_header(b"text") + b"probe text"), NONZERO_EXIT_STATUS),
    "garbage-token": (_announced(_header(b"not-a-result-token")), _SUCCESS_EXIT),
    "undecodable-text": (_announced(_header(b"text") + b"\xff\xfe\xfd"), _SUCCESS_EXIT),
    "no-separator": (_announced(_header(b"text").rstrip(b"\n")), _SUCCESS_EXIT),
    "silent": (b"", _SUCCESS_EXIT),
    "trailing-payload": (_announced(_header(b"encrypted") + b"probe text"), _SUCCESS_EXIT),
    "unavailable": (_header(b"unavailable"), _SUCCESS_EXIT),
    "unavailable-before-any-ceiling": (b"unavailable none none\n", _SUCCESS_EXIT),
    "surrogate": (_announced(_header(b"text") + _SURROGATE_TEXT.encode("utf-8", "surrogatepass")), _SUCCESS_EXIT),
    "text-under-job-object": (_announced(_header(b"text") + b"probe text"), _SUCCESS_EXIT),
    "text-under-rlimit": (_announced(_header(b"text", _RLIMIT) + b"probe text", _RLIMIT), _SUCCESS_EXIT),
    "refusal-under-rlimit": (_announced(_header(b"encrypted", _RLIMIT), _RLIMIT), _SUCCESS_EXIT),
    "text-without-ceiling": (_announced(_header(b"text", b"none") + b"probe text", b"none"), _SUCCESS_EXIT),
    "refusal-without-ceiling": (_announced(_header(b"encrypted", b"none"), b"none"), _SUCCESS_EXIT),
    "text-under-unknown-ceiling": (
        _announced(_header(b"text", b"seccomp-filter") + b"probe text", b"seccomp-filter"),
        _SUCCESS_EXIT,
    ),
    "text-from-old-parser": (
        _announced(_header(b"text", version=_OLD_PARSER) + b"probe text", version=_OLD_PARSER),
        _SUCCESS_EXIT,
    ),
    "refusal-from-old-parser": (
        _announced(_header(b"over-limits", version=_OLD_PARSER), version=_OLD_PARSER),
        _SUCCESS_EXIT,
    ),
    "text-with-unparsable-version": (
        _announced(_header(b"text", version=b"unknown") + b"probe text", version=b"unknown"),
        _SUCCESS_EXIT,
    ),
    "two-field-header": (_announced(b"text windows-job-object\nprobe text"), _SUCCESS_EXIT),
    "four-field-header": (_announced(b"text windows-job-object 999.0.0 extra\nprobe text"), _SUCCESS_EXIT),
    "double-space-header": (_announced(b"text  windows-job-object 999.0.0\nprobe text"), _SUCCESS_EXIT),
    "oversized-header": (_announced(_header(b"text", version=b"9" * 300) + b"probe text"), _SUCCESS_EXIT),
    "verdict-without-ready": (_header(b"text") + b"probe text", _SUCCESS_EXIT),
    "refusal-without-ready": (_header(b"over-limits"), _SUCCESS_EXIT),
    "die-before-ready": (b"", NONZERO_EXIT_STATUS),
    "die-after-ready": (_ready(), NONZERO_EXIT_STATUS),
    "exit-after-ready": (_ready(), _SUCCESS_EXIT),
    "partial-result-then-die": (_ready() + b"over-lim", NONZERO_EXIT_STATUS),
    "partial-result-then-exit-zero": (_ready() + b"over-lim", _SUCCESS_EXIT),
    "ready-then-other-ceiling": (_ready() + _header(b"text", _RLIMIT) + b"probe text", _SUCCESS_EXIT),
    "ready-then-other-version": (_ready() + _header(b"text", version=b"998.0.0") + b"probe text", _SUCCESS_EXIT),
    "ready-then-unavailable": (_announced(_header(b"unavailable")), _SUCCESS_EXIT),
    "ready-twice": (_ready() + _ready(), _SUCCESS_EXIT),
    "untrusted-ready-then-die": (_ready(b"none"), NONZERO_EXIT_STATUS),
    "old-parser-ready-then-die": (_ready(version=_OLD_PARSER), NONZERO_EXIT_STATUS),
}


def _hang(started_marker: str, survived_marker: str, sleep_seconds: str) -> int:
    """Mark the start, sleep without reading stdin, then mark survival if still alive."""
    Path(started_marker).write_text("started", encoding="utf-8")
    time.sleep(float(sleep_seconds))
    Path(survived_marker).write_text("survived", encoding="utf-8")
    return _SUCCESS_EXIT


def _ready_then_hang(started_marker: str, survived_marker: str, sleep_seconds: str) -> int:
    """Announce readiness as the real worker does, then hang without a result like _hang."""
    _write_and_flush(_ready())
    return _hang(started_marker, survived_marker, sleep_seconds)


def _report_environment() -> int:
    """Announce readiness, consume stdin, then return this process's environment, working directory and its contents as JSON text."""
    _write_and_flush(_ready())
    sys.stdin.buffer.read()
    report = {"environment": dict(os.environ), "cwd": os.getcwd(), "cwd_listing": os.listdir(os.getcwd())}
    _write_and_flush(_header(b"text") + json.dumps(report).encode("utf-8"))
    return _SUCCESS_EXIT


def _load_dotenv_then_hold(hold_seconds: str, *dotenv_paths: str) -> int:
    """Load NAME=value lines from each .env file into os.environ, as a .env loader would, then announce readiness and hold.

    A relative path is found from the working directory. Holding after the
    ready line, before the result, keeps the process running while the
    parent reads its environment block from outside.
    """
    for dotenv_path in dotenv_paths:
        for line in Path(dotenv_path).read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator:
                os.environ[name.strip()] = value.strip()
    _write_and_flush(_ready())
    sys.stdin.buffer.read()
    time.sleep(float(hold_seconds))
    _write_and_flush(_header(b"text") + b"loaded")
    return _SUCCESS_EXIT


def _flood() -> int:
    """Announce readiness, consume stdin, write a valid text header, then write output until killed."""
    _write_and_flush(_ready())
    sys.stdin.buffer.read()
    sys.stdout.buffer.write(_header(b"text"))
    while True:
        sys.stdout.buffer.write(_FLOOD_CHUNK)


def _exact_output(total_bytes: str) -> int:
    """Write a valid announced text result exactly total_bytes long, both header lines included."""
    _write_and_flush(_ready())
    sys.stdin.buffer.read()
    header = _header(b"text")
    _write_and_flush(header + b"a" * (int(total_bytes) - len(_ready()) - len(header)))
    return _SUCCESS_EXIT


def _answer_without_reading() -> int:
    """Announce readiness, then refuse the document as encrypted without reading a byte of stdin."""
    _write_and_flush(_announced(_header(b"encrypted")))
    return _SUCCESS_EXIT


def _terminated_before_ready() -> int:
    """Have the operating system end this process abruptly before it writes anything."""
    sys.stdin.buffer.read()
    return _terminate_abruptly()


def _terminated_after_ready() -> int:
    """Announce readiness, consume stdin, then have the operating system end this process abruptly."""
    _write_and_flush(_ready())
    sys.stdin.buffer.read()
    return _terminate_abruptly()


def _terminate_abruptly() -> int:
    """End this process at once with TERMINATED_EXIT_STATUS, as a crash or an enforced ceiling would."""
    if sys.platform == _WINDOWS_PLATFORM:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateProcess(kernel32.GetCurrentProcess(), ACCESS_VIOLATION_EXIT_STATUS)
    else:
        os.kill(os.getpid(), signal.SIGKILL)
    return NONZERO_EXIT_STATUS


def _write_and_flush(output: bytes) -> None:
    """Write bytes to stdout and flush them, so the parent sees them before anything else happens."""
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


def _write_raw_result(mode: str) -> int:
    """Consume stdin, write the mode's exact bytes and return its exit status."""
    output, exit_status = _RAW_RESULTS[mode]
    sys.stdin.buffer.read()
    _write_and_flush(output)
    return exit_status


def _report_ceilings(worker_path: str, limits_json: str, *lower_hard_first: str) -> int:
    """Install the real worker's ceiling on this process and print what is in force and what it stops.

    With a third argument (POSIX only), the address-space hard limit is first
    lowered to that many bytes, so the report shows whether the worker raised
    it again.
    """
    limits = json.loads(limits_json)
    if lower_hard_first:
        _lower_address_space_first(int(lower_hard_first[0]))
    worker = _load_worker(worker_path)
    report = {"ceiling": worker.install_ceiling(limits)}
    report.update(_read_back_ceiling(worker))
    report["past_the_ceiling"] = _try_allocating(limits["memory_limit_bytes"] + _PAST_THE_CEILING_BYTES)
    report["small"] = _try_allocating(_SMALL_ALLOCATION_BYTES)
    print(json.dumps(report))
    return _SUCCESS_EXIT


def _lower_address_space_first(lowered: int) -> None:
    """Lower this process's address-space soft and hard limits before the worker runs."""
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (lowered, lowered))


def _read_back_ceiling(worker: object) -> dict:
    """Return the limits now in force, read from outside the worker's own verification."""
    if sys.platform == _WINDOWS_PLATFORM:
        kernel32 = worker._load_kernel32()
        seen = worker._JobExtendedLimitInformation()
        information_class = worker._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
        queried = kernel32.QueryInformationJobObject(
            None, information_class, ctypes.byref(seen), ctypes.sizeof(seen), None
        )
        return {
            "queried": bool(queried),
            "limit_flags": seen.BasicLimitInformation.LimitFlags,
            "process_memory_limit": seen.ProcessMemoryLimit,
            "per_process_user_time_limit": seen.BasicLimitInformation.PerProcessUserTimeLimit,
        }
    import resource

    return {
        "address_space": list(resource.getrlimit(resource.RLIMIT_AS)),
        "cpu": list(resource.getrlimit(resource.RLIMIT_CPU)),
        "unlimited": resource.RLIM_INFINITY,
    }


def _allocate_until_refused(worker_path: str, limits_json: str, step_bytes: str) -> int:
    """Install the real worker's ceiling on this process, then commit fixed steps of memory until one is refused.

    Prints the ceiling, the step, how many steps succeeded, the bytes they
    reached, and (on Windows) this process's committed memory just before
    the first step, so the test can tell how close to the ceiling the
    refusal came. The steps are freed before the report is written.
    """
    worker = _load_worker(worker_path)
    report = {"ceiling": worker.install_ceiling(json.loads(limits_json)), "step_bytes": int(step_bytes)}
    report["committed_before"] = _own_committed_bytes()
    blocks = []
    try:
        while True:
            blocks.append(bytearray(report["step_bytes"]))
    except MemoryError:
        report["outcome"] = "MemoryError"
    report["steps"] = len(blocks)
    report["bytes_reached"] = len(blocks) * report["step_bytes"]
    blocks.clear()
    print(json.dumps(report))
    return _SUCCESS_EXIT


def _own_committed_bytes() -> int | None:
    """Return this process's committed memory now (psapi PagefileUsage) on Windows, else None."""
    if sys.platform != _WINDOWS_PLATFORM:
        return None
    outside_view = _load_module("outside_view", _OUTSIDE_VIEW_PATH)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    return outside_view.committed_memory(kernel32.GetCurrentProcess()).current


def _try_allocating(size: int) -> str:
    """Return "allocated" if size bytes can be committed now, else "MemoryError"."""
    try:
        block = bytearray(size)
    except MemoryError:
        return "MemoryError"
    del block
    return "allocated"


def _load_worker(worker_path: str) -> object:
    """Import the worker module from its file path, without importing the src package."""
    return _load_module("pdf_text_worker", Path(worker_path))


def _load_module(name: str, path: Path) -> object:
    """Import a module from its file path, without importing the package it sits in."""
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def main(arguments: list[str]) -> int:
    """Run the mode named by the first argument with the rest as its arguments."""
    mode, rest = arguments[0], arguments[1:]
    special_modes = {
        "hang": _hang,
        "ready-then-hang": _ready_then_hang,
        "environment": _report_environment,
        "flood": _flood,
        "exact-output": _exact_output,
        "answer-without-reading": _answer_without_reading,
        "terminated-before-ready": _terminated_before_ready,
        "terminated-after-ready": _terminated_after_ready,
        "ceilings": _report_ceilings,
        "allocate-until-refused": _allocate_until_refused,
        "load-dotenv": _load_dotenv_then_hold,
    }
    if mode in special_modes:
        return special_modes[mode](*rest)
    return _write_raw_result(mode)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
