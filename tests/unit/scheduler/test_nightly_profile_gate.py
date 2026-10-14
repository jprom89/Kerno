"""Offline tests for the nightly bias recalculation job's serving-profile gate (PILOT-READY-003).

What:  Checks that serving_profile_refusal refuses the pilot profile, an
       unrecognised profile and a missing profile outside development without
       echoing the configured value; that main() exits with
       SCHEDULED_JOB_REFUSED_EXIT_CODE and logs the reason before it opens any
       connection, enters the transaction factory or starts the batch; that
       main() still runs the batch for the full profile and for a blank profile
       in development; and that the real `python -m` entry path refuses the
       pilot in a child process.
Why:   The pilot excludes this job (docs/pilot_readiness_kickoff.md §4.3,
       issue #20). Removing its HTTP trigger is not enough: the cron
       entrypoint is a separate process and must refuse on its own. Every
       database path here is a recorder that fails the test if reached;
       nothing connects to a database, and .env loading is disabled by the
       test bootstrap and additionally stubbed.
How:   pytest tests/unit/scheduler/test_nightly_profile_gate.py -v
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys

import dotenv
import pytest

from config.constants import SCHEDULED_JOB_REFUSED_EXIT_CODE
from src.api.serving_profile import SERVING_PROFILE_VARIABLE
from src.scheduler import nightly_bias_recalculation as nightly

_JOB_LOGGER = "src.scheduler.nightly_bias_recalculation"
_REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[3]
_JOB_MODULE = "src.scheduler.nightly_bias_recalculation"
_CHILD_TIMEOUT_SECONDS = 60

# A distinctive unrecognised value: a refusal must never repeat it.
_UNRECOGNISED = "staging-zz-profile"

_PILOT_REASON = "excluded from the pilot serving profile"
_MISSING_REASON = "must be set to 'full' or 'pilot' outside development"
_UNRECOGNISED_REASON = "the configured value is not recognised"

# Variables a child process must not inherit: any database URL or test-target
# setting, so even a broken gate has nothing to connect to.
_CHILD_EXCLUDED_VARIABLES = (
    "DATABASE_URL",
    "KERNO_MIGRATION_DATABASE_URL",
    "KERNO_TEST_DATABASE_URL",
    "KERNO_TEST_DATABASE_APPROVAL",
    "KERNO_TEST_RUNTIME_DATABASE_URL",
    "KERNO_TEST_RUNTIME_DATABASE_APPROVAL",
    "KERNO_ENV",
    SERVING_PROFILE_VARIABLE,
)

REFUSED_ENVIRONMENTS = [
    pytest.param({SERVING_PROFILE_VARIABLE: "pilot"}, _PILOT_REASON, id="pilot"),
    pytest.param({SERVING_PROFILE_VARIABLE: " pilot "}, _PILOT_REASON, id="padded_pilot"),
    pytest.param({SERVING_PROFILE_VARIABLE: "pilot", "KERNO_ENV": "development"}, _PILOT_REASON, id="pilot_in_development"),
    pytest.param({SERVING_PROFILE_VARIABLE: _UNRECOGNISED}, _UNRECOGNISED_REASON, id="unrecognised"),
    pytest.param({SERVING_PROFILE_VARIABLE: "Pilot"}, _UNRECOGNISED_REASON, id="wrong_case"),
    pytest.param({SERVING_PROFILE_VARIABLE: "full,pilot"}, _UNRECOGNISED_REASON, id="list"),
    pytest.param(
        {SERVING_PROFILE_VARIABLE: _UNRECOGNISED, "KERNO_ENV": "development"},
        _UNRECOGNISED_REASON,
        id="unrecognised_in_development",
    ),
    pytest.param({}, _MISSING_REASON, id="unset_outside_development"),
    pytest.param({SERVING_PROFILE_VARIABLE: "   "}, _MISSING_REASON, id="blank_outside_development"),
    pytest.param({"KERNO_ENV": "production"}, _MISSING_REASON, id="unset_in_production"),
]

PERMITTED_ENVIRONMENTS = [
    pytest.param({SERVING_PROFILE_VARIABLE: "full"}, id="full"),
    pytest.param({SERVING_PROFILE_VARIABLE: " full "}, id="padded_full"),
    pytest.param({SERVING_PROFILE_VARIABLE: "full", "KERNO_ENV": "development"}, id="full_in_development"),
    pytest.param({"KERNO_ENV": "development"}, id="unset_in_development"),
    pytest.param({SERVING_PROFILE_VARIABLE: "", "KERNO_ENV": "development"}, id="blank_in_development"),
]


class _Recorder:
    """Records each call by name; a guarded call fails the test and a permitted one returns its scripted value."""

    def __init__(self) -> None:
        """Start with no calls."""
        self.calls: list[tuple[str, tuple, dict]] = []

    def guarded(self, name: str):
        """A stand-in that records the call and raises: reaching it means work started that must not."""
        def guard(*args, **kwargs):
            """Record, then refuse."""
            self.calls.append((name, args, kwargs))
            raise AssertionError(f"the refused job reached {name}")

        return guard

    def returning(self, name: str, value):
        """A stand-in that records the call and returns value."""
        def stand_in(*args, **kwargs):
            """Record, then return the scripted value."""
            self.calls.append((name, args, kwargs))
            return value

        return stand_in

    def names(self) -> list[str]:
        """The names of the calls made, in order."""
        return [name for name, _, _ in self.calls]


@pytest.fixture
def job(monkeypatch):
    """Isolate the profile variables and replace every database path of the job with a recorder."""
    for name in (SERVING_PROFILE_VARIABLE, "KERNO_ENV"):
        monkeypatch.delenv(name, raising=False)
    recorder = _Recorder()
    monkeypatch.setattr(dotenv, "load_dotenv", recorder.returning("load_dotenv", False))
    monkeypatch.setattr(logging, "basicConfig", recorder.returning("basicConfig", None))
    monkeypatch.setattr(nightly.psycopg2, "connect", recorder.guarded("psycopg2.connect"))
    monkeypatch.setattr(nightly, "_cron_transaction", recorder.guarded("_cron_transaction"))
    monkeypatch.setattr(nightly, "run_nightly_bias_recalculation", recorder.guarded("run_nightly_bias_recalculation"))
    return monkeypatch, recorder


def _set(monkeypatch, environment: dict) -> None:
    """Apply one scripted environment on top of the isolated one."""
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def _refusals(caplog) -> list[str]:
    """The error messages the job logged."""
    return [record.getMessage() for record in caplog.records if record.name == _JOB_LOGGER and record.levelno == logging.ERROR]


# ── serving_profile_refusal ─────────────────────────────────────────────────


@pytest.mark.parametrize("environment, reason", REFUSED_ENVIRONMENTS)
def test_the_refusal_names_why_the_job_may_not_run(environment, reason):
    refusal = nightly.serving_profile_refusal(environment)
    assert refusal is not None and reason in refusal


@pytest.mark.parametrize("value", [_UNRECOGNISED, "Pilot", "FULL", "full,pilot", "1"])
def test_the_refusal_never_repeats_an_unrecognised_value(value):
    refusal = nightly.serving_profile_refusal({SERVING_PROFILE_VARIABLE: value})
    assert repr(value) not in refusal
    assert _UNRECOGNISED not in refusal


@pytest.mark.parametrize("environment", PERMITTED_ENVIRONMENTS)
def test_the_full_profile_and_a_blank_development_profile_are_not_refused(environment):
    assert nightly.serving_profile_refusal(environment) is None


def test_the_refusal_reads_only_the_environment_it_is_given(monkeypatch):
    monkeypatch.setenv(SERVING_PROFILE_VARIABLE, "full")
    assert _PILOT_REASON in nightly.serving_profile_refusal({SERVING_PROFILE_VARIABLE: "pilot"})


# ── main(): refusal before any database work ────────────────────────────────


def test_the_refused_exit_status_is_neither_success_nor_an_ordinary_crash():
    assert SCHEDULED_JOB_REFUSED_EXIT_CODE not in (0, 1)


@pytest.mark.parametrize("environment, reason", REFUSED_ENVIRONMENTS)
def test_main_refuses_before_any_database_work(job, caplog, environment, reason):
    monkeypatch, recorder = job
    _set(monkeypatch, environment)
    caplog.set_level(logging.ERROR, logger=_JOB_LOGGER)
    with pytest.raises(SystemExit) as exited:
        nightly.main()
    assert exited.value.code == SCHEDULED_JOB_REFUSED_EXIT_CODE
    assert recorder.names() == ["load_dotenv", "basicConfig"]
    refusals = _refusals(caplog)
    assert len(refusals) == 1
    assert "refused before any database work" in refusals[0] and reason in refusals[0]
    assert _UNRECOGNISED not in caplog.text


def test_main_reads_the_profile_after_loading_dotenv(job):
    monkeypatch, recorder = job

    def load_pilot_profile(*args, **kwargs):
        """Stand in for a .env that sets the pilot profile."""
        recorder.calls.append(("load_dotenv", args, kwargs))
        os.environ[SERVING_PROFILE_VARIABLE] = "pilot"
        return True

    monkeypatch.setenv(SERVING_PROFILE_VARIABLE, "full")
    monkeypatch.setattr(dotenv, "load_dotenv", load_pilot_profile)
    with pytest.raises(SystemExit) as exited:
        nightly.main()
    assert exited.value.code == SCHEDULED_JOB_REFUSED_EXIT_CODE
    assert "run_nightly_bias_recalculation" not in recorder.names()


# ── main(): the full profile still runs the batch ───────────────────────────


@pytest.mark.parametrize("environment", PERMITTED_ENVIRONMENTS)
def test_main_runs_the_batch_for_the_full_profile(job, caplog, environment):
    monkeypatch, recorder = job
    _set(monkeypatch, environment)
    summary = {"success_count": 0, "failure_count": 0}
    monkeypatch.setattr(
        nightly, "run_nightly_bias_recalculation", recorder.returning("run_nightly_bias_recalculation", summary)
    )
    caplog.set_level(logging.INFO, logger=_JOB_LOGGER)
    nightly.main()
    assert recorder.names() == ["load_dotenv", "basicConfig", "run_nightly_bias_recalculation"]
    _, (factory, admin_session), _ = recorder.calls[-1]
    assert factory is nightly._cron_transaction
    assert admin_session.resolve_tenant_id() == nightly.PLATFORM_SCHEDULER_TENANT_ID
    assert _refusals(caplog) == []
    assert "entrypoint finished" in caplog.text


# ── The test process cannot load the real .env ─────────────────────────────


def test_the_test_bootstrap_disables_dotenv_loading():
    assert os.environ.get("PYTHON_DOTENV_DISABLED") == "1"


# ── The real entry path: python -m src.scheduler.nightly_bias_recalculation ──


def _child_environment(profile: str) -> dict[str, str]:
    """The child's environment: no database settings at all, .env loading disabled, and the given profile."""
    environment = {name: value for name, value in os.environ.items() if name not in _CHILD_EXCLUDED_VARIABLES}
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    environment[SERVING_PROFILE_VARIABLE] = profile
    return environment


@pytest.mark.parametrize("profile", ["pilot", _UNRECOGNISED])
def test_the_cron_entry_path_refuses_in_a_child_process(profile):
    completed = subprocess.run(
        [sys.executable, "-m", _JOB_MODULE],
        cwd=_REPOSITORY_ROOT,
        env=_child_environment(profile),
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == SCHEDULED_JOB_REFUSED_EXIT_CODE, completed.stderr
    assert "refused before any database work" in completed.stderr
    assert "Nightly bias recalculation starting" not in completed.stderr
    assert _UNRECOGNISED not in completed.stderr
