"""Offline proof of the TEST-SAFETY-001 test-database boundary — in-process checks.

What:  Proves, with no real database, that a test process can only ever
       touch the owner-approved kerno_test database: settings come only from
       the dedicated variables or .env.test; unsafe, empty, malformed or
       redirecting configuration is refused before any connection and without
       echoing a credential; every psycopg2.connect path is guarded and
       re-proves the lock; the lower-level entry points are absent from the
       repository; the live connection's identity is checked read-only; one
       session-level advisory lock, on the documented key, excludes a second
       workflow and is released; and the fixture and pytest hooks stop,
       re-prove and release exactly where they claim to.
Why:   These safeguards must be demonstrable without pointing a destructive
       fixture at any database. Everything here uses in-memory fakes and
       temporary files. Child-process and migration-wrapper proofs are in
       tests/unit/test_database_safety_processes.py.
How:   pytest tests/unit/test_database_safety.py -v
"""

from __future__ import annotations

import io
import os
import pathlib
import re
import shutil
import subprocess
import types

import dotenv
import psycopg2
import psycopg2.pool
import pytest

from tests import _database_safety as safety
from tests import conftest

SECRET = "probe-secret-zz"
GOOD_URL = f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test"
GOOD_APPROVAL = "kerno_test@127.0.0.1:5432/kerno_test"
FAKE_DEVELOPMENT_URL = f"postgresql://kerno_dev:{SECRET}@127.0.0.1:1/kerno_dev"
GOOD_IDENTITY = (
    "kerno_test", "kerno_test", "kerno_test", "127.0.0.1", 5432,
    False, False, False, False, False, 0, "kerno_test", safety.DISPOSABLE_DATABASE_COMMENT,
)
_DOCUMENTED_KEY = (0x4B45524E, 0x54455354)


def make_target(url: str = GOOD_URL) -> safety.DatabaseTarget:
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


def _outcome_of(action) -> BaseException | None:
    """Run action and return whatever it raised, pytest outcomes included.

    pytest.raises lets an unexpected Skipped outcome escape, which would turn
    "skipped instead of stopping" into a skipped test rather than a failure.
    """
    try:
        action()
    except BaseException as raised:  # noqa: BLE001 — pytest outcomes derive from BaseException
        return raised
    return None


# ── In-memory stand-in for PostgreSQL's advisory locks ──────────────────────


class FakeLockServer:
    """Advisory locks keyed exactly as PostgreSQL keys them, and a backend-pid counter."""

    def __init__(self) -> None:
        self.locks: dict[tuple, int] = {}
        self.last_pid = 0
        self.fail_holder_query = False
        self.fail_identity_query = False

    def holder(self) -> int | None:
        """The pid holding the documented key, if any."""
        return self.locks.get(_DOCUMENTED_KEY)


class FakeConnection:
    """A connection that understands exactly the statements ExclusiveSession issues."""

    def __init__(self, server: FakeLockServer, identity: tuple) -> None:
        server.last_pid += 1
        self.server = server
        self.pid = server.last_pid
        self.identity = identity
        self.executed: list[str] = []
        self.lock_keys: list[tuple] = []
        self.autocommit = False
        self.closed = False
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True
        for key in [key for key, pid in self.server.locks.items() if pid == self.pid]:
            del self.server.locks[key]


