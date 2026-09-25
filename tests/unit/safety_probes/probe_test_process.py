"""probe_test_process.py — what the test-database boundary did inside a real pytest process.

What:  Reports, as one JSON line on stdout, whether DATABASE_URL survived the
       bootstrap, whether .env loading is still disabled after the
       application has been imported, and how every connection path —
       psycopg2 directly, an empty DSN, SQLAlchemy, psycopg2's pool and the
       application's own pool — responds to a development URL. A second test
       asks for the db_connection fixture, so a run can show whether that
       fixture skipped, failed or stopped.
Why:   TEST-SAFETY-001 must prove the boundary holds in a genuine pytest
       process, with conftest loading in its real order, not only in unit
       tests of the helper. The development URL used here is fake and points
       at 127.0.0.1 port 1, where nothing listens: if the guard ever failed,
       the attempt would end in OperationalError, never in a real database.
How:   Run only as a child process by tests/unit/test_database_safety.py.
       The file name does not match python_files, so an ordinary pytest run
       never collects it.
"""

from __future__ import annotations

import io
import json
import os

import pytest

_FAKE_DEVELOPMENT_URL = os.environ.get(
    "KERNO_PROBE_DEVELOPMENT_URL", "postgresql://kerno_dev:probe-dev-secret@127.0.0.1:1/kerno_dev"
)
_PROBE_VARIABLE = "KERNO_PROBE_FROM_DOTENV"


def _outcome(attempt) -> str:
    """Run one connection attempt and name what happened: the exception class, or CONNECTED."""
    try:
        attempt()
    except Exception as exc:  # noqa: BLE001 — the class name is the observation
        return type(exc).__name__
    return "CONNECTED"


def _connection_outcomes() -> dict[str, str]:
    """Try every connection path against the fake development URL."""
    import psycopg2
    import psycopg2.pool
    from sqlalchemy import create_engine

    from src.api import dependencies

    return {
        "psycopg2_connect": _outcome(lambda: psycopg2.connect(_FAKE_DEVELOPMENT_URL)),
        "psycopg2_empty_dsn": _outcome(lambda: psycopg2.connect("")),
        "psycopg2_no_arguments": _outcome(lambda: psycopg2.connect()),
        "sqlalchemy_engine": _outcome(lambda: create_engine(_FAKE_DEVELOPMENT_URL).connect()),
        "psycopg2_pool": _outcome(lambda: psycopg2.pool.ThreadedConnectionPool(1, 1, dsn=_FAKE_DEVELOPMENT_URL)),
        "application_pool": _outcome(dependencies._get_pool),
    }


def test_probe_reports_the_boundary():
    import dotenv

    import src.api.app  # noqa: F401 — its import-time load_dotenv() is part of what is probed

    loaded = dotenv.load_dotenv(stream=io.StringIO(f"{_PROBE_VARIABLE}=1"))
    report = {
        "database_url_present": "DATABASE_URL" in os.environ,
        "dotenv_loaded": bool(loaded) or _PROBE_VARIABLE in os.environ,
        "outcomes": _connection_outcomes(),
    }
    print("KERNO_PROBE_REPORT=" + json.dumps(report), flush=True)


@pytest.mark.integration
def test_probe_needs_the_database(db_connection):
    assert db_connection is not None
