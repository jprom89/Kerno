"""
probe_real_worker.py — the real PDF worker, run in its own process to prove what only a real process can.

What:  Loads src/services/pdf_text_worker.py from its path and either runs its
       real main() or installs its real ceiling, selected by the first
       argument. "planted" <site> <live|deafened> <worker path> <limits JSON>
       runs the real worker on stdin under its real ceiling with one
       swallow_sites failure planted right after the worker imports pypdf
       (so after the ceiling is installed and verified, just when the worker
       itself first touches pypdf); "deafened" also switches the worker's
       observer off once the ready line is written, the control run showing
       that pypdf swallows the failure and nothing else in the worker
       notices. "without-monitoring" <worker path> <limits JSON> removes
       sys.monitoring, as an interpreter older than 3.12 lacks it, and runs
       the real worker's main(). "job-children" <worker path> <limits JSON>
       starts a child process, installs the real ceiling on this process,
       reads the Job Object back, tries to start a child again, and prints
       a JSON report.
Why:   That a failure pypdf swallows reaches the API as a refusal, through
       the real ready line, result line and parent; that a worker without
       sys.monitoring answers "unavailable" instead of parsing or crashing;
       and that a worker under its Job Object cannot start another program
       (so pypdf's jbig2dec, or anything else, never runs outside the
       2-slot admission cap): each can only be shown in a real child process.
How:   Started only by tests/unit/services/test_pdf_text_worker.py as
       [python, "-P", <this file>, <mode>, *arguments], with the command
       for "planted" handed to src/services/pdf_text_extraction.py in place
       of the worker's own. Standard library only: the worker and
       swallow_sites are loaded from their paths. The file name does not
       match python_files, so pytest never collects it.
"""

from __future__ import annotations

import ctypes
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_SWALLOW_SITES_PATH = Path(__file__).resolve().with_name("swallow_sites.py")
_DEAFENED = "deafened"
_SUCCESS_EXIT = 0
_CHILD_TIMEOUT_SECONDS = 60
_CHILD_STARTED = "started"
_CHILD_REFUSED = "refused"
_PASS_PROGRAM = "pass"


def _load_module(name: str, path: Path) -> Any:
    """Import a module from its file path, without importing the package it sits in."""
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _run_planted(site: str, observer_mode: str, worker_path: str, limits_json: str) -> int:
    """Run the real worker's main() with the site's failure planted once pypdf is imported."""
    worker = _load_module("pdf_text_worker", Path(worker_path))
    sites = _load_module("swallow_sites", _SWALLOW_SITES_PATH)
    real_import = worker.import_parser

    def import_then_plant() -> Any:
        """Import pypdf as the worker does, then plant the site's failure in it."""
        pypdf = real_import()
        sites.plant(site, setattr)
        return pypdf

    worker.import_parser = import_then_plant
    if observer_mode == _DEAFENED:
        _deafen_the_observer_after_ready(worker)
    sys.argv = [worker_path, limits_json]
    return worker.main()


def _run_without_monitoring(worker_path: str, limits_json: str) -> int:
    """Remove sys.monitoring, as on Python before 3.12, then run the real worker's main()."""
    del sys.monitoring
    worker = _load_module("pdf_text_worker", Path(worker_path))
    sys.argv = [worker_path, limits_json]
    return worker.main()


def _deafen_the_observer_after_ready(worker: Any) -> None:
    """Make the worker switch its observer's RAISE events off straight after it writes the ready line."""
    real_announce = worker.announce_ready

    def announce_then_deafen(result_channel: Any, ceiling: str, parser_version: str) -> None:
        """Announce as the worker does, then stop sys.monitoring delivering events to its observer."""
        real_announce(result_channel, ceiling, parser_version)
        monitoring = sys.monitoring
        for tool_id in range(worker._MONITORING_TOOL_ID_COUNT):
            if monitoring.get_tool(tool_id) == worker.OBSERVER_TOOL_NAME:
                monitoring.set_events(tool_id, monitoring.events.NO_EVENTS)

    worker.announce_ready = announce_then_deafen


def _report_job_children(worker_path: str, limits_json: str) -> int:
    """Start a child, install the worker's real ceiling, read the job back, try a child again, and print it all."""
    worker = _load_module("pdf_text_worker", Path(worker_path))
    report = {"before_the_ceiling": _try_starting_a_child()}
    report["ceiling"] = worker.install_ceiling(json.loads(limits_json))
    report.update(_read_back_job(worker))
    report["under_the_ceiling"] = _try_starting_a_child()
    print(json.dumps(report))
    return _SUCCESS_EXIT


def _try_starting_a_child() -> dict:
    """Try to run a child Python that does nothing; report whether it started or the system refused it."""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PASS_PROGRAM], capture_output=True, timeout=_CHILD_TIMEOUT_SECONDS
        )
    except OSError as exc:
        return {"outcome": _CHILD_REFUSED, "winerror": getattr(exc, "winerror", None), "errno": exc.errno}
    return {"outcome": _CHILD_STARTED, "returncode": completed.returncode}


def _read_back_job(worker: Any) -> dict:
    """Return the Job Object limits now in force on this process, read from outside the worker's own check."""
    kernel32 = worker._load_kernel32()
    seen = worker._JobExtendedLimitInformation()
    queried = kernel32.QueryInformationJobObject(
        None, worker._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(seen), ctypes.sizeof(seen), None
    )
    basic = seen.BasicLimitInformation
    return {
        "queried": bool(queried),
        "limit_flags": basic.LimitFlags,
        "active_process_limit": basic.ActiveProcessLimit,
        "process_memory_limit": seen.ProcessMemoryLimit,
    }


def main(arguments: list[str]) -> int:
    """Run the mode named by the first argument with the rest as its arguments."""
    mode, rest = arguments[0], arguments[1:]
    modes = {
        "planted": _run_planted,
        "without-monitoring": _run_without_monitoring,
        "job-children": _report_job_children,
    }
    return modes[mode](*rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