class FakeCursor:
    """Answers identity, try-lock, unlock, lock-held and holder queries against the fake server."""

    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self._rows: list = []

    def execute(self, sql: str, params=None) -> None:
        connection = self._connection
        if connection.closed:
            raise psycopg2.InterfaceError("connection already closed")
        text = " ".join(sql.split())
        connection.executed.append(text)
        self._rows = self._answer(text, tuple(params or ()), connection)

    def _answer(self, text: str, params: tuple, connection: FakeConnection) -> list:
        server = connection.server
        if text.startswith("SET TRANSACTION READ ONLY"):
            return []
        if text.startswith("SELECT current_database()"):
            if server.fail_identity_query:
                raise psycopg2.Error("identity query failed")
            return [connection.identity]
        if text.startswith(("SELECT pg_try_advisory_lock", "SELECT pg_advisory_unlock")):
            connection.lock_keys.append(params)
            return self._lock_or_unlock(text, params, connection)
        if "pid = pg_backend_pid()" in text:
            connection.lock_keys.append(params[:2])
            assert params[2] == 2, "a two-integer advisory lock is reported with objsubid = 2"
            return [(1 if server.locks.get(params[:2]) == connection.pid else 0,)]
        if "pg_stat_activity" in text:
            if server.fail_holder_query:
                raise psycopg2.Error("insufficient privilege")
            holder = server.locks.get(params[:2])
            return [(holder, "kerno-test-guard:pytest")] if holder else []
        raise AssertionError(f"unexpected statement: {text[:60]}")

    @staticmethod
    def _lock_or_unlock(text: str, key: tuple, connection: FakeConnection) -> list:
        locks = connection.server.locks
        if text.startswith("SELECT pg_try_advisory_lock"):
            free = key not in locks
            if free:
                locks[key] = connection.pid
            return [(free,)]
        if locks.get(key) == connection.pid:
            del locks[key]
        return [(True,)]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)


def fake_connect(server: FakeLockServer, opened: list, identity: tuple = GOOD_IDENTITY):
    """Return a connect function that opens fake connections on one fake server."""
    def connect(url, application_name=None):
        connection = FakeConnection(server, identity)
        opened.append(connection)
        return connection
    return connect


def _only_reads(connection: FakeConnection) -> bool:
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
    env_file.write_text(f"KERNO_TEST_DATABASE_URL={GOOD_URL}\nKERNO_TEST_DATABASE_APPROVAL={GOOD_APPROVAL}\n")
    with pytest.raises(safety.DatabaseTargetInvalid, match="KERNO_TEST_DATABASE_APPROVAL must be set"):
        safety.read_settings({"KERNO_TEST_DATABASE_URL": GOOD_URL}, env_file)
    from_environment = safety.read_settings(
        {"KERNO_TEST_DATABASE_URL": GOOD_URL, "KERNO_TEST_DATABASE_APPROVAL": GOOD_APPROVAL}, env_file
    )
    assert from_environment[2] == "the process environment"


def test_env_test_is_read_only_when_the_environment_is_silent_and_a_bom_is_tolerated(tmp_path):
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        f"KERNO_TEST_DATABASE_URL={GOOD_URL}\nKERNO_TEST_DATABASE_APPROVAL={GOOD_APPROVAL}\n",
        encoding="utf-8-sig",
    )
    assert safety.read_settings({}, env_file) == (GOOD_URL, GOOD_APPROVAL, ".env.test")
    assert safety.read_settings({}, tmp_path / "missing") is None


@pytest.mark.parametrize("extra", ["DATABASE_URL", "KERNO_JWT_SECRET", "MISTRAL_API_KEY"])
def test_env_test_refuses_any_other_key_and_names_it_without_its_value(tmp_path, extra):
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        f"KERNO_TEST_DATABASE_URL={GOOD_URL}\nKERNO_TEST_DATABASE_APPROVAL={GOOD_APPROVAL}\n"
        f"{extra}=postgresql://kerno_dev:{SECRET}@127.0.0.1:5432/kerno_dev\n"
    )
    with pytest.raises(safety.DatabaseTargetInvalid) as refused:
        safety.read_settings({}, env_file)
    assert extra in str(refused.value) and SECRET not in str(refused.value)


def test_a_line_that_is_not_an_assignment_is_counted_never_echoed(tmp_path):
    # python-dotenv reads a bare line as a key named after the whole line —
    # here a pasted connection URL with its password.
    env_file = tmp_path / ".env.test"
    env_file.write_text(
        f"KERNO_TEST_DATABASE_URL={GOOD_URL}\nKERNO_TEST_DATABASE_APPROVAL={GOOD_APPROVAL}\n"
        f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test\n"
    )
    with pytest.raises(safety.DatabaseTargetInvalid, match="not NAME=value") as refused:
        safety.read_settings({}, env_file)
    assert SECRET not in str(refused.value) and "postgresql://" not in str(refused.value)


