"""Offline proof of the TEST-SAFETY-001 test-database boundary.

What:  Proves, with no real database, that the test and test-migration
       processes can only ever touch the owner-approved kerno_test database:
       settings come only from the dedicated variables or .env.test; unsafe,
       empty, malformed or redirecting configuration is refused before any
       connection; every connection path (psycopg2, SQLAlchemy, pools) is
       guarded; the live connection's real identity is checked read-only; one
       session-level advisory lock excludes a second workflow, pytest and the
       migration wrapper alike, and is released on success and failure; and
       no credential reaches any message.
Why:   These safeguards must be demonstrable without ever pointing a
       destructive fixture at the development database. Negative cases use
       in-memory fakes, temporary files and child processes whose only
       "development URL" is fake and points at 127.0.0.1 port 1.
How:   pytest tests/unit/test_database_safety.py -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys

import dotenv
import psycopg2
import psycopg2.pool
import pytest

from tests import _database_safety as safety
from tests import conftest

_SECRET = "probe-secret-zz"
_GOOD_URL = f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_test"
_GOOD_APPROVAL = "kerno_test@127.0.0.1:5432/kerno_test"
_FAKE_DEVELOPMENT_URL = f"postgresql://kerno_dev:{_SECRET}@127.0.0.1:1/kerno_dev"
_GOOD_IDENTITY = (
    "kerno_test", "kerno_test", "kerno_test", "127.0.0.1", 5432,
    False, False, False, False, False, 0, "kerno_test", safety.DISPOSABLE_DATABASE_COMMENT,
)
_PROBE = pathlib.Path(__file__).parent / "safety_probes" / "probe_test_process.py"
_PROBE_TIMEOUT_SECONDS = 180
_UNREACHABLE_PORT_URL = f"postgresql://kerno_test:{_SECRET}@127.0.0.1:1/kerno_test"
_UNREACHABLE_PORT_APPROVAL = "kerno_test@127.0.0.1:1/kerno_test"


def _target(url: str = _GOOD_URL) -> safety.DatabaseTarget:
    """Resolve a target from a URL whose approval is its own identity."""
    parameters = safety.effective_parameters(url)
    unvalidated = safety.DatabaseTarget(
        host=parameters["host"], port=int(parameters["port"]),
        dbname=parameters["dbname"], user=parameters["user"], url=url,
    )
    return safety.resolve_target(url, unvalidated.identity, {})


def _must_not_reach_libpq(*args, **kwargs):
    """Stand-in for the real psycopg2 connect: reaching it means the guard let a connection through."""
    raise AssertionError("the guard let a connection through to libpq")


# ── In-memory stand-in for PostgreSQL's advisory locks ──────────────────────


class _FakeLockServer:
    """One advisory lock and a backend-pid counter, shared by every fake connection."""

    def __init__(self) -> None:
        self.holder: int | None = None
        self.last_pid = 0


class _FakeConnection:
    """A connection that understands exactly the statements ExclusiveSession issues."""

    def __init__(self, server: _FakeLockServer, identity: tuple) -> None:
        server.last_pid += 1
        self.server = server
        self.pid = server.last_pid
        self.identity = identity
        self.executed: list[str] = []
        self.autocommit = False
        self.closed = False
        self.rollbacks = 0

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True
        if self.server.holder == self.pid:
            self.server.holder = None


class _FakeCursor:
    """Answers identity, try-lock, unlock, lock-held and holder queries against the fake server."""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection
        self._rows: list = []

    def execute(self, sql: str, params=None) -> None:
        connection = self._connection
        if connection.closed:
            raise psycopg2.InterfaceError("connection already closed")
        text = " ".join(sql.split())
        connection.executed.append(text)
        self._rows = self._answer(text, connection)

    def _answer(self, text: str, connection: _FakeConnection) -> list:
        server = connection.server
        if text.startswith("SET TRANSACTION READ ONLY"):
            return []
        if "current_database()" in text:
            return [connection.identity]
        if text.startswith("SELECT pg_try_advisory_lock"):
            free = server.holder is None
            server.holder = connection.pid if free else server.holder
            return [(free,)]
        if text.startswith("SELECT pg_advisory_unlock"):
            server.holder = None if server.holder == connection.pid else server.holder
            return [(True,)]
        if "pid = pg_backend_pid()" in text:
            return [(1 if server.holder == connection.pid else 0,)]
        if "pg_stat_activity" in text:
            return [(server.holder, "kerno-test-guard:pytest")] if server.holder else []
        raise AssertionError(f"unexpected statement: {text[:60]}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)


def _fake_connect(server: _FakeLockServer, opened: list, identity: tuple = _GOOD_IDENTITY):
    """Return a connect function that opens fake connections on one fake server."""
    def connect(url, application_name=None):
        connection = _FakeConnection(server, identity)
        opened.append(connection)
        return connection
    return connect


def _only_reads(connection: _FakeConnection) -> bool:
    """True when every statement the connection executed was a read or a lock call."""
    return all(text.startswith(("SET TRANSACTION READ ONLY", "SELECT")) for text in connection.executed)


class _HeldSession:
    """A session that reports itself active and held, for guard tests."""

    active = True

    def assert_held(self) -> None:
        return None


# ── Settings: one source, the dedicated keys only ───────────────────────────


def test_settings_are_read_as_a_pair_from_the_environment_first(tmp_path):
    env_file = tmp_path / ".env.test"
    env_file.write_text(f"KERNO_TEST_DATABASE_URL={_GOOD_URL}\nKERNO_TEST_DATABASE_APPROVAL={_GOOD_APPROVAL}\n")
    with pytest.raises(safety.DatabaseTargetInvalid, match="KERNO_TEST_DATABASE_APPROVAL must be set"):
        safety.read_settings({"KERNO_TEST_DATABASE_URL": _GOOD_URL}, env_file)
    from_environment = safety.read_settings(
        {"KERNO_TEST_DATABASE_URL": _GOOD_URL, "KERNO_TEST_DATABASE_APPROVAL": _GOOD_APPROVAL}, env_file
    )
    assert from_environment[2] == "the process environment"


def test_env_test_is_read_only_when_the_environment_is_silent_and_a_bom_is_tolerated(tmp_path):
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        f"KERNO_TEST_DATABASE_URL={_GOOD_URL}\nKERNO_TEST_DATABASE_APPROVAL={_GOOD_APPROVAL}\n",
        encoding="utf-8-sig",
    )
    assert safety.read_settings({}, env_file) == (_GOOD_URL, _GOOD_APPROVAL, ".env.test")
    assert safety.read_settings({}, tmp_path / "missing") is None


@pytest.mark.parametrize("extra", ["DATABASE_URL", "KERNO_JWT_SECRET", "MISTRAL_API_KEY"])
def test_env_test_refuses_any_other_key_and_names_it_without_its_value(tmp_path, extra):
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        f"KERNO_TEST_DATABASE_URL={_GOOD_URL}\nKERNO_TEST_DATABASE_APPROVAL={_GOOD_APPROVAL}\n"
        f"{extra}=postgresql://kerno_dev:{_SECRET}@127.0.0.1:5432/kerno_dev\n"
    )
    with pytest.raises(safety.DatabaseTargetInvalid) as refused:
        safety.read_settings({}, env_file)
    assert extra in str(refused.value) and _SECRET not in str(refused.value)


@pytest.mark.parametrize("url", ["", "   "])
def test_an_empty_setting_is_invalid_not_absent(url):
    with pytest.raises(safety.DatabaseTargetInvalid, match="non-empty"):
        safety.read_settings({"KERNO_TEST_DATABASE_URL": url, "KERNO_TEST_DATABASE_APPROVAL": _GOOD_APPROVAL}, None)


def test_the_default_settings_file_is_the_repository_env_test_and_is_gitignored():
    assert safety.settings_file({}) == safety.REPOSITORY_ROOT / ".env.test"
    if shutil.which("git") is None:
        pytest.skip("git is not available to check the ignore rule")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env.test"], cwd=safety.REPOSITORY_ROOT, capture_output=True
    )
    assert ignored.returncode == 0, ".env.test is not ignored by git"


# ── URL validation: the effective parameters, not the text ──────────────────


@pytest.mark.parametrize(
    "url, reason",
    [
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_dev", "must be exactly 'kerno_test'"),
        (f"postgresql://kerno_dev:{_SECRET}@127.0.0.1:5432/kerno_test", "role must be exactly"),
        (f"postgresql://kerno_test:{_SECRET}@10.0.0.5:5432/kerno_test", "on this machine"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1,10.0.0.5:5432/kerno_test", "exactly one host"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1/kerno_test", "state port"),
        (f"postgresql://127.0.0.1:5432/kerno_test", "state user"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432", "state dbname"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_test?dbname=kerno_dev", "selects 'kerno_dev'"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_test?host=10.0.0.5", "on this machine"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_test?user=kerno_dev", "role must be exactly"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_test?hostaddr=10.0.0.5", "may not set hostaddr"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_test?service=dev", "may not set service"),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_test?options=-c%20role%3Dx", "may not set options"),
        ("definitely not a connection string ===", "not a parseable"),
    ],
)
def test_unsafe_or_redirecting_urls_are_refused_before_any_connection(url, reason, monkeypatch):
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    with pytest.raises(safety.DatabaseTargetInvalid, match=reason) as refused:
        safety.resolve_target(url, _GOOD_APPROVAL, {})
    assert _SECRET not in str(refused.value)
    assert "postgresql://" not in str(refused.value)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS", "PGTARGETSESSIONATTRS"])
def test_libpq_environment_that_can_redirect_a_connection_is_refused(variable):
    with pytest.raises(safety.DatabaseTargetInvalid, match=variable):
        safety.resolve_target(_GOOD_URL, _GOOD_APPROVAL, {variable: "10.0.0.5"})


@pytest.mark.parametrize(
    "approval",
    ["kerno_test@127.0.0.1:5433/kerno_test", "kerno_test@localhost:5432/kerno_test", "yes", _GOOD_URL],
)
def test_approval_must_name_exactly_this_target_and_is_never_echoed(approval):
    with pytest.raises(safety.DatabaseTargetInvalid, match="does not match") as refused:
        safety.resolve_target(_GOOD_URL, approval, {})
    assert _SECRET not in str(refused.value)


def test_a_valid_target_prints_as_its_identity_and_hides_its_url():
    target = safety.resolve_target(_GOOD_URL, _GOOD_APPROVAL, {})
    assert (target.host, target.port, target.dbname, target.user) == ("127.0.0.1", 5432, "kerno_test", "kerno_test")
    assert str(target) == target.identity == _GOOD_APPROVAL
    assert _SECRET not in repr(target) and _SECRET not in str(target)
    ipv6 = _target(f"postgresql://kerno_test:{_SECRET}@[::1]:5432/kerno_test")
    assert ipv6.identity == "kerno_test@[::1]:5432/kerno_test"


def test_redact_removes_the_url_and_the_password_from_foreign_messages():
    target = _target()
    message = f"failed for {_GOOD_URL} with password {_SECRET}"
    assert _SECRET not in safety.redact(message, target)
    assert "postgresql://" not in safety.redact(message, target)


# ── The live connection's real identity ─────────────────────────────────────


def test_a_matching_identity_has_no_problems():
    identity = dict(zip(safety._IDENTITY_FIELDS, _GOOD_IDENTITY))
    assert safety.identity_problems(identity, _target()) == []


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("database", "kerno_dev", "connected database"),
        ("session_user", "kerno_dev", "session_user"),
        ("current_user", "postgres", "current_user"),
        ("database_owner", "kerno_dev", "database_owner"),
        ("server_address", "10.0.0.5", "not a TCP loopback"),
        ("server_address", None, "not a TCP loopback"),
        ("server_port", 5433, "server port"),
        ("superuser", True, "superuser"),
        ("bypass_rls", True, "bypass_rls"),
        ("create_database", True, "create_database"),
        ("create_role", True, "create_role"),
        ("replication", True, "replication"),
        ("memberships", 1, "member of other roles"),
        ("database_comment", None, "approval comment"),
    ],
)
def test_every_identity_mismatch_is_named(field, value, expected):
    identity = dict(zip(safety._IDENTITY_FIELDS, _GOOD_IDENTITY))
    identity[field] = value
    assert any(expected in problem for problem in safety.identity_problems(identity, _target()))


def test_identity_is_verified_read_only_and_leaves_no_transaction_open():
    connection = _FakeConnection(_FakeLockServer(), _GOOD_IDENTITY)
    safety.verify_live_connection(connection, _target())
    assert connection.executed[0] == "SET TRANSACTION READ ONLY"
    assert connection.rollbacks == 1 and _only_reads(connection)


# ── One workflow at a time ──────────────────────────────────────────────────


def test_the_lock_is_the_documented_two_integer_key():
    assert (safety.EXCLUSIVE_LOCK_CLASS_KEY, safety.EXCLUSIVE_LOCK_OBJECT_KEY) == (0x4B45524E, 0x54455354)
    assert "pg_try_advisory_lock(%s, %s)" in safety._TRY_LOCK_SQL
    assert "xact" not in safety._TRY_LOCK_SQL


@pytest.mark.parametrize("first, second", [("pytest", "pytest"), ("pytest", "migrate"), ("migrate", "pytest")])
def test_a_second_workflow_is_refused_before_it_writes_anything(first, second):
    server, opened = _FakeLockServer(), []
    holder = safety.ExclusiveSession(_target(), first, connect=_fake_connect(server, opened))
    holder.acquire()
    contender = safety.ExclusiveSession(_target(), second, connect=_fake_connect(server, opened))
    with pytest.raises(safety.DatabaseTargetBusy, match=f"pid {holder._connection.pid}"):
        contender.acquire()
    assert not contender.active and opened[1].closed and _only_reads(opened[1])
    assert server.holder == opened[0].pid


def test_the_lock_is_released_after_success_and_after_failure():
    server, opened = _FakeLockServer(), []
    session = safety.ExclusiveSession(_target(), "pytest", connect=_fake_connect(server, opened))
    with pytest.raises(RuntimeError):
        session.acquire()
        try:
            raise RuntimeError("a fixture failed mid-run")
        finally:
            session.release()
    assert server.holder is None and opened[0].closed
    session.acquire()
    session.release()
    session.release()
    assert server.holder is None and opened[1].closed


def test_a_wrong_live_target_is_refused_and_takes_no_lock():
    server, opened = _FakeLockServer(), []
    wrong = ("kerno_dev",) + _GOOD_IDENTITY[1:]
    session = safety.ExclusiveSession(_target(), "pytest", connect=_fake_connect(server, opened, wrong))
    with pytest.raises(safety.DatabaseTargetRejected, match="kerno_dev"):
        session.acquire()
    assert server.holder is None and opened[0].closed
    assert not any("advisory_lock" in text for text in opened[0].executed)


def test_losing_the_lock_or_the_guard_connection_is_detected():
    server, opened = _FakeLockServer(), []
    session = safety.ExclusiveSession(_target(), "pytest", connect=_fake_connect(server, opened))
    session.acquire()
    session.assert_held()
    server.holder = None
    with pytest.raises(safety.DatabaseExclusivityLost):
        session.assert_held()
    server.holder = opened[0].pid
    opened[0].closed = True
    with pytest.raises(safety.DatabaseExclusivityLost):
        session.assert_held()


def test_an_unreachable_target_is_refused_without_the_password():
    def refuse(url, application_name=None):
        raise psycopg2.OperationalError(f"could not connect using {_GOOD_URL} password {_SECRET}")

    with pytest.raises(safety.DatabaseTargetRejected) as refused:
        safety.ExclusiveSession(_target(), "pytest", connect=refuse).acquire()
    assert _SECRET not in str(refused.value)


def test_the_process_session_is_acquired_once_rechecked_every_time_and_released():
    server, opened = _FakeLockServer(), []
    state = safety.ProcessState(target=_target())
    first = safety.ensure_exclusive_session("pytest", state=state, connect=_fake_connect(server, opened))
    again = safety.ensure_exclusive_session("pytest", state=state)
    assert first is again and len(opened) == 1
    assert sum("pid = pg_backend_pid()" in text for text in opened[0].executed) == 2
    safety.release_exclusive_session(state)
    assert state.session is None and server.holder is None


def test_no_session_is_opened_without_a_valid_target():
    with pytest.raises(safety.DatabaseTargetNotConfigured):
        safety.ensure_exclusive_session("pytest", state=safety.ProcessState())
    with pytest.raises(safety.DatabaseTargetInvalid, match="bad settings"):
        safety.ensure_exclusive_session("pytest", state=safety.ProcessState(problem="bad settings"))


# ── The connection guard every path goes through ────────────────────────────


def test_psycopg2_connect_is_the_guard_in_this_process():
    assert psycopg2.connect is safety._guarded_connect


def test_with_no_target_every_connection_path_is_refused_before_libpq(monkeypatch):
    from sqlalchemy import create_engine

    monkeypatch.setattr(safety, "_STATE", safety.ProcessState())
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    attempts = [
        lambda: psycopg2.connect(_FAKE_DEVELOPMENT_URL),
        lambda: psycopg2.connect(""),
        lambda: psycopg2.connect(),
        lambda: psycopg2.connect(host="127.0.0.1", port=1, dbname="kerno_dev", user="kerno_dev"),
        lambda: create_engine(_FAKE_DEVELOPMENT_URL).connect(),
        lambda: psycopg2.pool.ThreadedConnectionPool(1, 1, dsn=_FAKE_DEVELOPMENT_URL),
    ]
    for attempt in attempts:
        with pytest.raises(safety.DatabaseTargetNotConfigured):
            attempt()


def test_with_a_target_but_no_held_session_even_the_target_is_refused(monkeypatch):
    monkeypatch.setattr(safety, "_STATE", safety.ProcessState(target=_target()))
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    with pytest.raises(safety.DatabaseTargetRejected, match="outside the exclusive session"):
        psycopg2.connect(_GOOD_URL)


@pytest.mark.parametrize(
    "dsn, keywords",
    [
        (_FAKE_DEVELOPMENT_URL, {}),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5432/kerno_test?dbname=kerno_dev", {}),
        (f"postgresql://kerno_test:{_SECRET}@127.0.0.1:5433/kerno_test", {}),
        (None, {"host": "127.0.0.1", "port": 5432, "dbname": "kerno_test", "user": "postgres"}),
        (_GOOD_URL, {"options": "-c role=postgres"}),
        ("", {}),
    ],
)
def test_inside_the_session_only_the_approved_target_is_allowed(monkeypatch, dsn, keywords):
    state = safety.ProcessState(target=_target(), session=_HeldSession())
    with pytest.raises(safety.KernoTestDatabaseError) as refused:
        safety.authorize_connection(dsn, keywords, state)
    assert _SECRET not in str(refused.value)
    safety.authorize_connection(_GOOD_URL, {}, state)
    safety.authorize_connection(
        None, {"host": "127.0.0.1", "port": 5432, "dbname": "kerno_test", "user": "kerno_test", "password": "x"}, state
    )


# ── Bootstrap ───────────────────────────────────────────────────────────────


def _bootstrap(environ: dict, tmp_path, loaded_modules=None) -> safety.ProcessState:
    """Bootstrap a fresh state against a fake environment with no .env.test on disk."""
    environ.setdefault(safety.TEST_ENV_FILE_VARIABLE, str(tmp_path / "absent.env.test"))
    return safety.bootstrap_test_process(environ, safety.ProcessState(), loaded_modules or {})


def test_an_inherited_database_url_is_removed_and_never_becomes_authorisation(tmp_path):
    environ = {"DATABASE_URL": _FAKE_DEVELOPMENT_URL}
    state = _bootstrap(environ, tmp_path)
    assert "DATABASE_URL" not in environ
    assert state.target is None and state.problem is None
    assert environ["PYTHON_DOTENV_DISABLED"] == "1"


def test_a_valid_target_becomes_the_process_database_url(tmp_path):
    environ = {"KERNO_TEST_DATABASE_URL": _GOOD_URL, "KERNO_TEST_DATABASE_APPROVAL": _GOOD_APPROVAL}
    state = _bootstrap(environ, tmp_path)
    assert state.target.identity == _GOOD_APPROVAL and environ["DATABASE_URL"] == _GOOD_URL


def test_invalid_settings_are_recorded_as_a_problem_and_adopt_nothing(tmp_path):
    environ = {
        "DATABASE_URL": _FAKE_DEVELOPMENT_URL,
        "KERNO_TEST_DATABASE_URL": _FAKE_DEVELOPMENT_URL,
        "KERNO_TEST_DATABASE_APPROVAL": _GOOD_APPROVAL,
    }
    state = _bootstrap(environ, tmp_path)
    assert state.target is None and "kerno_test" in state.problem and _SECRET not in state.problem
    assert "DATABASE_URL" not in environ


def test_a_bootstrap_after_the_application_was_imported_is_refused(tmp_path):
    state = _bootstrap({}, tmp_path, loaded_modules={"src.api.app": object(), "pytest": object()})
    assert "src.api.app" in state.problem


def test_dotenv_loading_stays_disabled_in_this_test_process_after_the_app_is_imported():
    import src.api.app  # noqa: F401

    assert dotenv.load_dotenv(stream=io.StringIO("KERNO_SAFETY_PROBE=1")) is False
    assert "KERNO_SAFETY_PROBE" not in os.environ
    live = safety.current_state().target
    assert os.environ.get("DATABASE_URL") == (live.url if live else None)


# ── The fixture path: stop before any write ─────────────────────────────────


def _outcome_of(action) -> BaseException | None:
    """Run action and return whatever it raised, pytest outcomes included.

    pytest.raises would let a Skipped outcome escape and turn "skipped instead
    of stopping" into a skipped test rather than a failed one.
    """
    try:
        action()
    except BaseException as raised:  # noqa: BLE001 — pytest outcomes derive from BaseException
        return raised
    return None


class _Config:
    """The one pytest config call the fixture helpers make."""

    def __init__(self, require_live: bool = False) -> None:
        self._require_live = require_live

    def getoption(self, name: str) -> bool:
        return self._require_live


@pytest.mark.parametrize(
    "error, code",
    [
        (safety.DatabaseTargetBusy("held by pid 7"), pytest.ExitCode.INTERRUPTED),
        (safety.DatabaseTargetRejected("wrong database"), pytest.ExitCode.USAGE_ERROR),
    ],
)
def test_a_busy_or_rejected_target_stops_the_run_before_any_fixture_write(monkeypatch, error, code):
    def refuse(purpose, state=None, connect=None):
        raise error

    monkeypatch.setattr(safety, "ensure_exclusive_session", refuse)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    outcome = _outcome_of(
        lambda: conftest.open_guarded_fixture_connection(_Config(), state=safety.ProcessState(target=_target()))
    )
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == code


def test_with_no_target_the_fixture_skips_or_fails_when_live_is_required():
    with pytest.raises(pytest.skip.Exception, match="no approved test database"):
        conftest.open_guarded_fixture_connection(_Config(), state=safety.ProcessState())
    with pytest.raises(pytest.fail.Exception, match="live database required"):
        conftest.open_guarded_fixture_connection(_Config(require_live=True), state=safety.ProcessState())


def _calls_in_source_order(function_name: str) -> list[str]:
    """Names of the functions a conftest function calls, in the order they appear."""
    import ast

    tree = ast.parse(pathlib.Path(conftest.__file__).read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    return [call.func.id for call in sorted(calls, key=lambda call: (call.lineno, call.col_offset))]


def test_the_fixture_proves_exclusivity_before_every_seed_and_every_cleanup():
    calls = [
        name for name in _calls_in_source_order("db_connection")
        if name in {"open_guarded_fixture_connection", "require_exclusive_session",
                    "_teardown_seed_data", "_seed_integration_data"}
    ]
    assert calls == [
        "open_guarded_fixture_connection", "require_exclusive_session",
        "_teardown_seed_data", "_seed_integration_data",
        "require_exclusive_session", "_teardown_seed_data",
    ]


def test_cleanup_stops_the_run_when_exclusivity_cannot_be_proved():
    server, opened = _FakeLockServer(), []
    state = safety.ProcessState(target=_target())
    assert isinstance(_outcome_of(lambda: conftest.require_exclusive_session(state)), pytest.exit.Exception)
    safety.ensure_exclusive_session("pytest", state=state, connect=_fake_connect(server, opened))
    assert _outcome_of(lambda: conftest.require_exclusive_session(state)) is None
    server.holder = None
    assert isinstance(_outcome_of(lambda: conftest.require_exclusive_session(state)), pytest.exit.Exception)
    safety.release_exclusive_session(state)


# ── Real pytest processes, fake targets ─────────────────────────────────────


def _run_probe(tmp_path, env: dict | None = None, *args: str) -> subprocess.CompletedProcess:
    """Run the probe in a fresh pytest process with only the given test settings; never a real target."""
    stripped = {
        "DATABASE_URL", "PYTHON_DOTENV_DISABLED",
        safety.TEST_DATABASE_URL_VARIABLE, safety.TEST_DATABASE_APPROVAL_VARIABLE,
        *safety.TARGET_CHANGING_LIBPQ_VARIABLES,
    }
    child = {name: value for name, value in os.environ.items() if name not in stripped}
    child[safety.TEST_ENV_FILE_VARIABLE] = str(tmp_path / "absent.env.test")
    child.update(env or {})
    command = [sys.executable, "-m", "pytest", str(_PROBE), "-s", "-rs", "-p", "no:cacheprovider", *args]
    return subprocess.run(
        command, cwd=safety.REPOSITORY_ROOT, env=child, capture_output=True, text=True,
        timeout=_PROBE_TIMEOUT_SECONDS,
    )


def _report(completed: subprocess.CompletedProcess) -> dict:
    """Return the probe's JSON report from the child's output."""
    for line in completed.stdout.splitlines():
        if line.startswith("KERNO_PROBE_REPORT="):
            return json.loads(line.split("=", 1)[1])
    raise AssertionError(f"no probe report; exit {completed.returncode}:\n{completed.stdout[-2000:]}")


@pytest.mark.slow
@pytest.mark.parametrize("inherited", [{}, {"DATABASE_URL": _FAKE_DEVELOPMENT_URL}])
def test_with_no_test_settings_no_connection_path_can_open_a_database(tmp_path, inherited):
    completed = _run_probe(tmp_path, inherited)
    assert completed.returncode == 0, completed.stdout[-2000:]
    report = _report(completed)
    assert report["database_url_present"] is False and report["dotenv_loaded"] is False
    outcomes = report["outcomes"]
    assert outcomes.pop("application_pool") == "RuntimeError"
    assert set(outcomes.values()) == {"DatabaseTargetNotConfigured"}
    assert "1 passed, 1 skipped" in completed.stdout
    assert "live database test skipped: no approved test database is configured" in completed.stdout
    assert _SECRET not in completed.stdout + completed.stderr


@pytest.mark.slow
def test_requiring_live_validation_without_settings_fails_clearly(tmp_path):
    completed = _run_probe(tmp_path, {"DATABASE_URL": _FAKE_DEVELOPMENT_URL}, "--require-live-database")
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert "--require-live-database was given" in completed.stdout + completed.stderr
    assert "KERNO_PROBE_REPORT" not in completed.stdout


@pytest.mark.slow
@pytest.mark.parametrize(
    "url, approval, expected",
    [
        (_FAKE_DEVELOPMENT_URL, "kerno_dev@127.0.0.1:1/kerno_dev", "must be exactly 'kerno_test'"),
        ("", _UNREACHABLE_PORT_APPROVAL, "non-empty"),
        ("::: not a url :::", _UNREACHABLE_PORT_APPROVAL, "not a parseable"),
        (_UNREACHABLE_PORT_URL + "?dbname=kerno_dev", _UNREACHABLE_PORT_APPROVAL, "selects 'kerno_dev'"),
    ],
)
def test_invalid_test_settings_fail_the_run_before_any_test_or_connection(tmp_path, url, approval, expected):
    completed = _run_probe(
        tmp_path, {"KERNO_TEST_DATABASE_URL": url, "KERNO_TEST_DATABASE_APPROVAL": approval}
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output[-2000:]
    assert "test database configuration refused" in output and expected in output
    assert "KERNO_PROBE_REPORT" not in completed.stdout and _SECRET not in output


@pytest.mark.slow
def test_a_redirecting_libpq_variable_fails_the_run(tmp_path):
    completed = _run_probe(tmp_path, {
        "KERNO_TEST_DATABASE_URL": _UNREACHABLE_PORT_URL,
        "KERNO_TEST_DATABASE_APPROVAL": _UNREACHABLE_PORT_APPROVAL,
        "PGHOSTADDR": "10.0.0.5",
    })
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR
    assert "PGHOSTADDR" in completed.stdout + completed.stderr


@pytest.mark.slow
def test_an_unreachable_approved_target_stops_the_run_instead_of_skipping(tmp_path):
    completed = _run_probe(tmp_path, {
        "KERNO_TEST_DATABASE_URL": _UNREACHABLE_PORT_URL,
        "KERNO_TEST_DATABASE_APPROVAL": _UNREACHABLE_PORT_APPROVAL,
    })
    output = completed.stdout + completed.stderr
    report = _report(completed)
    assert report["database_url_present"] is True
    assert report["outcomes"]["psycopg2_connect"] == "DatabaseTargetRejected"
    assert completed.returncode == pytest.ExitCode.USAGE_ERROR, output[-2000:]
    assert "test database refused" in output and "skipped" not in output
    assert _SECRET not in output


# ── The migration wrapper ───────────────────────────────────────────────────


def _load_wrapper():
    """Import scripts/migrate_test_database.py by path."""
    path = safety.REPOSITORY_ROOT / "scripts" / "migrate_test_database.py"
    spec = importlib.util.spec_from_file_location("migrate_test_database", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSafety:
    """The wrapper's view of the safety module, with scripted outcomes and a record of calls."""

    KernoTestDatabaseError = safety.KernoTestDatabaseError
    DatabaseExclusivityLost = safety.DatabaseExclusivityLost
    redact = staticmethod(safety.redact)

    def __init__(self, prepare_error=None, session_error=None) -> None:
        self.prepare_error = prepare_error
        self.session_error = session_error
        self.calls: list[str] = []

    def prepare_migration_process(self):
        self.calls.append("prepare")
        if self.prepare_error:
            raise self.prepare_error
        return _target()

    def ensure_exclusive_session(self, purpose):
        self.calls.append(f"lock:{purpose}")
        if self.session_error:
            raise self.session_error
        return _RecordingSession(self.calls)

    def release_exclusive_session(self):
        self.calls.append("release")


