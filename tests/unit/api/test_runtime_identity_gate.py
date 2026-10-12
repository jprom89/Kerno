"""Offline tests for the API's startup runtime-identity gate (src/api/runtime_identity.py).

What:  Checks that identity_problems names every owner or elevated property;
       that enforce_runtime_identity refuses each problem, the owner's
       KERNO_MIGRATION_DATABASE_URL and an unreadable identity outside
       development and only warns in development, never echoing a URL or
       secret; that read_connection_identity reads read-only from the app pool
       and always rolls back and returns the connection; and that the app
       lifespan runs the gate after the ALLOWED_ORIGINS check.
Why:   C2 (PILOT-READY-002, docs/pilot_readiness_kickoff.md §1.6). Row-level
       security binds only a non-owner login, so the API must refuse to start
       as anything else. Every reader and pool here is a fake; nothing
       connects to a database.
How:   pytest tests/unit/api/test_runtime_identity_gate.py -v
"""

from __future__ import annotations

import dataclasses
import logging
import os

import psycopg2
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")

from config.constants import EXAMPLE_ALLOWED_ORIGIN
from src.api import dependencies, runtime_identity
from src.api.app import create_app

SECRET = "gate-secret-zz"
OWNER_URL = f"postgresql://kerno_test:{SECRET}@127.0.0.1:5432/kerno_test"
_GATE_LOGGER = "src.api.runtime_identity"

RESTRICTED = runtime_identity.ConnectionIdentity(
    session_user="kerno_test_app", superuser=False, bypass_rls=False, create_role=False,
    create_database=False, replication=False, memberships=0, owns_database=False, owned_objects=0,
)

# One elevated or owning property at a time, and the phrase the problem must contain.
ELEVATIONS = [
    pytest.param({"superuser": True}, "superuser", id="superuser"),
    pytest.param({"bypass_rls": True}, "bypass_rls", id="bypass_rls"),
    pytest.param({"create_role": True}, "create_role", id="create_role"),
    pytest.param({"create_database": True}, "create_database", id="create_database"),
    # An otherwise restricted, non-owning login that may stream the whole cluster's WAL.
    pytest.param({"replication": True}, "replication", id="replication"),
    pytest.param({"memberships": 1}, "member of another role", id="membership"),
    pytest.param({"owns_database": True}, "owns the database", id="owns_database"),
    pytest.param({"owned_objects": 3}, "owns database objects", id="owned_objects"),
]

READ_FAILURES = [
    pytest.param(psycopg2.OperationalError(f"could not connect to {OWNER_URL}"), "OperationalError", id="driver"),
    pytest.param(RuntimeError(f"pool exhausted for password {SECRET}"), "RuntimeError", id="runtime"),
]


def _reader_returning(identity):
    """A stand-in for read_connection_identity that returns identity."""
    def read_identity():
        """Return the scripted identity."""
        return identity

    return read_identity


def _reader_raising(error):
    """A stand-in for read_connection_identity that raises error."""
    def read_identity():
        """Raise the scripted error."""
        raise error

    return read_identity


def _gate_warnings(caplog) -> list[logging.LogRecord]:
    """The warnings the gate logged."""
    return [record for record in caplog.records if record.name == _GATE_LOGGER and record.levelno == logging.WARNING]


def _refusal(development: bool, reader) -> str:
    """Run the gate outside development and return its refusal message."""
    with pytest.raises(RuntimeError) as refused:
        runtime_identity.enforce_runtime_identity(development=development, read_identity=reader)
    return str(refused.value)


@pytest.fixture
def no_owner_url(monkeypatch):
    """Remove the owner URL the test bootstrap may have pinned, so each test states its own."""
    monkeypatch.delenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, raising=False)
    return monkeypatch


# ── identity_problems ───────────────────────────────────────────────────────


def test_a_restricted_identity_has_no_problems():
    assert runtime_identity.identity_problems(RESTRICTED) == []


@pytest.mark.parametrize("change, phrase", ELEVATIONS)
def test_each_elevated_or_owning_property_is_named(change, phrase):
    problems = runtime_identity.identity_problems(dataclasses.replace(RESTRICTED, **change))
    assert len(problems) == 1
    assert phrase in problems[0]


