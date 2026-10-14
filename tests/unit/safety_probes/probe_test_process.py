"""probe_test_process.py — what the test-database boundary did inside a real pytest process.

What:  Reports, as one JSON line on stdout, whether DATABASE_URL survived the
       bootstrap, whether .env loading is still disabled after the
       application has been imported, and how every connection path —
       psycopg2 directly, an empty DSN, SQLAlchemy, psycopg2's pool and the
       application's own pool — responds to a development URL. A second test
       asks for the db_connection fixture, so a run can show whether that
       fixture skipped, failed or stopped, and a third skips itself so a
       --require-live-database run can show that a skipped live test fails
       the run. A fourth uses nothing at all, so a run selecting only it
       shows that both flags and valid settings are not enough to pass a
       --require-runtime-identity run. A fifth, in TestRuntimeProbe, asks
       for runtime_connection (C2) with the owner fixture replaced inside
       that class only, so a run
       with no database can show the runtime fixture's own decision — skipped
       as "runtime identity not configured", failed, or stopped — and how its
       skip is counted. A sixth is marked pilot_profile and uses nothing
       (PILOT-READY-003), so a run can show that --runtime-profile pilot
       selects only it and that every other run deselects it, never skips it.
       Its name shares no keyword with the other probes' -k selections.
Why:   TEST-SAFETY-001 must prove the boundary holds in a genuine pytest
       process, with conftest loading in its real order, not only in unit
       tests of the helper. The development URL used here is fake and points
       at 127.0.0.1 port 1, where nothing listens: if the guard ever failed,
       the attempt would end in OperationalError, never in a real database.
How:   Run only as a child process by tests/unit/test_database_safety_processes.py.
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


@pytest.mark.integration
def test_probe_skips_itself():
    # Selected only by the required-live accounting check, which must turn
    # this skip into a failed run.
    pytest.skip("probe integration test skipping on purpose")


def test_probe_harmless_unit_check():
    # Uses no fixture and touches no database: a run selecting only this
    # cannot have verified anything, so --require-runtime-identity must fail it.
    assert "kerno_test_app".startswith("kerno_test")


@pytest.mark.pilot_profile
def test_probe_pilot_marked_check():
    # Uses no fixture and touches no database; prints a marker so a run can
    # show it actually ran, which only --runtime-profile pilot allows.
    print("KERNO_PILOT_PROBE_RAN", flush=True)


class TestRuntimeProbe:
    """The runtime fixture's own outcome in a real process, with no database behind the owner fixture.

    runtime_connection asks for db_connection first, and offline that fixture
    has nothing to reach, so it is replaced here — for this class only — by a
    stand-in that touches nothing. The real ordering and the owner seeding are
    proved in-process by tests/unit/test_database_safety_runtime.py. Not marked
    by hand: conftest's collection hook must mark it runtime_identity.
    """

    @pytest.fixture
    def db_connection(self):
        """Stand in for the owner fixture without opening any connection."""
        return None

    def test_probe_runtime_fixture_outcome(self, runtime_connection):
        assert runtime_connection is not None