class _RecordingSession:
    """A held session that records each re-proof, and can be told to lose the lock."""

    def __init__(self, calls: list[str], lose_after: int | None = None) -> None:
        self.calls = calls
        self.lose_after = lose_after

    def assert_held(self) -> None:
        self.calls.append("held?")
        if self.lose_after is not None and self.calls.count("held?") > self.lose_after:
            raise safety.DatabaseExclusivityLost("lost")


def test_the_wrapper_refuses_without_configuration_and_never_takes_the_lock():
    wrapper = _load_wrapper()
    fake = _FakeSafety(prepare_error=safety.DatabaseTargetNotConfigured("not configured"))
    assert wrapper.main(["upgrade", "head"], safety=fake) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert fake.calls == ["prepare"]


def test_the_wrapper_is_excluded_by_a_running_suite_and_releases_nothing_it_never_held():
    wrapper = _load_wrapper()
    fake = _FakeSafety(session_error=safety.DatabaseTargetBusy("held by pytest"))
    assert wrapper.main(["upgrade", "head"], safety=fake) == wrapper.EXIT_TARGET_REFUSED
    assert fake.calls == ["prepare", "lock:migrate"]


def test_the_wrapper_reproves_the_lock_before_every_step_and_always_releases(monkeypatch):
    wrapper = _load_wrapper()
    fake = _FakeSafety()

    def record_step(config, command, step):
        fake.calls.append(step)

    monkeypatch.setattr(wrapper, "alembic_config", lambda: object())
    monkeypatch.setattr(wrapper, "current_revision", lambda target: "y0z1a2b3")
    monkeypatch.setattr(wrapper, "plan_steps", lambda *args: ["z1a2b3c4", "a2b3c4d5"])
    monkeypatch.setattr(wrapper, "apply_step", record_step)
    assert wrapper.main(["upgrade", "head"], safety=fake) == wrapper.EXIT_DONE
    assert fake.calls == ["prepare", "lock:migrate", "held?", "z1a2b3c4", "held?", "a2b3c4d5", "release"]


