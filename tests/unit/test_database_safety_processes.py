"""Offline proof of the TEST-SAFETY-001 boundary in real child processes, and of the migration wrapper.

What:  Runs genuine pytest processes (conftest loading in its real order) and
       genuine runs of scripts/migrate_test_database.py, and checks what they
       did: with no settings nothing can connect; an inherited DATABASE_URL is
       ignored; an explicit live request fails; invalid or redirecting
       settings fail the run before any test; an unreachable approved target
       stops the run instead of skipping; parallel workers are refused; a
       skipped live test fails a required live run; and no child ever prints
       the password. Also checks the wrapper's decisions, its lock re-proofs
       and its release on every path, including its printed output. For the
       C2 runtime identity: --require-runtime-identity needs
       --require-live-database and configured runtime settings; runtime
       settings naming another role stop the run before any test; without
       runtime settings a runtime test is skipped by name and the run passes;
       an inherited KERNO_MIGRATION_DATABASE_URL naming anything but the owner
       target stops pytest and the wrapper before any connection; and the
       wrapper never reads the runtime settings.
Why:   A boundary that is only unit-tested could still be bypassed by the
       real startup order. Every URL a child sees is fake and points at
       127.0.0.1 port 1, where nothing listens, and children never inherit a
       real .env.test or .env.test.runtime (KERNO_TEST_ENV_FILE and
       KERNO_TEST_RUNTIME_ENV_FILE point at missing files) or any database,
       runtime or migration URL from the parent.
How:   pytest tests/unit/test_database_safety_processes.py -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

from tests import _database_safety as safety
from tests.unit.test_database_safety import FAKE_DEVELOPMENT_URL, GOOD_URL, SECRET, make_target

_PROBE = pathlib.Path(__file__).parent / "safety_probes" / "probe_test_process.py"
_WRAPPER = safety.REPOSITORY_ROOT / "scripts" / "migrate_test_database.py"
_CHILD_TIMEOUT_SECONDS = 180
_UNREACHABLE_URL = f"postgresql://kerno_test:{SECRET}@127.0.0.1:1/kerno_test"
_UNREACHABLE_APPROVAL = "kerno_test@127.0.0.1:1/kerno_test"
_UNREACHABLE_SETTINGS = {
    "KERNO_TEST_DATABASE_URL": _UNREACHABLE_URL,
    "KERNO_TEST_DATABASE_APPROVAL": _UNREACHABLE_APPROVAL,
}


_STRIPPED_FROM_CHILDREN = frozenset({
    "DATABASE_URL", "PYTHON_DOTENV_DISABLED", "PYTEST_ADDOPTS",
    safety.TEST_DATABASE_URL_VARIABLE, safety.TEST_DATABASE_APPROVAL_VARIABLE,
    safety.TEST_RUNTIME_DATABASE_URL_VARIABLE, safety.TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE,
    # A live parent pins this to its own owner URL; a child must start clean.
    safety.MIGRATION_DATABASE_URL_VARIABLE,
    *safety.TARGET_CHANGING_LIBPQ_VARIABLES,
})
_RUNTIME_PROBE = "runtime_fixture_outcome"
_UNREACHABLE_RUNTIME_URL = f"postgresql://kerno_test_app:{SECRET}@127.0.0.1:1/kerno_test"


def _child_environment(tmp_path, overrides: dict | None) -> dict:
    """The parent's environment without any database or runtime settings, plus the given overrides.

    Both settings files point at missing paths, so a child never reads a real
    .env.test or .env.test.runtime.
    """
    child = {name: value for name, value in os.environ.items() if name not in _STRIPPED_FROM_CHILDREN}
    child[safety.TEST_ENV_FILE_VARIABLE] = str(tmp_path / "absent.env.test")
    child[safety.TEST_RUNTIME_ENV_FILE_VARIABLE] = str(tmp_path / "absent.env.test.runtime")
    child["KERNO_PROBE_DEVELOPMENT_URL"] = FAKE_DEVELOPMENT_URL
    child.update(overrides or {})
    return child


def _run(command: list[str], tmp_path, overrides: dict | None = None) -> subprocess.CompletedProcess:
    """Run one child process from the repository root with a controlled environment."""
    return subprocess.run(
        command, cwd=safety.REPOSITORY_ROOT, env=_child_environment(tmp_path, overrides),
        capture_output=True, text=True, timeout=_CHILD_TIMEOUT_SECONDS,
    )


def _pytest_probe(tmp_path, overrides: dict | None, selection: str, *extra: str) -> subprocess.CompletedProcess:
    """Run the probe file in a fresh pytest process, selecting tests by keyword."""
    command = [sys.executable, "-m", "pytest", str(_PROBE), "-s", "-rs", "-p", "no:cacheprovider", "-k", selection, *extra]
    return _run(command, tmp_path, overrides)


def _output(completed: subprocess.CompletedProcess) -> str:
    """Everything the child printed."""
    return completed.stdout + completed.stderr


def _report(completed: subprocess.CompletedProcess) -> dict:
    """Return the probe's JSON report from the child's output."""
    for line in completed.stdout.splitlines():
        if line.startswith("KERNO_PROBE_REPORT="):
            return json.loads(line.split("=", 1)[1])
    raise AssertionError(f"no probe report; exit {completed.returncode}:\n{completed.stdout[-2000:]}")


def _skip_reported(output: str) -> bool:
    """True when pytest reported a skipped test: an -rs line, a skip reason, or a count in the stats line.

    Not a bare search for "skipped": the terminal summary's runtime-identity
    line says "runtime-identity tests were skipped" whenever no runtime
    identity is configured, even in a run that selected no runtime test.
    """
    lines = output.splitlines()
    return (
        any(line.startswith("SKIPPED") for line in lines)
        or "live database test skipped" in output
        or re.search(r"\b\d+ skipped\b", output) is not None
    )


def _wrapper(tmp_path, overrides: dict | None, *arguments: str) -> subprocess.CompletedProcess:
    """Run scripts/migrate_test_database.py as a real child process."""
    return _run([sys.executable, str(_WRAPPER), *arguments], tmp_path, overrides)


def test_children_inherit_no_database_runtime_or_migration_settings(tmp_path, monkeypatch):
    for name in _STRIPPED_FROM_CHILDREN:
        monkeypatch.setenv(name, "1")
    child = _child_environment(tmp_path, None)
    assert sorted(_STRIPPED_FROM_CHILDREN & set(child)) == []
    assert not pathlib.Path(child[safety.TEST_RUNTIME_ENV_FILE_VARIABLE]).exists()
    assert not pathlib.Path(child[safety.TEST_ENV_FILE_VARIABLE]).exists()


# ── Real pytest processes ───────────────────────────────────────────────────


@pytest.mark.slow
@pytest.mark.parametrize("inherited", [{}, {"DATABASE_URL": FAKE_DEVELOPMENT_URL}])
def test_with_no_test_settings_no_connection_path_can_open_a_database(tmp_path, inherited):
    completed = _pytest_probe(tmp_path, inherited, "reports or needs")
    assert completed.returncode == 0, _output(completed)[-2000:]
    report = _report(completed)
    assert report["database_url_present"] is False and report["dotenv_loaded"] is False
    outcomes = report["outcomes"]
    assert outcomes.pop("application_pool") == "RuntimeError"
    assert set(outcomes.values()) == {"DatabaseTargetNotConfigured"}
    assert "1 passed, 1 skipped" in completed.stdout
    assert "live database test skipped: no approved test database is configured" in completed.stdout
    assert SECRET not in _output(completed)


@pytest.mark.slow
def test_requiring_live_validation_without_settings_fails_clearly(tmp_path):
    completed = _pytest_probe(tmp_path, {"DATABASE_URL": FAKE_DEVELOPMENT_URL}, "reports", "--require-live-database")
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert "--require-live-database was given" in _output(completed)
    assert "KERNO_PROBE_REPORT" not in completed.stdout and SECRET not in _output(completed)


@pytest.mark.slow
@pytest.mark.parametrize(
    "url, approval, expected",
    [
        (FAKE_DEVELOPMENT_URL, "kerno_dev@127.0.0.1:1/kerno_dev", "must be exactly 'kerno_test'"),
        ("", _UNREACHABLE_APPROVAL, "non-empty"),
        ("", "", "non-empty"),
        ("::: not a url :::", _UNREACHABLE_APPROVAL, "not a parseable"),
        (_UNREACHABLE_URL + "?dbname=kerno_dev", _UNREACHABLE_APPROVAL, "selects a different database"),
    ],
)
def test_invalid_test_settings_fail_the_run_before_any_test_or_connection(tmp_path, url, approval, expected):
    overrides = {"KERNO_TEST_DATABASE_URL": url, "KERNO_TEST_DATABASE_APPROVAL": approval}
    completed = _pytest_probe(tmp_path, overrides, "reports")
    output = _output(completed)
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output[-2000:]
    assert "test database configuration refused" in output and expected in output
    assert "KERNO_PROBE_REPORT" not in completed.stdout and SECRET not in output


@pytest.mark.slow
def test_a_redirecting_libpq_variable_fails_the_run(tmp_path):
    completed = _pytest_probe(tmp_path, {**_UNREACHABLE_SETTINGS, "PGHOSTADDR": "10.0.0.5"}, "reports")
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert "PGHOSTADDR" in _output(completed)


@pytest.mark.slow
def test_an_unreachable_approved_target_stops_the_run_instead_of_skipping(tmp_path):
    completed = _pytest_probe(tmp_path, _UNREACHABLE_SETTINGS, "reports or needs")
    output = _output(completed)
    report = _report(completed)
    assert report["database_url_present"] is True
    assert report["outcomes"]["psycopg2_connect"] == "DatabaseTargetRejected"
    assert report["outcomes"]["application_pool"] == "DatabaseTargetRejected"
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output[-2000:]
    assert "test database refused" in output and not _skip_reported(output)
    assert SECRET not in output


@pytest.mark.slow
def test_parallel_workers_are_refused_against_a_configured_database(tmp_path):
    completed = _pytest_probe(tmp_path, {**_UNREACHABLE_SETTINGS, "PYTEST_XDIST_WORKER": "gw0"}, "reports")
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert "parallel test workers are not supported" in _output(completed)


@pytest.mark.slow
def test_a_required_live_run_fails_when_a_live_test_skips(tmp_path):
    completed = _pytest_probe(tmp_path, _UNREACHABLE_SETTINGS, "skips_itself", "--require-live-database")
    assert completed.returncode == pytest.ExitCode.TESTS_FAILED, _output(completed)[-2000:]
    assert "FAILED: 1 integration test(s) skipped in a required live run" in completed.stdout


# ── The runtime identity (C2) in real pytest processes ──────────────────────


@pytest.mark.slow
def test_the_runtime_flag_without_the_live_flag_is_a_usage_error(tmp_path):
    completed = _pytest_probe(tmp_path, {}, "reports", "--require-runtime-identity")
    output = _output(completed)
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output[-2000:]
    assert "--require-runtime-identity must be combined with --require-live-database" in output
    assert "KERNO_PROBE_REPORT" not in completed.stdout


@pytest.mark.slow
def test_requiring_the_runtime_identity_without_runtime_settings_fails_naming_the_variables(tmp_path):
    completed = _pytest_probe(
        tmp_path, _UNREACHABLE_SETTINGS, "reports", "--require-live-database", "--require-runtime-identity"
    )
    output = _output(completed)
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output[-2000:]
    assert "--require-runtime-identity was given but no runtime identity is configured" in output
    assert safety.TEST_RUNTIME_DATABASE_URL_VARIABLE in output
    assert safety.TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE in output
    assert "KERNO_PROBE_REPORT" not in completed.stdout and SECRET not in output


@pytest.mark.slow
@pytest.mark.parametrize(
    "url, approval",
    [
        (_UNREACHABLE_URL, _UNREACHABLE_APPROVAL),
        (f"postgresql://kerno_dev:{SECRET}@127.0.0.1:1/kerno_test", "kerno_dev@127.0.0.1:1/kerno_test"),
    ],
    ids=["the owner role", "kerno_dev"],
)
def test_runtime_settings_naming_another_role_fail_the_run_before_any_test(tmp_path, url, approval):
    overrides = {
        **_UNREACHABLE_SETTINGS,
        safety.TEST_RUNTIME_DATABASE_URL_VARIABLE: url,
        safety.TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE: approval,
    }
    completed = _pytest_probe(tmp_path, overrides, "reports")
    output = _output(completed)
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output[-2000:]
    assert "test database configuration refused" in output
    assert "the runtime role must be exactly 'kerno_test_app'" in output
    assert "KERNO_PROBE_REPORT" not in completed.stdout and SECRET not in output


@pytest.mark.slow
def test_both_flags_and_valid_settings_cannot_pass_a_run_that_verified_nothing(tmp_path):
    # Every setting is syntactically valid, so configuration passes; the one
    # selected test uses no fixture, so no session opens and nothing is
    # verified. Nothing listens on port 1, and nothing tries to connect.
    overrides = {
        **_UNREACHABLE_SETTINGS,
        safety.TEST_RUNTIME_DATABASE_URL_VARIABLE: _UNREACHABLE_RUNTIME_URL,
        safety.TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE: "kerno_test_app@127.0.0.1:1/kerno_test",
    }
    completed = _pytest_probe(
        tmp_path, overrides, "harmless", "--require-live-database", "--require-runtime-identity"
    )
    output = _output(completed)
    assert completed.returncode == pytest.ExitCode.TESTS_FAILED, output[-2000:]
    assert "1 passed" in completed.stdout and not _skip_reported(output)
    assert "FAILED: required runtime-identity run incomplete: the owner's exclusive test-database session" in output
    assert "FAILED: required runtime-identity run incomplete: the runtime identity was never verified live" in output
    assert "runtime identity: kerno_test_app@127.0.0.1:1/kerno_test configured, not used in this run" in output
    assert SECRET not in output


@pytest.mark.slow
@pytest.mark.parametrize(
    "owner, flags",
    [({}, ()), (_UNREACHABLE_SETTINGS, ("--require-live-database",))],
    ids=["ordinary run", "setup-stage owner regression run"],
)
def test_without_runtime_settings_a_runtime_test_skips_by_name_and_the_run_passes(tmp_path, owner, flags):
    # -m proves the collection hook marked the probe: unmarked, nothing is selected.
    completed = _pytest_probe(tmp_path, owner, _RUNTIME_PROBE, "-m", "runtime_identity", *flags)
    output = _output(completed)
    assert completed.returncode == pytest.ExitCode.OK, output[-2000:]
    assert "1 skipped" in completed.stdout and "FAILED" not in completed.stdout
    skip_lines = [line for line in completed.stdout.splitlines() if line.startswith("SKIPPED")]
    assert len(skip_lines) == 1 and skip_lines[0].endswith("runtime identity not configured")
    assert "runtime identity: not configured" in completed.stdout and SECRET not in output


# ── KERNO_MIGRATION_DATABASE_URL, and the wrapper never reading runtime settings ─


@pytest.mark.slow
@pytest.mark.parametrize(
    "inherited", [FAKE_DEVELOPMENT_URL, _UNREACHABLE_RUNTIME_URL], ids=["development database", "runtime user"]
)
def test_an_inherited_conflicting_migration_url_stops_pytest_and_the_wrapper_before_any_connection(tmp_path, inherited):
    overrides = {**_UNREACHABLE_SETTINGS, safety.MIGRATION_DATABASE_URL_VARIABLE: inherited}
    tested = _pytest_probe(tmp_path, overrides, "reports")
    assert tested.returncode == pytest.ExitCode.USAGE_ERROR, _output(tested)[-2000:]
    assert "test database configuration refused: unset KERNO_MIGRATION_DATABASE_URL" in _output(tested)
    assert "KERNO_PROBE_REPORT" not in tested.stdout
    migrated = _wrapper(tmp_path, overrides, "current")
    assert migrated.returncode == 2, _output(migrated)[-2000:]
    # No "test database:" line: the wrapper refused before opening its guard session.
    assert "refused: unset KERNO_MIGRATION_DATABASE_URL" in migrated.stdout and "test database:" not in migrated.stdout
    assert SECRET not in _output(tested) + _output(migrated)


@pytest.mark.slow
def test_a_matching_inherited_migration_url_is_accepted_by_the_wrapper_process(tmp_path):
    matching = "host=127.0.0.1 port=1 dbname=kerno_test user=kerno_test"
    completed = _wrapper(tmp_path, {**_UNREACHABLE_SETTINGS, safety.MIGRATION_DATABASE_URL_VARIABLE: matching}, "current")
    # 3, not 2: the configuration was accepted and only the unreachable target refused.
    assert completed.returncode == 3, _output(completed)[-2000:]
    assert f"test database: {_UNREACHABLE_APPROVAL}" in completed.stdout and SECRET not in _output(completed)


def _invalid_runtime_settings(tmp_path) -> dict:
    """Runtime settings that break every rule at once: half a pair in the environment plus a file with extra keys."""
    runtime_file = tmp_path / "invalid.env.test.runtime"
    runtime_file.write_text(
        f"{safety.TEST_RUNTIME_DATABASE_URL_VARIABLE}=postgresql://kerno_dev:{SECRET}@10.0.0.5:5432/kerno_dev\n"
        f"DATABASE_URL={FAKE_DEVELOPMENT_URL}\n",
        encoding="utf-8",
    )
    return {
        safety.TEST_RUNTIME_DATABASE_URL_VARIABLE: f"postgresql://kerno_dev:{SECRET}@10.0.0.5:5432/kerno_dev",
        safety.TEST_RUNTIME_ENV_FILE_VARIABLE: str(runtime_file),
    }


@pytest.mark.slow
@pytest.mark.parametrize("command", ["current", "grant-runtime"])
def test_the_wrapper_process_never_reads_runtime_settings(tmp_path, command):
    baseline = _wrapper(tmp_path, _UNREACHABLE_SETTINGS, command)
    with_runtime = _wrapper(tmp_path, {**_UNREACHABLE_SETTINGS, **_invalid_runtime_settings(tmp_path)}, command)
    output = _output(with_runtime)
    assert baseline.returncode == 3 and with_runtime.returncode == 3, output[-2000:]
    assert f"test database: {_UNREACHABLE_APPROVAL}" in with_runtime.stdout
    assert "KERNO_TEST_RUNTIME" not in output and SECRET not in output


@pytest.mark.slow
def test_the_wrapper_process_without_owner_settings_refuses_for_the_owner_and_ignores_runtime_settings(tmp_path):
    completed = _wrapper(tmp_path, _invalid_runtime_settings(tmp_path), "current")
    output = _output(completed)
    assert completed.returncode == 2, output[-2000:]
    assert "requires KERNO_TEST_DATABASE_URL" in completed.stdout
    assert "KERNO_TEST_RUNTIME" not in output and SECRET not in output


# ── The migration wrapper as a real process ─────────────────────────────────


@pytest.mark.slow
def test_the_wrapper_process_refuses_an_inherited_database_url_without_settings(tmp_path):
    completed = _run([sys.executable, str(_WRAPPER), "upgrade", "head"], tmp_path, {"DATABASE_URL": FAKE_DEVELOPMENT_URL})
    assert completed.returncode == 2
    assert "requires KERNO_TEST_DATABASE_URL" in completed.stdout and SECRET not in _output(completed)


@pytest.mark.slow
def test_the_wrapper_process_refuses_the_development_database_by_name(tmp_path):
    overrides = {"KERNO_TEST_DATABASE_URL": FAKE_DEVELOPMENT_URL, "KERNO_TEST_DATABASE_APPROVAL": "x"}
    completed = _run([sys.executable, str(_WRAPPER), "current"], tmp_path, overrides)
    assert completed.returncode == 2
    assert "must be exactly 'kerno_test'" in completed.stdout and SECRET not in _output(completed)


@pytest.mark.slow
def test_the_wrapper_process_names_the_target_by_identity_and_stops_when_unreachable(tmp_path):
    completed = _run([sys.executable, str(_WRAPPER), "upgrade", "head"], tmp_path, _UNREACHABLE_SETTINGS)
    assert completed.returncode == 3
    assert f"test database: {_UNREACHABLE_APPROVAL}" in completed.stdout
    assert SECRET not in _output(completed) and "postgresql://" not in _output(completed)


# ── The migration wrapper's decisions, with a scripted safety module ────────


def _load_wrapper():
    """Import scripts/migrate_test_database.py by path."""
    spec = importlib.util.spec_from_file_location("migrate_test_database", _WRAPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingSession:
    """A held session that records each re-proof, and can be told to lose the lock."""

    def __init__(self, calls: list[str], lose_after: int | None = None) -> None:
        self.calls = calls
        self.lose_after = lose_after

    def assert_held(self) -> None:
        self.calls.append("held?")
        if self.lose_after is not None and self.calls.count("held?") > self.lose_after:
            raise safety.DatabaseExclusivityLost("lost")


class _FakeSafety:
    """The wrapper's view of the safety module, with scripted outcomes and a record of calls."""

    KernoTestDatabaseError = safety.KernoTestDatabaseError
    DatabaseExclusivityLost = safety.DatabaseExclusivityLost
    redact = staticmethod(safety.redact)

    def __init__(self, prepare_error=None, session_error=None, lose_after=None) -> None:
        self.prepare_error = prepare_error
        self.session_error = session_error
        self.lose_after = lose_after
        self.calls: list[str] = []

    def prepare_migration_process(self):
        self.calls.append("prepare")
        if self.prepare_error:
            raise self.prepare_error
        return make_target()

    def ensure_exclusive_session(self, purpose):
        self.calls.append(f"lock:{purpose}")
        if self.session_error:
            raise self.session_error
        return _RecordingSession(self.calls, self.lose_after)

    def release_exclusive_session(self):
        self.calls.append("release")