def test_every_problem_is_named_when_all_are_present():
    everything = dataclasses.replace(
        RESTRICTED, superuser=True, bypass_rls=True, create_role=True, create_database=True,
        replication=True, memberships=2, owns_database=True, owned_objects=9,
    )
    assert len(runtime_identity.identity_problems(everything)) == len(ELEVATIONS)


# ── enforce_runtime_identity ────────────────────────────────────────────────


@pytest.mark.parametrize("change, phrase", ELEVATIONS)
def test_each_problem_refuses_startup_outside_development(no_owner_url, change, phrase):
    message = _refusal(False, _reader_returning(dataclasses.replace(RESTRICTED, **change)))
    assert phrase in message and "not a restricted runtime identity" in message


@pytest.mark.parametrize("change, phrase", ELEVATIONS)
def test_each_problem_only_warns_in_development(no_owner_url, caplog, change, phrase):
    caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
    reader = _reader_returning(dataclasses.replace(RESTRICTED, **change))
    assert runtime_identity.enforce_runtime_identity(development=True, read_identity=reader) is None
    warnings = _gate_warnings(caplog)
    assert len(warnings) == 1
    assert phrase in warnings[0].getMessage() and "allowed in development only" in warnings[0].getMessage()


def test_the_owner_url_in_the_environment_refuses_startup_without_echoing_it(no_owner_url):
    no_owner_url.setenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, OWNER_URL)
    message = _refusal(False, _reader_returning(RESTRICTED))
    assert "KERNO_MIGRATION_DATABASE_URL" in message
    assert SECRET not in message and "postgresql://" not in message


def test_the_owner_url_in_the_environment_only_warns_in_development(no_owner_url, caplog):
    no_owner_url.setenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, OWNER_URL)
    caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
    runtime_identity.enforce_runtime_identity(development=True, read_identity=_reader_returning(RESTRICTED))
    assert "KERNO_MIGRATION_DATABASE_URL" in _gate_warnings(caplog)[0].getMessage()
    assert SECRET not in caplog.text and "postgresql://" not in caplog.text


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_owner_url_is_treated_as_absent(no_owner_url, blank):
    no_owner_url.setenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, blank)
    runtime_identity.enforce_runtime_identity(development=False, read_identity=_reader_returning(RESTRICTED))


@pytest.mark.parametrize("error, error_name", READ_FAILURES)
def test_an_unreadable_identity_refuses_startup_without_echoing_the_error_text(no_owner_url, error, error_name):
    message = _refusal(False, _reader_raising(error))
    assert f"could not be read ({error_name})" in message
    assert SECRET not in message and "postgresql://" not in message


@pytest.mark.parametrize("error, error_name", READ_FAILURES)
def test_an_unreadable_identity_only_warns_in_development(no_owner_url, caplog, error, error_name):
    caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
    runtime_identity.enforce_runtime_identity(development=True, read_identity=_reader_raising(error))
    assert f"could not be read ({error_name})" in _gate_warnings(caplog)[0].getMessage()
    assert SECRET not in caplog.text and "postgresql://" not in caplog.text


def test_every_problem_is_collected_into_one_refusal(no_owner_url):
    no_owner_url.setenv(runtime_identity.MIGRATION_DATABASE_URL_VARIABLE, OWNER_URL)
    elevated = dataclasses.replace(RESTRICTED, superuser=True, owned_objects=1)
    message = _refusal(False, _reader_returning(elevated))
    assert "KERNO_MIGRATION_DATABASE_URL" in message
    assert "superuser" in message and "owns database objects" in message


@pytest.mark.parametrize("development", [False, True])
def test_a_restricted_identity_passes_silently(no_owner_url, caplog, development):
    caplog.set_level(logging.DEBUG, logger=_GATE_LOGGER)
    runtime_identity.enforce_runtime_identity(development=development, read_identity=_reader_returning(RESTRICTED))
    assert [record for record in caplog.records if record.name == _GATE_LOGGER] == []


def test_without_an_injected_reader_the_gate_reads_the_pool_identity(no_owner_url):
    no_owner_url.setattr(runtime_identity, "read_connection_identity", _reader_returning(RESTRICTED))
    runtime_identity.enforce_runtime_identity(development=False)


# ── read_connection_identity ────────────────────────────────────────────────


