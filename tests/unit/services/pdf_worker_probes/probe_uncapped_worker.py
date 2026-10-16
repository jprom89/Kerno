"""
probe_uncapped_worker.py — the real PDF worker, with or without its ceiling installation, as the ceiling's counterfactual.

What:  Loads src/services/pdf_text_worker.py from its path and runs its real
       main() on stdin, selected by the first argument. "capped" changes
       nothing. "uncapped" replaces install_ceiling, and only it, with a
       stand-in that installs no ceiling and names the one the real
       function would have installed on this platform. Everything else (the
       ready line, the parser's version, hooks, observer and self-tests, the
       budgets, the result line, and the parent's handling of it) is the
       real code, unchanged.
Why:   That the real ceiling stops a document close to
       PDF_WORKER_MEMORY_LIMIT_BYTES shows the ceiling held only if, without
       it, the same code on the same document would have needed more. The
       two modes differ in that one function alone, so a run of each is that
       comparison.
How:   Started only by tests/unit/services/test_pdf_text_extraction.py as
       [python, "-P", <this file>, <capped|uncapped>, <worker path>, <limits
       JSON>], handed to src/services/pdf_text_extraction.py in place of the
       worker's own command. An "uncapped" run has no memory bound of its
       own: the test watches its committed memory and kills it once it
       passes a threshold above the ceiling. Standard library only. The
       file name does not match python_files, so pytest never collects it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_UNCAPPED = "uncapped"
_WINDOWS_PLATFORM = "win32"


def _load_worker(worker_path: str) -> Any:
    """Import the worker module from its file path, without importing the src package."""
    specification = importlib.util.spec_from_file_location("pdf_text_worker", Path(worker_path))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _without_a_ceiling(worker: Any) -> None:
    """Replace only the worker's install_ceiling, with one that installs nothing and names this platform's ceiling."""

    def install_nothing(limits: dict, platform: str | None = None) -> str:
        """Install no ceiling, and report the one the real install_ceiling would have put in force here."""
        if sys.platform == _WINDOWS_PLATFORM:
            return worker.CEILING_WINDOWS_JOB_OBJECT
        return worker.CEILING_POSIX_RLIMIT

    worker.install_ceiling = install_nothing


def main(arguments: list[str]) -> int:
    """Run the real worker's main() on stdin, without its ceiling when the mode is "uncapped"."""
    mode, worker_path, limits_json = arguments
    worker = _load_worker(worker_path)
    if mode == _UNCAPPED:
        _without_a_ceiling(worker)
    sys.argv = [worker_path, limits_json]
    return worker.main()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