def _scripted(monkeypatch, wrapper, calls: list[str], steps=("z1a2b3c4", "a2b3c4d5"), current="y0z1a2b3", fail_step=None):
    """Replace the wrapper's Alembic touch points with recorders; nothing real runs."""
    def apply_step(config, command, step):
        if step == fail_step:
            raise RuntimeError(f"permission denied while connecting with {GOOD_URL}")
        calls.append(step)

    monkeypatch.setattr(wrapper, "alembic_config", lambda: object())
    monkeypatch.setattr(wrapper, "current_revision", lambda target: current)
    monkeypatch.setattr(wrapper, "plan_steps", lambda *args: list(steps))
    monkeypatch.setattr(wrapper, "apply_step", apply_step)


def test_the_wrapper_refuses_without_configuration_and_never_takes_the_lock(capsys):
    wrapper = _load_wrapper()
    fake = _FakeSafety(prepare_error=safety.DatabaseTargetNotConfigured("not configured"))
    assert wrapper.main(["upgrade", "head"], safety=fake) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert fake.calls == ["prepare"]


def test_the_wrapper_is_excluded_by_a_running_suite_and_still_releases(capsys):
    wrapper = _load_wrapper()
    fake = _FakeSafety(session_error=safety.DatabaseTargetBusy("held by pytest"))
    assert wrapper.main(["upgrade", "head"], safety=fake) == wrapper.EXIT_TARGET_REFUSED
    assert fake.calls == ["prepare", "lock:migrate", "release"]


