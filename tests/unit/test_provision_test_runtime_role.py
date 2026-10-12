"""Offline proof of scripts/provision_test_runtime_role.py — the administrator-run kerno_test_app setup.

What:  Drives the runtime-role provisioning script against an in-memory fake
       server and proves:
       - it refuses before asking for any password when .env.test.runtime
         exists, a redirecting libpq variable or a runtime setting is in the
         environment, the file would be tracked by git, or the arguments are
         wrong;
       - it refuses, changing nothing, an empty administrator password, a
         failed login, a non-superuser or non-loopback administrator
         connection, a missing or altered owner target, PUBLIC CONNECT on
         kerno_test, an existing kerno_test_app, and a declined confirmation;
       - a full run makes exactly one change, the approved CREATE ROLE carrying
         a SCRAM verifier, after writing .env.test.runtime with exclusive
         create, and then verifies the role from the catalog;
       - the file holds exactly the runtime pair the test guard accepts, and
         the password appears nowhere else;
       - it opens one administrator connection, to postgres, and never logs in
         as kerno_test_app, grants, alters or drops anything, or contacts
         kerno_dev;
       - on a partial failure it reports what exists and the owner's decision,
         and deletes nothing;
       - it never touches the test connection guard.
Why:   The script runs once, with administrator rights, on the owner's
       machine. Its safety properties have to be shown before that run, not
       discovered during it.
How:   pytest tests/unit/test_provision_test_runtime_role.py -v
"""

from __future__ import annotations

import ast
import base64
import builtins
import dataclasses
import importlib.util
import pathlib
import re
import subprocess
import sys

import psycopg2
import pytest
from psycopg2 import sql

from tests import _database_safety as safety

_SCRIPT = safety.REPOSITORY_ROOT / "scripts" / "provision_test_runtime_role.py"
_FILE_NAME = ".env.test.runtime"
_ADMIN_SECRET = "fake-admin-secret-zz"
_OWNER_TARGET = safety.DatabaseTarget(
    host="127.0.0.1", port=5432, dbname="kerno_test", user="kerno_test",
    url="postgresql://kerno_test:fake-owner-secret@127.0.0.1:5432/kerno_test",
)
# The role exactly as CREATE ROLE should leave it, written out here rather than
# taken from the script, so a wrong expectation in the script fails a test.
_HEALTHY_ROLE = {
    "can_login": True, "superuser": False, "create_database": False, "create_role": False,
    "replication": False, "bypass_rls": False, "inherit": False, "memberships": 0,
    "owned_objects": 0, "can_connect": False,
}
_CREATE_ROLE_PATTERN = re.compile(
    r'^CREATE ROLE "kerno_test_app" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS '
    r"NOINHERIT PASSWORD 'SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+'$"
)
_VERIFIER_PATTERN = re.compile(r"SCRAM-SHA-256\$[A-Za-z0-9+/=:$]+")