class _FakeCursor:
    """Records each statement; returns the scripted row or raises the scripted error."""

    def __init__(self, connection) -> None:
        """Attach to the scripted connection."""
        self.connection = connection

    def execute(self, sql) -> None:
        """Record the statement, failing the identity query when the script says so."""
        self.connection.calls.append(f"execute:{sql}")
        if sql == runtime_identity._IDENTITY_SQL and self.connection.query_error:
            raise self.connection.query_error

    def fetchone(self):
        """Return the scripted identity row."""
        self.connection.calls.append("fetchone")
        return self.connection.row


class _FakeRawConnection:
    """A pooled connection that records rollback, and can fail the query or the rollback."""

    def __init__(self, calls, row, query_error=None, rollback_error=None) -> None:
        """Script the identity row and any failure."""
        self.calls = calls
        self.row = row
        self.query_error = query_error
        self.rollback_error = rollback_error

    def cursor(self):
        """Return a cursor over this script."""
        return _FakeCursor(self)

    def rollback(self) -> None:
        """Record the rollback, then fail it if scripted."""
        self.calls.append("rollback")
        if self.rollback_error:
            raise self.rollback_error


class _FakePool:
    """Hands out one scripted connection and records its return."""

    def __init__(self, connection) -> None:
        """Hold the one connection this pool lends."""
        self.connection = connection

    def getconn(self):
        """Lend the connection."""
        self.connection.calls.append("getconn")
        return self.connection

    def putconn(self, connection) -> None:
        """Record the connection's return."""
        assert connection is self.connection
        self.connection.calls.append("putconn")


# Columns in _IDENTITY_SQL order: session_user, rolsuper, rolbypassrls,
# rolcreaterole, rolcreatedb, rolreplication, memberships, owns database, owned objects.
_IDENTITY_ROW = ("kerno_test_app", False, False, False, False, False, 0, False, 0)
_REPLICATION_ROW = ("kerno_test_app", False, False, False, False, True, 0, False, 0)


def _lend(monkeypatch, connection) -> None:
    """Make dependencies._get_pool() return a pool over connection."""
    pool = _FakePool(connection)

    def get_pool():
        """Return the fake pool."""
        return pool

    monkeypatch.setattr(dependencies, "_get_pool", get_pool)


def test_read_connection_identity_reads_read_only_and_returns_the_connection(monkeypatch):
    connection = _FakeRawConnection([], _IDENTITY_ROW)
    _lend(monkeypatch, connection)
    assert runtime_identity.read_connection_identity() == RESTRICTED
    assert connection.calls == [
        "getconn", "execute:SET TRANSACTION READ ONLY", f"execute:{runtime_identity._IDENTITY_SQL}",
        "fetchone", "rollback", "putconn",
    ]


def test_the_replication_column_is_read_into_the_replication_attribute_alone(monkeypatch):
    _lend(monkeypatch, _FakeRawConnection([], _REPLICATION_ROW))
    identity = runtime_identity.read_connection_identity()
    assert identity == dataclasses.replace(RESTRICTED, replication=True)
    assert runtime_identity.identity_problems(identity) == ["the login has the replication attribute"]


def test_a_failing_identity_query_still_rolls_back_and_returns_the_connection(monkeypatch):
    connection = _FakeRawConnection([], _IDENTITY_ROW, query_error=psycopg2.OperationalError("server gone"))
    _lend(monkeypatch, connection)
    with pytest.raises(psycopg2.OperationalError):
        runtime_identity.read_connection_identity()
    assert connection.calls[-2:] == ["rollback", "putconn"]
    assert "fetchone" not in connection.calls


def test_a_failing_rollback_still_returns_the_connection(monkeypatch):
    closed = psycopg2.InterfaceError("connection already closed")
    connection = _FakeRawConnection([], _IDENTITY_ROW, rollback_error=closed)
    _lend(monkeypatch, connection)
    with pytest.raises(psycopg2.InterfaceError):
        runtime_identity.read_connection_identity()
    assert connection.calls[-2:] == ["rollback", "putconn"]


def test_a_missing_catalog_row_is_an_error_after_the_connection_is_returned(monkeypatch):
    connection = _FakeRawConnection([], None)
    _lend(monkeypatch, connection)
    with pytest.raises(RuntimeError, match="could not be read"):
        runtime_identity.read_connection_identity()
    assert connection.calls[-2:] == ["rollback", "putconn"]