@pytest.mark.parametrize("url, approval", [("", GOOD_APPROVAL), ("   ", GOOD_APPROVAL), ("", ""), (GOOD_URL, "")])
def test_empty_settings_are_invalid_not_absent_even_when_both_are_empty(tmp_path, url, approval):
    environment = {"KERNO_TEST_DATABASE_URL": url, "KERNO_TEST_DATABASE_APPROVAL": approval}
    with pytest.raises(safety.DatabaseTargetInvalid, match="non-empty"):
        safety.read_settings(environment, None)
    env_file = tmp_path / ".env.test"
    env_file.write_text(f"KERNO_TEST_DATABASE_URL={url}\nKERNO_TEST_DATABASE_APPROVAL={approval}\n")
    with pytest.raises(safety.DatabaseTargetInvalid, match="non-empty"):
        safety.read_settings({}, env_file)


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
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_dev", "selects a different database"),
        (f"postgresql://kerno_dev:{SECRET}@127.0.0.1:5432/kerno_test", "logs in as a different role"),
        (f"postgresql://kerno_test:{SECRET}@10.0.0.5:5432/kerno_test", "literal loopback address"),
        (f"postgresql://kerno_test:{SECRET}@localhost:5432/kerno_test", "literal loopback address"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1,10.0.0.5:5432/kerno_test", "exactly one host"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1/kerno_test", "state port"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:05432/kerno_test", "plain number"),
        ("postgresql://127.0.0.1:5432/kerno_test", "state user"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432", "state dbname"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test?dbname=kerno_dev", "selects a different database"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test?host=10.0.0.5", "literal loopback address"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test?user=kerno_dev", "different role"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test?hostaddr=10.0.0.5", "may not set hostaddr"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test?service=dev", "may not set service"),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test?options=-c%20role%3Dx", "may not set options"),
        ("definitely not a connection string ===", "not a parseable"),
    ],
)
def test_unsafe_or_redirecting_urls_are_refused_before_any_connection(url, reason, monkeypatch):
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    with pytest.raises(safety.DatabaseTargetInvalid, match=reason) as refused:
        safety.resolve_target(url, GOOD_APPROVAL, {})
    assert SECRET not in str(refused.value)
    assert "postgresql://" not in str(refused.value)


def test_an_unencoded_at_sign_in_the_password_leaks_no_part_of_it():
    # libpq ends the user-info at the first "@", so the password's tail is
    # read as the host — which must be refused without being printed.
    url = "postgresql://kerno_test:probe@tail-of-secret-zz@127.0.0.1:5432/kerno_test"
    with pytest.raises(safety.DatabaseTargetInvalid) as refused:
        safety.resolve_target(url, GOOD_APPROVAL, {})
    assert "tail-of-secret-zz" not in str(refused.value)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS", "PGTARGETSESSIONATTRS"])
def test_libpq_environment_that_can_redirect_a_connection_is_refused(variable):
    with pytest.raises(safety.DatabaseTargetInvalid, match=variable):
        safety.resolve_target(GOOD_URL, GOOD_APPROVAL, {variable: "10.0.0.5"})


@pytest.mark.parametrize(
    "approval",
    ["kerno_test@127.0.0.1:5433/kerno_test", "kerno_test@[::1]:5432/kerno_test", "yes", GOOD_URL],
)
def test_approval_must_name_exactly_this_target_and_is_never_echoed(approval):
    with pytest.raises(safety.DatabaseTargetInvalid, match="does not match") as refused:
        safety.resolve_target(GOOD_URL, approval, {})
    assert SECRET not in str(refused.value)


def test_a_valid_target_prints_as_its_identity_and_hides_its_url():
    target = safety.resolve_target(GOOD_URL, GOOD_APPROVAL, {})
    assert (target.host, target.port, target.dbname, target.user) == ("127.0.0.1", 5432, "kerno_test", "kerno_test")
    assert str(target) == target.identity == GOOD_APPROVAL
    assert SECRET not in repr(target) and SECRET not in str(target)
    ipv6 = make_target(f"postgresql://kerno_test:{SECRET}@[::1]:5432/kerno_test")
    assert ipv6.identity == "kerno_test@[::1]:5432/kerno_test"


def test_redact_removes_the_url_and_the_password_from_foreign_messages():
    target = make_target()
    message = f"failed for {GOOD_URL} with password {SECRET}"
    assert SECRET not in safety.redact(message, target)
    assert "postgresql://" not in safety.redact(message, target)