def test_the_wrapper_reproves_before_every_step_and_after_the_last_and_prints_no_credential(monkeypatch, capsys):
    wrapper = _load_wrapper()
    fake = _FakeSafety()
    _scripted(monkeypatch, wrapper, fake.calls)
    assert wrapper.main(["upgrade", "head"], safety=fake) == wrapper.EXIT_DONE
    assert fake.calls == [
        "prepare", "lock:migrate", "held?", "z1a2b3c4", "held?", "a2b3c4d5", "held?", "release",
    ]
    printed = capsys.readouterr().out
    assert "test database: kerno_test@127.0.0.1:5432/kerno_test" in printed
    assert SECRET not in printed and "postgresql://" not in printed


def test_a_lock_lost_during_the_last_step_is_not_reported_as_success(monkeypatch, capsys):
    wrapper = _load_wrapper()
    fake = _FakeSafety(lose_after=2)
    _scripted(monkeypatch, wrapper, fake.calls)
    assert wrapper.main(["upgrade", "head"], safety=fake) == wrapper.EXIT_TARGET_REFUSED
    assert fake.calls[-1] == "release" and "done:" not in capsys.readouterr().out


def test_a_failing_migration_releases_the_lock_and_prints_no_credential(monkeypatch, capsys):
    wrapper = _load_wrapper()
    fake = _FakeSafety()
    _scripted(monkeypatch, wrapper, fake.calls, fail_step="a2b3c4d5")
    assert wrapper.main(["upgrade", "head"], safety=fake) == wrapper.EXIT_MIGRATION_FAILED
    assert fake.calls[-1] == "release"
    printed = capsys.readouterr().out
    assert "migration failed" in printed and SECRET not in printed and "postgresql://" not in printed