def test_the_wrapper_stops_when_exclusivity_is_lost_and_on_migration_errors(monkeypatch):
    wrapper = _load_wrapper()
    calls: list[str] = []
    session = _RecordingSession(calls, lose_after=1)
    monkeypatch.setattr(wrapper, "alembic_config", lambda: object())
    monkeypatch.setattr(wrapper, "current_revision", lambda target: None)
    monkeypatch.setattr(wrapper, "plan_steps", lambda *args: ["001", "a1b2c3d4"])
    monkeypatch.setattr(wrapper, "apply_step", lambda config, command, step: calls.append(step))
    assert wrapper.run_command("upgrade", "head", session, _target(), safety) == wrapper.EXIT_TARGET_REFUSED
    assert calls == ["held?", "001", "held?"]

    def explode(config, command, step):
        raise RuntimeError(f"permission denied, connecting with {_GOOD_URL}")

    monkeypatch.setattr(wrapper, "apply_step", explode)
    assert wrapper.run_command("upgrade", "head", _RecordingSession([]), _target(), safety) == wrapper.EXIT_MIGRATION_FAILED


def test_the_wrapper_plans_one_revision_per_step():
    wrapper = _load_wrapper()
    config = wrapper.alembic_config()
    assert wrapper.plan_steps(config, "upgrade", "y0z1a2b3", "head") == ["z1a2b3c4", "a2b3c4d5"]
    assert wrapper.plan_steps(config, "downgrade", "a2b3c4d5", "z1a2b3c4") == ["z1a2b3c4"]
    assert wrapper.plan_steps(config, "downgrade", None, "base") == []
    assert len(wrapper.plan_steps(config, "upgrade", None, "head")) == 26


@pytest.mark.parametrize("argv", [[], ["upgrade"], ["current", "x"], ["drop", "all"], ["downgrade", ""]])
def test_the_wrapper_rejects_malformed_commands(argv):
    assert _load_wrapper().parse_arguments(argv) is None