# ── The live connection's real identity ─────────────────────────────────────


def _select_list(sql: str) -> list[str]:
    """Split a SELECT list on top-level commas (commas inside parentheses stay put)."""
    body = sql.split("SELECT", 1)[1].split("\nFROM pg_roles", 1)[0]
    items = []
    depth = 0
    current = ""
    for character in body:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        if character == "," and depth == 0:
            items.append(current.strip())
            current = ""
        else:
            current += character
    items.append(current.strip())
    return items


def test_the_identity_query_columns_line_up_with_the_field_names():
    expected_fragments = [
        "current_database()", "session_user", "current_user", "inet_server_addr()", "inet_server_port()",
        "rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolreplication",
        "pg_auth_members", "datdba", "shobj_description",
    ]
    columns = _select_list(safety._IDENTITY_SQL)
    assert len(columns) == len(safety._IDENTITY_FIELDS) == len(expected_fragments)
    for column, fragment in zip(columns, expected_fragments):
        assert fragment in column, (column, fragment)


def test_a_matching_identity_has_no_problems():
    identity = dict(zip(safety._IDENTITY_FIELDS, GOOD_IDENTITY))
    assert safety.identity_problems(identity, make_target()) == []


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
    identity = dict(zip(safety._IDENTITY_FIELDS, GOOD_IDENTITY))
    identity[field] = value
    assert any(expected in problem for problem in safety.identity_problems(identity, make_target()))


def test_identity_is_verified_read_only_and_leaves_no_transaction_open():
    connection = FakeConnection(FakeLockServer(), GOOD_IDENTITY)
    safety.verify_live_connection(connection, make_target())
    assert connection.executed[0] == "SET TRANSACTION READ ONLY"
    assert connection.rollbacks == 1 and _only_reads(connection)


# ── One workflow at a time ──────────────────────────────────────────────────


def test_every_lock_statement_uses_the_documented_two_integer_key():
    assert (safety.EXCLUSIVE_LOCK_CLASS_KEY, safety.EXCLUSIVE_LOCK_OBJECT_KEY) == _DOCUMENTED_KEY
    assert "xact" not in safety._TRY_LOCK_SQL
    assert "locks.database" in safety._LOCK_HOLDERS_SQL
    server, opened = FakeLockServer(), []
    session = safety.ExclusiveSession(make_target(), "pytest", connect=fake_connect(server, opened))
    session.acquire()
    session.assert_held()
    session.release()
    assert opened[0].lock_keys and set(opened[0].lock_keys) == {_DOCUMENTED_KEY}


@pytest.mark.parametrize("first, second", [("pytest", "pytest"), ("pytest", "migrate"), ("migrate", "pytest")])
def test_a_second_workflow_is_refused_before_it_writes_anything(first, second):
    server, opened = FakeLockServer(), []
    holder = safety.ExclusiveSession(make_target(), first, connect=fake_connect(server, opened))
    holder.acquire()
    contender = safety.ExclusiveSession(make_target(), second, connect=fake_connect(server, opened))
    with pytest.raises(safety.DatabaseTargetBusy, match=f"pid {opened[0].pid}"):
        contender.acquire()
    assert not contender.active and opened[1].closed and _only_reads(opened[1])
    assert server.holder() == opened[0].pid


def test_a_busy_lock_is_reported_as_busy_even_when_the_holder_cannot_be_named():
    server, opened = FakeLockServer(), []
    safety.ExclusiveSession(make_target(), "pytest", connect=fake_connect(server, opened)).acquire()
    server.fail_holder_query = True
    with pytest.raises(safety.DatabaseTargetBusy, match="holder not visible"):
        safety.ExclusiveSession(make_target(), "migrate", connect=fake_connect(server, opened)).acquire()


def test_a_driver_error_while_verifying_is_a_credential_free_refusal_and_closes_the_connection():
    server, opened = FakeLockServer(), []
    server.fail_identity_query = True
    with pytest.raises(safety.DatabaseTargetRejected) as refused:
        safety.ExclusiveSession(make_target(), "pytest", connect=fake_connect(server, opened)).acquire()
    assert opened[0].closed and server.holder() is None and SECRET not in str(refused.value)