def test_a_guard_refusal_inside_a_step_is_a_target_refusal_not_a_migration_failure(monkeypatch, capsys):
    wrapper = _load_wrapper()
    _scripted(monkeypatch, wrapper, [])

    def refuse(target):
        raise safety.DatabaseTargetRejected("a connection was requested to something else")

    monkeypatch.setattr(wrapper, "current_revision", refuse)
    code = wrapper.run_command("upgrade", "head", _RecordingSession([]), make_target(), safety)
    assert code == wrapper.EXIT_TARGET_REFUSED


def test_an_unreachable_destination_is_a_usage_refusal(monkeypatch, capsys):
    wrapper = _load_wrapper()
    _scripted(monkeypatch, wrapper, [])

    def unplannable(*args):
        raise ValueError("no such revision")

    monkeypatch.setattr(wrapper, "plan_steps", unplannable)
    code = wrapper.run_command("upgrade", "nonsense", _RecordingSession([]), make_target(), safety)
    assert code == wrapper.EXIT_CONFIGURATION_REFUSED


def test_the_wrapper_plans_one_revision_per_step_between_fixed_historical_revisions():
    from alembic.script import ScriptDirectory

    wrapper = _load_wrapper()
    config = wrapper.alembic_config()
    assert wrapper.plan_steps(config, "upgrade", "x9y0z1a2", "z1a2b3c4") == ["y0z1a2b3", "z1a2b3c4"]
    assert wrapper.plan_steps(config, "downgrade", "z1a2b3c4", "x9y0z1a2") == ["y0z1a2b3", "x9y0z1a2"]
    assert wrapper.plan_steps(config, "downgrade", None, "base") == []
    every_revision = list(ScriptDirectory.from_config(config).walk_revisions())
    assert len(wrapper.plan_steps(config, "upgrade", None, "head")) == len(every_revision)


@pytest.mark.parametrize("argv", [[], ["upgrade"], ["current", "x"], ["drop", "all"], ["downgrade", ""]])
def test_the_wrapper_rejects_malformed_commands(argv):
    assert _load_wrapper().parse_arguments(argv) is None