def test_the_identity_query_is_a_single_catalog_read():
    query = " ".join(runtime_identity._IDENTITY_SQL.split())
    assert query.startswith("SELECT session_user,")
    assert ";" not in query
    for catalog in ("pg_roles", "pg_auth_members", "pg_shdepend", "pg_database"):
        assert catalog in query
    assert "roles.rolcreatedb, roles.rolreplication," in query
    assert "dbid IN (0, databases.oid)" in query


# ── The app lifespan runs the gate ──────────────────────────────────────────
#
# `with TestClient(...)` is what runs the lifespan; a bare TestClient never would.


def _no_real_pool():
    """Stands in for the application pool: a gate test must never reach a real one."""
    raise AssertionError("the identity gate test reached the application pool")


@pytest.fixture
def env(monkeypatch):
    """Clear every variable the lifespan reads so each test states its own environment."""
    for name in ("KERNO_ENV", "FRONTEND_URL", "ALLOWED_ORIGINS", "KERNO_ENABLE_DOCS", "KERNO_MIGRATION_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KERNO_JWT_SECRET", "test-secret-for-unit-tests")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/for-startup-checks-only")
    monkeypatch.setattr(dependencies, "_get_pool", _no_real_pool)
    return monkeypatch


def test_startup_outside_development_refuses_an_elevated_login(env):
    env.setenv("KERNO_ENV", "production")
    superuser = dataclasses.replace(RESTRICTED, superuser=True)
    env.setattr(runtime_identity, "read_connection_identity", _reader_returning(superuser))
    with pytest.raises(RuntimeError, match="superuser"):
        with TestClient(create_app()):
            pass


def test_startup_outside_development_refuses_the_owner_url(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("KERNO_MIGRATION_DATABASE_URL", OWNER_URL)
    env.setattr(runtime_identity, "read_connection_identity", _reader_returning(RESTRICTED))
    with pytest.raises(RuntimeError, match="KERNO_MIGRATION_DATABASE_URL") as refused:
        with TestClient(create_app()):
            pass
    assert SECRET not in str(refused.value)


def test_startup_outside_development_refuses_an_unreadable_identity(env):
    env.setenv("KERNO_ENV", "production")
    env.setattr(runtime_identity, "read_connection_identity", _reader_raising(psycopg2.OperationalError(OWNER_URL)))
    with pytest.raises(RuntimeError, match="could not be read") as refused:
        with TestClient(create_app()):
            pass
    assert SECRET not in str(refused.value)


def test_startup_outside_development_accepts_a_restricted_login(env):
    env.setenv("KERNO_ENV", "production")
    env.setattr(runtime_identity, "read_connection_identity", _reader_returning(RESTRICTED))
    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 200


def test_startup_in_development_warns_about_an_elevated_login_and_starts(env, caplog):
    env.setenv("KERNO_ENV", "development")
    caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
    database_owner = dataclasses.replace(RESTRICTED, owns_database=True)
    env.setattr(runtime_identity, "read_connection_identity", _reader_returning(database_owner))
    with TestClient(create_app()) as client:
        assert client.get("/").status_code in (200, 302)
    assert "owns the database" in _gate_warnings(caplog)[0].getMessage()


def test_the_gate_runs_after_the_allowed_origins_check(env):
    reads: list[str] = []

    def read_identity():
        """Record the read; the origins check must have refused first."""
        reads.append("read")
        return RESTRICTED

    env.setenv("KERNO_ENV", "production")
    env.setenv("ALLOWED_ORIGINS", f"https://app.example,{EXAMPLE_ALLOWED_ORIGIN}")
    env.setattr(runtime_identity, "read_connection_identity", read_identity)
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        with TestClient(create_app()):
            pass
    assert reads == []


def test_the_gate_runs_once_at_startup_with_valid_origins(env):
    reads: list[str] = []

    def read_identity():
        """Record the read."""
        reads.append("read")
        return RESTRICTED

    env.setenv("KERNO_ENV", "production")
    env.setenv("ALLOWED_ORIGINS", "https://app.example")
    env.setattr(runtime_identity, "read_connection_identity", read_identity)
    with TestClient(create_app()):
        pass
    assert reads == ["read"]