def test_the_lock_is_released_after_success_and_after_failure():
    server, opened = FakeLockServer(), []
    session = safety.ExclusiveSession(make_target(), "pytest", connect=fake_connect(server, opened))
    with pytest.raises(RuntimeError):
        session.acquire()
        try:
            raise RuntimeError("a fixture failed mid-run")
        finally:
            session.release()
    assert server.holder() is None and opened[0].closed
    session.acquire()
    session.release()
    session.release()
    assert server.holder() is None and opened[1].closed


def test_a_wrong_live_target_is_refused_and_takes_no_lock():
    server, opened = FakeLockServer(), []
    wrong = ("kerno_dev",) + GOOD_IDENTITY[1:]
    session = safety.ExclusiveSession(make_target(), "pytest", connect=fake_connect(server, opened, wrong))
    with pytest.raises(safety.DatabaseTargetRejected, match="kerno_dev"):
        session.acquire()
    assert server.holder() is None and opened[0].closed
    assert not any("advisory_lock" in text for text in opened[0].executed)


def test_losing_the_lock_or_the_guard_connection_is_detected_and_marks_the_session_inactive():
    server, opened = FakeLockServer(), []
    session = safety.ExclusiveSession(make_target(), "pytest", connect=fake_connect(server, opened))
    session.acquire()
    session.assert_held()
    server.locks.clear()
    with pytest.raises(safety.DatabaseExclusivityLost):
        session.assert_held()
    assert not session.active
    session.release()
    session.acquire()
    opened[1].closed = True
    with pytest.raises(safety.DatabaseExclusivityLost):
        session.assert_held()


def test_an_unreachable_target_is_refused_without_the_password():
    def refuse(url, application_name=None):
        raise psycopg2.OperationalError(f"could not connect using {GOOD_URL} password {SECRET}")

    with pytest.raises(safety.DatabaseTargetRejected) as refused:
        safety.ExclusiveSession(make_target(), "pytest", connect=refuse).acquire()
    assert SECRET not in str(refused.value)


def test_the_process_session_is_acquired_once_rechecked_every_time_and_released():
    server, opened = FakeLockServer(), []
    state = safety.ProcessState(target=make_target())
    first = safety.ensure_exclusive_session("pytest", state=state, connect=fake_connect(server, opened))
    again = safety.ensure_exclusive_session("pytest", state=state)
    assert first is again and len(opened) == 1
    assert sum("pid = pg_backend_pid()" in text for text in opened[0].executed) == 2
    safety.release_exclusive_session(state)
    assert state.session is None and server.holder() is None


def test_no_session_is_opened_without_a_valid_target():
    with pytest.raises(safety.DatabaseTargetNotConfigured):
        safety.ensure_exclusive_session("pytest", state=safety.ProcessState())
    with pytest.raises(safety.DatabaseTargetInvalid, match="bad settings"):
        safety.ensure_exclusive_session("pytest", state=safety.ProcessState(problem="bad settings"))


# ── The connection guard every psycopg2.connect goes through ────────────────


def test_psycopg2_connect_is_the_guard_in_this_process():
    assert psycopg2.connect is safety._guarded_connect


def test_with_no_target_every_connection_path_is_refused_before_libpq(monkeypatch):
    from sqlalchemy import create_engine

    monkeypatch.setattr(safety, "_STATE", safety.ProcessState())
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    attempts = [
        lambda: psycopg2.connect(FAKE_DEVELOPMENT_URL),
        lambda: psycopg2.connect(""),
        lambda: psycopg2.connect(),
        lambda: psycopg2.connect(host="127.0.0.1", port=1, dbname="kerno_dev", user="kerno_dev"),
        lambda: create_engine(FAKE_DEVELOPMENT_URL).connect(),
        lambda: psycopg2.pool.ThreadedConnectionPool(1, 1, dsn=FAKE_DEVELOPMENT_URL),
    ]
    for attempt in attempts:
        with pytest.raises(safety.DatabaseTargetNotConfigured):
            attempt()