def _load_script():
    """Import the runtime provisioning script by path."""
    spec = importlib.util.spec_from_file_location("provision_test_runtime_role", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered first: dataclasses resolve the script's string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RUNTIME = _load_script()


def _text(statement) -> str:
    """Render a psycopg2.sql composition (or a plain string) offline; the owner script's test has the same helper."""
    if isinstance(statement, str):
        return statement
    if isinstance(statement, sql.Composed):
        return "".join(_text(part) for part in statement.seq)
    if isinstance(statement, sql.SQL):
        return statement.string
    if isinstance(statement, sql.Identifier):
        return ".".join(f'"{name}"' for name in statement.strings)
    if isinstance(statement, sql.Literal):
        return repr(statement.wrapped)
    raise AssertionError(f"unexpected composable {statement!r}")


@dataclasses.dataclass
class _FakeServer:
    """Catalog answers, injected faults, and a log of every statement and connection."""

    server_row: tuple = ("127.0.0.1", 5432, True, "PostgreSQL 18.4")
    owner_role_row: tuple | None = (True,)
    database_row: tuple | None = ("kerno_test", safety.DISPOSABLE_DATABASE_COMMENT, False)
    runtime_role_row: tuple | None = None
    role_state: dict = dataclasses.field(default_factory=dict)
    fail_on_create: bool = False
    create_status: str = "CREATE ROLE"
    fail_on_verification: bool = False
    stores_other_password: bool = False
    env_file: pathlib.Path | None = None
    statements: list = dataclasses.field(default_factory=list)
    connections: list = dataclasses.field(default_factory=list)
    stored_verifier: str | None = None
    file_at_create: str | None = None
    autocommit_at_create: bool | None = None

    def answer(self, text: str, params):
        """Return the canned row for one of the script's catalog reads."""
        if text == RUNTIME.SERVER_SQL:
            return self.server_row
        if text == RUNTIME.OWNER_ROLE_SQL:
            return self.owner_role_row
        if text == RUNTIME.OWNER_DATABASE_SQL:
            return self.database_row
        if text == RUNTIME.EXISTING_RUNTIME_ROLE_SQL:
            return self.runtime_role_row
        if text == RUNTIME.VERIFICATION_SQL:
            return self.verified_role(params)
        raise AssertionError(f"unexpected statement: {text[:70]}")

    def create_role(self, statement, connection) -> str:
        """Record what existed when CREATE ROLE arrived, keep the verifier it carried, and return its status."""
        self.autocommit_at_create = connection.autocommit
        if self.env_file is not None and self.env_file.exists():
            self.file_at_create = self.env_file.read_text(encoding="utf-8")
        if self.fail_on_create:
            raise psycopg2.Error(f"role creation failed\nLINE 1: {_text(statement)}")
        self.stored_verifier = next(part.wrapped for part in statement.seq if isinstance(part, sql.Literal))
        return self.create_status

    def verified_role(self, params) -> tuple:
        """The catalog's view of the new role: healthy unless a test overrides part of it."""
        if self.fail_on_verification:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")
        state = dict(_HEALTHY_ROLE)
        state["stored_verifier_matches"] = params["verifier"] == self.stored_verifier and not self.stores_other_password
        state.update(self.role_state)
        return tuple(state[field] for field in RUNTIME.VERIFICATION_FIELDS)


@dataclasses.dataclass
class _FakeConnection:
    """A connection to one database as one role on the fake server."""

    server: _FakeServer
    keywords: dict
    autocommit: bool = False
    closed: bool = False

    def cursor(self):
        """Open a cursor on this connection."""
        return _FakeCursor(self)

    def close(self) -> None:
        """Mark the connection closed."""
        self.closed = True


@dataclasses.dataclass
class _FakeCursor:
    """Executes against the fake server; CREATE ROLE gets a command status, reads get canned rows."""

    connection: _FakeConnection
    statusmessage: str | None = None
    row: tuple | None = None

    def execute(self, statement, params=None) -> None:
        """Log the statement, then answer it."""
        server = self.connection.server
        text = _text(statement)
        server.statements.append((self.connection.keywords["dbname"], text, params))
        if text.startswith("CREATE ROLE"):
            self.statusmessage = server.create_role(statement, self.connection)
        else:
            self.row = server.answer(text, params)

    def fetchone(self):
        """Return the canned row of the last read."""
        return self.row


@dataclasses.dataclass
class _Outcome:
    """What one run returned and what it asked for."""

    code: int
    env_file: pathlib.Path
    prompts: list


def _connector(server: _FakeServer):
    """A connect function that opens fake connections and records their keyword arguments."""
    def connect(**keywords):
        """Open one fake connection."""
        connection = _FakeConnection(server, keywords)
        server.connections.append(connection)
        return connection
    return connect


def _run(tmp_path, server=None, answer="yes", secret=_ADMIN_SECRET, environ=None, argv=(), ask=None):
    """Run the script's entry point with every dependency faked."""
    env_file = tmp_path / _FILE_NAME
    server = server or _FakeServer()
    server.env_file = env_file
    prompts: list[str] = []

    def read_secret(prompt):
        """Record the hidden-prompt request and return the fake administrator password."""
        prompts.append(prompt)
        return secret

    def ask_owner(prompt):
        """Record the confirmation request and return the canned answer."""
        prompts.append(prompt)
        return answer

    code = RUNTIME.run(
        list(argv), environ=environ or {}, connect=_connector(server), read_secret=read_secret,
        ask=ask or ask_owner, env_file=env_file, is_ignored=lambda path: True,
    )
    return _Outcome(code, env_file, prompts)


def _writes(server: _FakeServer) -> list[str]:
    """Every statement that is not a read, in order."""
    return [text for _, text, _ in server.statements if not text.lstrip().startswith("SELECT")]


def _everything(server: _FakeServer) -> str:
    """Every statement and its parameters, upper-cased, for forbidden-word checks.

    The random base64 verifier is masked first: upper-cased, it could spell a
    forbidden word by chance and make the check flaky.
    """
    joined = " ".join(f"{text} {params!r}" for _, text, params in server.statements)
    return _VERIFIER_PATTERN.sub("<VERIFIER>", joined).upper()


def _password_in(env_file: pathlib.Path) -> str:
    """The runtime password, read back from the settings file the run wrote."""
    url = env_file.read_text(encoding="utf-8").splitlines()[0].split("=", 1)[1]
    return safety.effective_parameters(url)["password"]


def _assert_no_destructive_statement(server: _FakeServer) -> None:
    """Nothing was granted, revoked, altered, dropped or otherwise changed beyond CREATE ROLE."""
    everything = _everything(server)
    for forbidden in ("DROP", "GRANT", "REVOKE", "ALTER", "TRUNCATE", "DELETE", "INSERT", "UPDATE",
                      "COMMENT", "SET ROLE", "CREATE DATABASE", "KERNO_DEV", "PG_HBA", "PG_RELOAD_CONF"):
        assert forbidden not in everything, forbidden


# ── Refusals before any password ────────────────────────────────────────────


def test_an_existing_runtime_settings_file_is_refused_before_asking_for_any_password(tmp_path):
    (tmp_path / _FILE_NAME).write_text("KEEP=me\n", encoding="utf-8")
    server = _FakeServer()
    outcome = _run(tmp_path, server)
    assert outcome.code == RUNTIME.EXIT_REFUSED and outcome.prompts == [] and server.connections == []
    assert outcome.env_file.read_text(encoding="utf-8") == "KEEP=me\n"


@pytest.mark.parametrize(
    "environ",
    [
        {"PGHOSTADDR": "10.0.0.5"},
        {"PGSERVICE": "dev"},
        {"PGOPTIONS": "-c role=other"},
        {"KERNO_TEST_RUNTIME_DATABASE_URL": "x"},
        {"KERNO_TEST_RUNTIME_DATABASE_APPROVAL": ""},
        {"KERNO_TEST_RUNTIME_ENV_FILE": "elsewhere.env"},
    ],
)
def test_redirecting_or_runtime_environment_is_refused_before_asking_for_any_password(tmp_path, capsys, environ):
    server = _FakeServer()
    outcome = _run(tmp_path, server, environ=environ)
    assert outcome.code == RUNTIME.EXIT_REFUSED and outcome.prompts == []
    assert server.connections == [] and not outcome.env_file.exists()
    assert next(iter(environ)) in capsys.readouterr().out


def test_a_settings_file_git_would_track_is_refused_before_asking_for_any_password(tmp_path):
    prompts: list[str] = []
    code = RUNTIME.run([], environ={}, connect=_connector(_FakeServer()), read_secret=prompts.append,
                       ask=prompts.append, env_file=tmp_path / _FILE_NAME, is_ignored=lambda path: False)
    assert code == RUNTIME.EXIT_REFUSED and prompts == [] and not (tmp_path / _FILE_NAME).exists()


def test_git_being_unavailable_is_refused_naming_the_runtime_file(tmp_path, monkeypatch, capsys):
    def no_git(*args, **kwargs):
        """Fail the way a missing git executable does."""
        raise OSError("git not found")

    monkeypatch.setattr(subprocess, "run", no_git)
    prompts: list[str] = []
    code = RUNTIME.run([], environ={}, connect=_connector(_FakeServer()), read_secret=prompts.append,
                       ask=prompts.append, env_file=tmp_path / _FILE_NAME)
    assert code == RUNTIME.EXIT_REFUSED and prompts == []
    assert "git is not available to confirm .env.test.runtime is ignored." in capsys.readouterr().out


def test_the_real_runtime_settings_file_is_gitignored_in_this_repository():
    assert RUNTIME.runtime_file_gitignored(safety.DEFAULT_TEST_RUNTIME_ENV_FILE) is True


@pytest.mark.parametrize("argv", [["--admin-role"], ["--admin-role", ""], ["--other", "x"], ["postgres"]])
def test_unknown_arguments_are_refused_before_asking_for_any_password(tmp_path, argv):
    server = _FakeServer()
    outcome = _run(tmp_path, server, argv=argv)
    assert outcome.code == RUNTIME.EXIT_REFUSED and outcome.prompts == [] and server.connections == []


def test_an_empty_administrator_password_is_refused_without_connecting(tmp_path):
    server = _FakeServer()
    outcome = _run(tmp_path, server, secret="")
    assert outcome.code == RUNTIME.EXIT_REFUSED and server.connections == [] and not outcome.env_file.exists()


def test_an_authentication_failure_creates_nothing_and_leaks_nothing(tmp_path, capsys):
    def refuse(**keywords):
        """Fail the login the way the server does, echoing the password to prove it is redacted."""
        raise psycopg2.OperationalError(f'password authentication failed for user "postgres" ({_ADMIN_SECRET})')

    code = RUNTIME.run([], environ={}, connect=refuse, read_secret=lambda prompt: _ADMIN_SECRET,
                       ask=lambda prompt: "yes", env_file=tmp_path / _FILE_NAME, is_ignored=lambda path: True)
    printed = capsys.readouterr().out
    assert code == RUNTIME.EXIT_REFUSED and not (tmp_path / _FILE_NAME).exists()
    assert "Created before stopping: nothing" in printed and _ADMIN_SECRET not in printed


# ── Server refusals: nothing changed ────────────────────────────────────────


@pytest.mark.parametrize(
    "server_row, fragment",
    [
        (("127.0.0.1", 5432, False, "PostgreSQL 18.4"), "PostgreSQL 16"),
        (("10.1.2.3", 5432, True, "PostgreSQL 18.4"), "loopback"),
        (("127.0.0.1", 5433, True, "PostgreSQL 18.4"), "loopback"),
    ],
)
def test_a_non_superuser_or_non_loopback_administrator_is_refused_and_nothing_is_changed(
    tmp_path, capsys, server_row, fragment
):
    server = _FakeServer(server_row=server_row)
    outcome = _run(tmp_path, server)
    assert outcome.code == RUNTIME.EXIT_REFUSED and len(outcome.prompts) == 1
    assert _writes(server) == [] and not outcome.env_file.exists()
    assert all(connection.closed for connection in server.connections)
    assert fragment in capsys.readouterr().out


@pytest.mark.parametrize(
    "fault, fragment",
    [
        ({"owner_role_row": None}, "role kerno_test does not exist"),
        ({"database_row": None}, "database kerno_test does not exist"),
        ({"database_row": ("someone_else", safety.DISPOSABLE_DATABASE_COMMENT, False)}, "owned by 'someone_else'"),
        ({"database_row": ("kerno_test", None, False)}, "disposable-database comment"),
        ({"database_row": ("kerno_test", safety.DISPOSABLE_DATABASE_COMMENT, True)}, "PUBLIC holds CONNECT"),
    ],
)
def test_a_missing_or_altered_owner_target_is_refused_and_nothing_is_changed(tmp_path, capsys, fault, fragment):
    server = _FakeServer(**fault)
    outcome = _run(tmp_path, server)
    assert outcome.code == RUNTIME.EXIT_REFUSED and len(outcome.prompts) == 1
    assert _writes(server) == [] and not outcome.env_file.exists()
    assert all(connection.closed for connection in server.connections)
    assert fragment in capsys.readouterr().out


def test_an_existing_runtime_role_is_reported_and_never_reused_altered_or_dropped(tmp_path, capsys):
    server = _FakeServer(runtime_role_row=(True, False, False, False, False, False, False))
    outcome = _run(tmp_path, server)
    printed = capsys.readouterr().out
    assert outcome.code == RUNTIME.EXIT_REFUSED and len(outcome.prompts) == 1
    assert _writes(server) == [] and not outcome.env_file.exists()
    assert "kerno_test_app already exists" in printed and "State: nothing was created" in printed
    _assert_no_destructive_statement(server)
    assert all(connection.closed for connection in server.connections)


def test_declining_the_confirmation_changes_nothing(tmp_path, capsys):
    server = _FakeServer()
    outcome = _run(tmp_path, server, answer="no")
    assert outcome.code == RUNTIME.EXIT_REFUSED and _writes(server) == [] and not outcome.env_file.exists()
    assert all(connection.closed for connection in server.connections)
    assert "declined: nothing was changed." in capsys.readouterr().out


# ── A full run ──────────────────────────────────────────────────────────────


def test_a_full_run_issues_exactly_the_expected_statements_in_order(tmp_path):
    server = _FakeServer()
    outcome = _run(tmp_path, server)
    assert outcome.code == RUNTIME.EXIT_DONE
    assert [text for _, text, _ in server.statements] == [
        RUNTIME.SERVER_SQL, RUNTIME.OWNER_ROLE_SQL, RUNTIME.OWNER_DATABASE_SQL,
        RUNTIME.EXISTING_RUNTIME_ROLE_SQL, _writes(server)[0], RUNTIME.VERIFICATION_SQL,
    ]
    assert len(_writes(server)) == 1 and _CREATE_ROLE_PATTERN.match(_writes(server)[0])
    assert server.statements[1][2] == ["kerno_test"] and server.statements[3][2] == ["kerno_test_app"]
    verification_parameters = server.statements[5][2]
    assert verification_parameters["role"] == "kerno_test_app" and verification_parameters["database"] == "kerno_test"
    assert verification_parameters["verifier"] == server.stored_verifier


def test_the_settings_file_is_complete_on_disk_before_create_role_and_the_role_is_committed_on_return(tmp_path):
    server = _FakeServer()
    outcome = _run(tmp_path, server)
    assert outcome.code == RUNTIME.EXIT_DONE
    assert server.file_at_create == outcome.env_file.read_text(encoding="utf-8")
    assert len(server.file_at_create.splitlines()) == 2
    assert server.autocommit_at_create is True


def test_the_settings_file_holds_exactly_the_runtime_pair_the_guard_accepts(tmp_path):
    outcome = _run(tmp_path)
    password = _password_in(outcome.env_file)
    assert outcome.env_file.read_bytes() == (
        f"KERNO_TEST_RUNTIME_DATABASE_URL=postgresql://kerno_test_app:{password}@127.0.0.1:5432/kerno_test\n"
        "KERNO_TEST_RUNTIME_DATABASE_APPROVAL=kerno_test_app@127.0.0.1:5432/kerno_test\n"
    ).encode("utf-8")
    url, approval, _ = safety.read_runtime_settings({}, outcome.env_file)
    target = safety.resolve_runtime_target(url, approval, {}, _OWNER_TARGET)
    assert target.identity == "kerno_test_app@127.0.0.1:5432/kerno_test"
    assert len(password) == 43


def test_the_password_appears_only_in_the_file_and_the_server_receives_only_its_verifier(tmp_path, capsys):
    server = _FakeServer()
    outcome = _run(tmp_path, server)
    printed = capsys.readouterr().out
    password = _password_in(outcome.env_file)
    assert password not in " ".join(f"{text} {params!r}" for _, text, params in server.statements)
    assert all(password not in repr(connection.keywords) for connection in server.connections)
    assert password not in printed and _ADMIN_SECRET not in printed and "postgresql://" not in printed
    assert server.stored_verifier not in printed
    iterations_and_salt = server.stored_verifier.split("$")[1]
    iterations, salt = iterations_and_salt.split(":")
    rebuilt = RUNTIME.owner_provisioning.scram_verifier(password, base64.b64decode(salt), int(iterations))
    assert rebuilt == server.stored_verifier


def test_the_settings_file_is_opened_once_with_exclusive_create_and_nothing_else_is_opened(tmp_path, monkeypatch):
    opened: list[tuple[pathlib.Path, str]] = []
    real_open = builtins.open

    def recording_open(file, mode="r", *args, **kwargs):
        """Record every open the script makes, then open for real."""
        opened.append((pathlib.Path(file), mode))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    outcome = _run(tmp_path)
    monkeypatch.undo()
    assert outcome.code == RUNTIME.EXIT_DONE
    assert opened == [(tmp_path / _FILE_NAME, "x")]


def test_a_file_that_appears_before_the_write_is_never_overwritten_and_no_role_is_created(tmp_path, capsys):
    server = _FakeServer()
    env_file = tmp_path / _FILE_NAME

    def confirm_then_race(prompt):
        """Answer yes, but let another process create the file first."""
        env_file.write_text("SOMEONE=else\n", encoding="utf-8")
        return "yes"

    outcome = _run(tmp_path, server, ask=confirm_then_race)
    printed = capsys.readouterr().out
    assert outcome.code == RUNTIME.EXIT_REFUSED and _writes(server) == []
    assert env_file.read_text(encoding="utf-8") == "SOMEONE=else\n"
    assert "FileExistsError" in printed and "State: nothing was created" in printed


def test_only_one_administrator_connection_to_postgres_is_opened_and_no_runtime_login_is_attempted(tmp_path):
    server = _FakeServer()
    outcome = _run(tmp_path, server)
    assert outcome.code == RUNTIME.EXIT_DONE and len(server.connections) == 1
    keywords = server.connections[0].keywords
    assert (keywords["user"], keywords["dbname"], keywords["host"], keywords["port"]) == (
        "postgres", "postgres", "127.0.0.1", 5432,
    )
    assert keywords["password"] == _ADMIN_SECRET and server.connections[0].closed


def test_the_admin_role_option_names_the_administrator(tmp_path):
    server = _FakeServer()
    outcome = _run(tmp_path, server, argv=["--admin-role", "kerno_admin"])
    assert outcome.code == RUNTIME.EXIT_DONE and server.connections[0].keywords["user"] == "kerno_admin"
    assert "kerno_admin" in outcome.prompts[0]


def test_nothing_is_granted_altered_or_dropped_and_the_development_database_is_never_contacted(tmp_path):
    server = _FakeServer()
    outcome = _run(tmp_path, server)
    assert outcome.code == RUNTIME.EXIT_DONE
    _assert_no_destructive_statement(server)
    assert {database for database, _, _ in server.statements} == {"postgres"}


# ── Partial failure ─────────────────────────────────────────────────────────


def test_a_failed_create_role_reports_the_file_and_deletes_nothing(tmp_path, capsys):
    server = _FakeServer(fail_on_create=True)
    outcome = _run(tmp_path, server)
    printed = capsys.readouterr().out
    report = next(line for line in printed.splitlines() if line.startswith("Created before stopping:"))
    assert outcome.code == RUNTIME.EXIT_STOPPED and outcome.env_file.exists()
    assert len(outcome.env_file.read_text(encoding="utf-8").splitlines()) == 2
    assert report == "Created before stopping: .env.test.runtime (the runtime pair; the password only there)"
    assert "confirm the role is absent" in printed and "then delete .env.test.runtime" in printed
    assert "SCRAM-SHA-256$" not in printed
    assert server.statements[-1][1].startswith("CREATE ROLE")
    _assert_no_destructive_statement(server)
    assert all(connection.closed for connection in server.connections)


def test_a_create_role_with_the_wrong_status_stops_before_verification(tmp_path, capsys):
    server = _FakeServer(create_status="SELECT 0")
    outcome = _run(tmp_path, server)
    printed = capsys.readouterr().out
    assert outcome.code == RUNTIME.EXIT_STOPPED and outcome.env_file.exists()
    assert "expected 'CREATE ROLE'" in printed and "confirm the role is absent" in printed
    assert server.statements[-1][1].startswith("CREATE ROLE")


@pytest.mark.parametrize(
    "fault, fragment",
    [
        ({"role_state": {"memberships": 1}}, "no role memberships, as a member or as a granted role (found 1)"),
        ({"role_state": {"owned_objects": 2}}, "owns no object (no pg_shdepend owner rows) (found 2)"),
        ({"role_state": {"inherit": True}}, "NOINHERIT (found True)"),
        ({"role_state": {"superuser": True}}, "NOSUPERUSER (found True)"),
        ({"role_state": {"bypass_rls": True}}, "NOBYPASSRLS (found True)"),
        ({"role_state": {"can_login": False}}, "LOGIN (found False)"),
        ({"role_state": {"can_connect": True}}, "no CONNECT on kerno_test yet (grant-runtime grants it) (found True)"),
        ({"stores_other_password": True}, "stored exactly the SCRAM verifier it was sent (found False)"),
        ({"fail_on_verification": True}, "OperationalError: server closed the connection"),
    ],
)
def test_a_failed_verification_reports_the_role_and_the_owner_decision_and_deletes_nothing(
    tmp_path, capsys, fault, fragment
):
    server = _FakeServer(**fault)
    outcome = _run(tmp_path, server)
    printed = capsys.readouterr().out
    report = next(line for line in printed.splitlines() if line.startswith("Created before stopping:"))
    assert outcome.code == RUNTIME.EXIT_STOPPED and outcome.env_file.exists()
    assert fragment in printed
    assert ".env.test.runtime" in report and "role kerno_test_app" in report
    assert "ALTER ROLE kerno_test_app NOLOGIN" in printed and "DROP ROLE kerno_test_app" in printed
    _assert_no_destructive_statement(server)
    assert all(connection.closed for connection in server.connections)


# ── Static properties and separation ────────────────────────────────────────


def test_verification_counts_memberships_in_both_directions_and_owned_objects():
    query = " ".join(RUNTIME.VERIFICATION_SQL.split())
    assert "pg_auth_members AS m WHERE m.member = r.oid OR m.roleid = r.oid" in query
    assert "s.refclassid = 'pg_authid'::regclass AND s.refobjid = r.oid AND s.deptype = 'o'" in query


def test_verification_columns_are_read_in_the_order_the_fields_name_them():
    column_for_field = {
        "can_login": "r.rolcanlogin", "superuser": "r.rolsuper", "create_database": "r.rolcreatedb",
        "create_role": "r.rolcreaterole", "replication": "r.rolreplication", "bypass_rls": "r.rolbypassrls",
        "inherit": "r.rolinherit", "memberships": "pg_auth_members", "owned_objects": "pg_shdepend",
        "can_connect": "has_database_privilege", "stored_verifier_matches": "r.rolpassword",
    }
    assert set(RUNTIME.VERIFICATION_FIELDS) == set(column_for_field) == set(_HEALTHY_ROLE) | {"stored_verifier_matches"}
    positions = [RUNTIME.VERIFICATION_SQL.index(column_for_field[field]) for field in RUNTIME.VERIFICATION_FIELDS]
    assert positions == sorted(positions)


def test_the_credential_never_shows_its_secrets():
    credential = RUNTIME.RuntimeCredential.generate()
    assert credential.password not in repr(credential) and credential.verifier not in repr(credential)
    assert credential.verifier.startswith("SCRAM-SHA-256$4096:")


def test_the_script_never_installs_or_changes_the_test_connection_guard():
    source = _SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("bootstrap_test_process", "install_connection_guard", "prepare_migration_process",
                      "_guarded_connect", "verify_runtime_identity", "load_dotenv", "dotenv_values",
                      "PYTHON_DOTENV_DISABLED", "DEFAULT_TEST_ENV_FILE", "TEST_DATABASE_URL_VARIABLE"):
        assert forbidden not in source, forbidden
    connect_before = psycopg2.connect
    _load_script()
    assert psycopg2.connect is connect_before


def test_the_script_imports_only_constants_from_the_safety_module():
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "tests", "the safety module itself is imported, not only its constants"
            if node.module == "tests._database_safety":
                imported.extend(alias.name for alias in node.names)
        if isinstance(node, ast.Import):
            assert all("_database_safety" not in alias.name for alias in node.names)
    assert imported and all(name.isupper() for name in imported)