def test_with_a_target_but_no_held_session_even_the_target_is_refused(monkeypatch):
    monkeypatch.setattr(safety, "_STATE", safety.ProcessState(target=make_target()))
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    with pytest.raises(safety.DatabaseTargetRejected, match="outside the exclusive session"):
        psycopg2.connect(GOOD_URL)


def test_every_new_connection_reproves_the_lock_and_none_is_allowed_after_it_is_lost():
    server, opened = FakeLockServer(), []
    state = safety.ProcessState(target=make_target())
    safety.ensure_exclusive_session("pytest", state=state, connect=fake_connect(server, opened))
    safety.authorize_connection(GOOD_URL, {}, state)
    checks_before = sum("pid = pg_backend_pid()" in text for text in opened[0].executed)
    safety.authorize_connection(GOOD_URL, {}, state)
    assert sum("pid = pg_backend_pid()" in text for text in opened[0].executed) == checks_before + 1
    server.locks.clear()
    with pytest.raises(safety.DatabaseExclusivityLost):
        safety.authorize_connection(GOOD_URL, {}, state)
    with pytest.raises(safety.DatabaseTargetRejected, match="outside the exclusive session"):
        safety.authorize_connection(GOOD_URL, {}, state)
    safety.release_exclusive_session(state)


def test_a_redirecting_libpq_variable_set_after_bootstrap_is_refused_at_connect_time(monkeypatch):
    state = safety.ProcessState(target=make_target(), session=_HeldSession())
    monkeypatch.setenv("PGHOSTADDR", "10.0.0.5")
    with pytest.raises(safety.DatabaseTargetInvalid, match="PGHOSTADDR"):
        safety.authorize_connection(GOOD_URL, {}, state)


@pytest.mark.parametrize(
    "dsn, keywords",
    [
        (FAKE_DEVELOPMENT_URL, {}),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test?dbname=kerno_dev", {}),
        (f"postgresql://kerno_test:{SECRET}@127.0.0.1:5433/kerno_test", {}),
        (None, {"host": "127.0.0.1", "port": 5432, "dbname": "kerno_test", "user": "postgres"}),
        (GOOD_URL, {"options": "-c role=postgres"}),
        ("", {}),
    ],
)
def test_inside_the_session_only_the_approved_target_is_allowed(dsn, keywords):
    state = safety.ProcessState(target=make_target(), session=_HeldSession())
    with pytest.raises(safety.KernoTestDatabaseError) as refused:
        safety.authorize_connection(dsn, keywords, state)
    assert SECRET not in str(refused.value)
    safety.authorize_connection(GOOD_URL, {}, state)
    safety.authorize_connection(
        None, {"host": "127.0.0.1", "port": 5432, "dbname": "kerno_test", "user": "kerno_test", "password": "x"}, state
    )


_LOW_LEVEL_ENTRY_POINTS = re.compile(
    r"psycopg2\._connect\(|_psycopg\._connect\(|extensions\.connection\("
    r"|\b(Logging|MinTimeLogging|RealDict|NamedTuple|Dict|LogicalReplication|PhysicalReplication)Connection\("
    r"|\bcreator\s*=\s*[A-Za-z_]"
    r"|^\s*(import|from)\s+(psycopg|asyncpg|pg8000)\b(?!2)",
    re.MULTILINE,
)


def test_no_code_uses_a_connection_entry_point_the_guard_does_not_cover():
    offenders = []
    for folder in ("src", "tests", "scripts", "migrations"):
        for path in (safety.REPOSITORY_ROOT / folder).rglob("*.py"):
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue
            if _LOW_LEVEL_ENTRY_POINTS.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(safety.REPOSITORY_ROOT)))
    assert offenders == []


# ── Bootstrap ───────────────────────────────────────────────────────────────


def _bootstrap(environ: dict, tmp_path, loaded_modules=None) -> safety.ProcessState:
    """Bootstrap a fresh state against a fake environment with no .env.test on disk."""
    environ.setdefault(safety.TEST_ENV_FILE_VARIABLE, str(tmp_path / "absent.env.test"))
    return safety.bootstrap_test_process(environ, safety.ProcessState(), loaded_modules or {})


def test_an_inherited_database_url_is_removed_and_never_becomes_authorisation(tmp_path):
    environ = {"DATABASE_URL": FAKE_DEVELOPMENT_URL}
    state = _bootstrap(environ, tmp_path)
    assert "DATABASE_URL" not in environ
    assert state.target is None and state.problem is None
    assert environ["PYTHON_DOTENV_DISABLED"] == "1"


def test_a_valid_target_becomes_the_process_database_url(tmp_path):
    environ = {"KERNO_TEST_DATABASE_URL": GOOD_URL, "KERNO_TEST_DATABASE_APPROVAL": GOOD_APPROVAL}
    state = _bootstrap(environ, tmp_path)
    assert state.target.identity == GOOD_APPROVAL and environ["DATABASE_URL"] == GOOD_URL


def test_invalid_settings_are_recorded_as_a_problem_and_adopt_nothing(tmp_path):
    environ = {
        "DATABASE_URL": FAKE_DEVELOPMENT_URL,
        "KERNO_TEST_DATABASE_URL": FAKE_DEVELOPMENT_URL,
        "KERNO_TEST_DATABASE_APPROVAL": GOOD_APPROVAL,
    }
    state = _bootstrap(environ, tmp_path)
    assert state.target is None and "kerno_test" in state.problem and SECRET not in state.problem
    assert "DATABASE_URL" not in environ


def test_a_bootstrap_after_the_application_was_imported_is_refused(tmp_path):
    state = _bootstrap({}, tmp_path, loaded_modules={"src.api.app": object(), "pytest": object()})
    assert "src.api.app" in state.problem


def test_the_migration_bootstrap_requires_settings_and_ignores_an_inherited_database_url(tmp_path):
    environ = {"DATABASE_URL": FAKE_DEVELOPMENT_URL, safety.TEST_ENV_FILE_VARIABLE: str(tmp_path / "absent")}
    with pytest.raises(safety.DatabaseTargetNotConfigured, match="requires KERNO_TEST_DATABASE_URL"):
        safety.prepare_migration_process(environ, safety.ProcessState(), {})
    assert "DATABASE_URL" not in environ


def test_dotenv_loading_stays_disabled_in_this_test_process_after_the_app_is_imported():
    import src.api.app  # noqa: F401

    assert dotenv.load_dotenv(stream=io.StringIO("KERNO_SAFETY_PROBE=1")) is False
    assert "KERNO_SAFETY_PROBE" not in os.environ
    live = safety.current_state().target
    expected = live.url if live else None
    # A boolean, not an equality of values: a failure must not print a URL.
    assert (os.environ.get("DATABASE_URL") == expected), "DATABASE_URL is not the validated test target"


# ── The fixture and hooks: stop, re-prove, release ──────────────────────────


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
        (safety.DatabaseExclusivityLost("lost"), pytest.ExitCode.INTERRUPTED),
        (safety.DatabaseTargetRejected("wrong database"), pytest.ExitCode.USAGE_ERROR),
    ],
)
def test_a_busy_lost_or_rejected_target_stops_the_run_before_any_fixture_write(monkeypatch, error, code):
    def refuse(purpose, state=None, connect=None):
        raise error

    monkeypatch.setattr(safety, "ensure_exclusive_session", refuse)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    outcome = _outcome_of(
        lambda: conftest.open_guarded_fixture_connection(_Config(), state=safety.ProcessState(target=make_target()))
    )
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == code


def test_with_no_target_the_fixture_skips_or_fails_when_live_is_required():
    skipped = _outcome_of(lambda: conftest.open_guarded_fixture_connection(_Config(), state=safety.ProcessState()))
    assert isinstance(skipped, pytest.skip.Exception) and "no approved test database" in str(skipped)
    failed = _outcome_of(
        lambda: conftest.open_guarded_fixture_connection(_Config(require_live=True), state=safety.ProcessState())
    )
    assert isinstance(failed, pytest.fail.Exception), f"expected a failure, got {failed!r}"
    assert "live database required" in str(failed)


def test_the_fixture_proves_exclusivity_before_its_seed_and_before_its_cleanup(monkeypatch):
    events: list[str] = []

    class _RawConnection:
        autocommit = True

        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    def open_connection(config):
        events.append("open")
        return _RawConnection()

    monkeypatch.setattr(conftest, "open_guarded_fixture_connection", open_connection)
    monkeypatch.setattr(conftest, "require_exclusive_session", lambda state=None: events.append("prove"))
    monkeypatch.setattr(conftest, "_teardown_seed_data", lambda conn: events.append("cleanup"))
    monkeypatch.setattr(conftest, "_seed_integration_data", lambda conn: events.append("seed"))
    fixture = conftest.db_connection.__wrapped__(types.SimpleNamespace(config=_Config()))
    next(fixture)
    events.append("test body")
    with pytest.raises(StopIteration):
        next(fixture)
    assert [event for event in events if event != "commit"] == [
        "open", "prove", "cleanup", "seed", "test body", "rollback", "prove", "cleanup", "close",
    ]


def test_cleanup_stops_the_run_when_exclusivity_cannot_be_proved():
    server, opened = FakeLockServer(), []
    state = safety.ProcessState(target=make_target())
    assert isinstance(_outcome_of(lambda: conftest.require_exclusive_session(state)), pytest.exit.Exception)
    safety.ensure_exclusive_session("pytest", state=state, connect=fake_connect(server, opened))
    assert _outcome_of(lambda: conftest.require_exclusive_session(state)) is None
    server.locks.clear()
    assert isinstance(_outcome_of(lambda: conftest.require_exclusive_session(state)), pytest.exit.Exception)
    safety.release_exclusive_session(state)


def test_the_session_end_reproves_the_lock_and_a_loss_is_not_a_success(monkeypatch):
    server, opened = FakeLockServer(), []
    state = safety.ProcessState(target=make_target())
    safety.ensure_exclusive_session("pytest", state=state, connect=fake_connect(server, opened))
    monkeypatch.setattr(conftest, "_TEST_DATABASE", state)
    monkeypatch.setattr(conftest, "_FINAL_EXCLUSIVITY_PROBLEMS", [])
    held = types.SimpleNamespace(exitstatus=pytest.ExitCode.OK)
    conftest.pytest_sessionfinish(held, pytest.ExitCode.OK)
    assert held.exitstatus == pytest.ExitCode.OK
    server.locks.clear()
    lost = types.SimpleNamespace(exitstatus=pytest.ExitCode.OK)
    conftest.pytest_sessionfinish(lost, pytest.ExitCode.OK)
    assert lost.exitstatus == pytest.ExitCode.INTERRUPTED and conftest._FINAL_EXCLUSIVITY_PROBLEMS
    safety.release_exclusive_session(state)


def test_a_required_live_run_in_which_an_integration_test_skipped_fails(monkeypatch):
    monkeypatch.setattr(conftest, "_TEST_DATABASE", safety.ProcessState())
    monkeypatch.setattr(conftest, "_REQUIRE_LIVE", {"enabled": True})
    monkeypatch.setattr(conftest, "_LIVE_TEST_SKIPS", [])
    conftest.pytest_runtest_logreport(types.SimpleNamespace(skipped=True, keywords={"integration": 1}, nodeid="live"))
    conftest.pytest_runtest_logreport(types.SimpleNamespace(skipped=True, keywords={}, nodeid="unit"))
    assert conftest._LIVE_TEST_SKIPS == ["live"]
    session = types.SimpleNamespace(exitstatus=pytest.ExitCode.OK)
    conftest.pytest_sessionfinish(session, pytest.ExitCode.OK)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_parallel_workers_are_detected_from_options_and_worker_environment(monkeypatch):
    def config(workers=None, dist="no"):
        return types.SimpleNamespace(option=types.SimpleNamespace(numprocesses=workers, dist=dist))

    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert not conftest._parallel_workers_requested(config())
    assert conftest._parallel_workers_requested(config(workers=4))
    assert conftest._parallel_workers_requested(config(dist="loadfile"))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    assert conftest._parallel_workers_requested(config())


def test_unconfigure_releases_the_lock_and_its_connection(monkeypatch):
    server, opened = FakeLockServer(), []
    state = safety.ProcessState(target=make_target())
    safety.ensure_exclusive_session("pytest", state=state, connect=fake_connect(server, opened))
    monkeypatch.setattr(safety, "_STATE", state)
    conftest.pytest_unconfigure(None)
    assert state.session is None and server.holder() is None and opened[0].closed
