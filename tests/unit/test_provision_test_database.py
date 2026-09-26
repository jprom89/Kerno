"""Offline proof of scripts/provision_test_database.py — the owner-run kerno_test setup.

What:  Drives the provisioning script against an in-memory fake server and
       proves:
       - it refuses before connecting when .env.test exists, a redirecting
         libpq variable is set, or no administrator password is given;
       - it stops, changing nothing, when a kerno_test role or database
         already exists, or when the owner declines;
       - a full run issues exactly the runbook's statements in order, with a
         SCRAM verifier instead of the password;
       - it writes .env.test with only the two settings, closes the
         administrator connections, and verifies by logging in as the
         restricted role;
       - on a partial failure it reports exactly what was created and
         deletes nothing;
       - neither password ever reaches its output;
       - it never touches the test connection guard.
Why:   The script runs once, with administrator rights, on the owner's
       machine. Its safety properties have to be shown before that run, not
       discovered during it.
How:   pytest tests/unit/test_provision_test_database.py -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import pathlib

import psycopg2
import pytest
from psycopg2 import sql

from tests import _database_safety as safety

_SCRIPT = safety.REPOSITORY_ROOT / "scripts" / "provision_test_database.py"
_ADMIN_SECRET = "admin-secret-zz"
_DEV_ACL = "{=Tc/kerno_dev,kerno_dev=CTc/kerno_dev}"


def _load_script():
    """Import the provisioning script by path."""
    spec = importlib.util.spec_from_file_location("provision_test_database", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROVISION = _load_script()


def _text(statement) -> str:
    """Render a psycopg2.sql composition (or a plain string) without a real connection."""
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


class _FakeServer:
    """Catalog answers and a log of every statement and connection, shared by all fake connections."""

    def __init__(self, role_row=None, database_row=None, fail_on=None, acl_after=_DEV_ACL,
                 odd_status_on=None) -> None:
        self.role_row = role_row
        self.database_row = database_row
        self.fail_on = fail_on
        self.odd_status_on = odd_status_on
        self.acl_reads = [_DEV_ACL, acl_after]
        self.statements: list[tuple[str, str]] = []
        self.connections: list["_FakeConnection"] = []
        self.events: list[str] = []


class _FakeConnection:
    """A connection to one database as one role on the fake server."""

    def __init__(self, server: _FakeServer, keywords: dict) -> None:
        self.server = server
        self.keywords = keywords
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.server.events.append(f"close {self.keywords['user']}@{self.keywords['dbname']}")


class _FakeCursor:
    """Executes against the fake server; writes get a command status, reads get canned rows."""

    _STATUSES = {
        "CREATE ROLE": "CREATE ROLE", "CREATE DATABASE": "CREATE DATABASE", "REVOKE": "REVOKE",
        "CREATE EXTENSION": "CREATE EXTENSION", "COMMENT": "COMMENT",
    }

    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.statusmessage = None
        self._row = None

    def execute(self, statement, params=None) -> None:
        server = self.connection.server
        text = _text(statement)
        server.statements.append((self.connection.keywords["dbname"], text))
        for prefix, status in self._STATUSES.items():
            if text.startswith(prefix):
                if server.fail_on == prefix:
                    raise psycopg2.Error(f"{prefix} failed")
                self.statusmessage = "SELECT 0" if server.odd_status_on == prefix else status
                return
        self._row = self._read(text, server)

    def _read(self, text: str, server: _FakeServer):
        if "inet_server_addr()" in text:
            return ("127.0.0.1", 5432, True, "PostgreSQL 18.4")
        if text.startswith("SELECT rolcanlogin"):
            return server.role_row
        if "shobj_description" in text and "datacl" not in text:
            return server.database_row
        if text.startswith("SELECT datacl"):
            return (server.acl_reads.pop(0),)
        if text == "SELECT current_database()":
            return ("kerno_test",)
        if text.startswith("SELECT rolsuper"):
            return (False, False, False, False, False, True, 0)
        if "datacl::text" in text:
            return ("kerno_test", "{kerno_test=CTc/kerno_test}", safety.DISPOSABLE_DATABASE_COMMENT)
        if text.startswith("SELECT current_database(), session_user"):
            return ("kerno_test", "kerno_test", "kerno_test", 5432, True, True)
        raise AssertionError(f"unexpected read: {text[:70]}")

    def fetchone(self):
        return self._row


def _connector(server: _FakeServer):
    """A connect function that opens fake connections and records their keyword arguments."""
    def connect(**keywords):
        server.events.append(f"open {keywords['user']}@{keywords['dbname']}")
        connection = _FakeConnection(server, keywords)
        server.connections.append(connection)
        return connection
    return connect


def _run(tmp_path, server=None, answer="yes", secret=_ADMIN_SECRET, environ=None, argv=()):
    """Run the script's entry point with every dependency faked; return (exit code, env file)."""
    env_file = tmp_path / ".env.test"
    code = PROVISION.run(
        list(argv), environ=environ or {}, connect=_connector(server or _FakeServer()),
        read_secret=lambda prompt: secret, ask=lambda prompt: answer,
        env_file=env_file, is_ignored=lambda path: True,
    )
    return code, env_file


def _writes(server: _FakeServer) -> list[str]:
    """Every statement that changes something, in order."""
    return [text for _, text in server.statements if not text.startswith("SELECT")]


# ── Refusals before any change ──────────────────────────────────────────────


def test_an_existing_env_test_is_refused_before_asking_for_any_password(tmp_path):
    (tmp_path / ".env.test").write_text("KERNO_TEST_DATABASE_URL=keep-me\n")
    asked: list[str] = []
    code = PROVISION.run([], environ={}, connect=_connector(_FakeServer()),
                         read_secret=asked.append, ask=asked.append,
                         env_file=tmp_path / ".env.test", is_ignored=lambda path: True)
    assert code == PROVISION.EXIT_REFUSED and asked == []
    assert (tmp_path / ".env.test").read_text() == "KERNO_TEST_DATABASE_URL=keep-me\n"


@pytest.mark.parametrize(
    "environ",
    [{"PGHOSTADDR": "10.0.0.5"}, {"PGSERVICE": "dev"}, {"KERNO_TEST_DATABASE_URL": "x"}],
)
def test_redirecting_or_overriding_environment_is_refused_before_connecting(tmp_path, environ):
    server = _FakeServer()
    code, env_file = _run(tmp_path, server, environ=environ)
    assert code == PROVISION.EXIT_REFUSED and server.connections == [] and not env_file.exists()


def test_a_settings_file_git_would_track_is_refused(tmp_path):
    code = PROVISION.run([], environ={}, connect=_connector(_FakeServer()), read_secret=lambda p: _ADMIN_SECRET,
                         ask=lambda p: "yes", env_file=tmp_path / ".env.test", is_ignored=lambda path: False)
    assert code == PROVISION.EXIT_REFUSED


def test_an_empty_administrator_password_is_refused_without_connecting(tmp_path):
    server = _FakeServer()
    code, env_file = _run(tmp_path, server, secret="")
    assert code == PROVISION.EXIT_REFUSED and server.connections == [] and not env_file.exists()


@pytest.mark.parametrize(
    "existing",
    [
        {"role_row": (True, False, False, False, False, False)},
        {"database_row": ("kerno_test", None)},
    ],
)
def test_an_existing_role_or_database_is_reported_and_nothing_is_changed(tmp_path, capsys, existing):
    server = _FakeServer(**existing)
    code, env_file = _run(tmp_path, server)
    assert code == PROVISION.EXIT_REFUSED
    assert _writes(server) == [] and not env_file.exists()
    assert all(connection.closed for connection in server.connections)
    assert "exists" in capsys.readouterr().out


def test_declining_the_confirmation_changes_nothing(tmp_path):
    server = _FakeServer()
    code, env_file = _run(tmp_path, server, answer="no")
    assert code == PROVISION.EXIT_REFUSED and _writes(server) == [] and not env_file.exists()


def test_an_authentication_failure_creates_nothing_and_leaks_nothing(tmp_path, capsys):
    def refuse(**keywords):
        raise psycopg2.OperationalError(f'password authentication failed for user "postgres" ({_ADMIN_SECRET})')

    code = PROVISION.run([], environ={}, connect=refuse, read_secret=lambda p: _ADMIN_SECRET, ask=lambda p: "yes",
                         env_file=tmp_path / ".env.test", is_ignored=lambda path: True)
    printed = capsys.readouterr().out
    assert code == PROVISION.EXIT_REFUSED and not (tmp_path / ".env.test").exists()
    assert "Created before stopping: nothing" in printed and _ADMIN_SECRET not in printed


# ── A full run ──────────────────────────────────────────────────────────────


def test_a_full_run_issues_exactly_the_runbook_statements_in_order(tmp_path):
    server = _FakeServer()
    code, _ = _run(tmp_path, server)
    assert code == PROVISION.EXIT_DONE
    writes = _writes(server)
    assert [text.split(" ")[0] + " " + text.split(" ")[1] for text in writes] == [
        "CREATE ROLE", "CREATE DATABASE", "REVOKE ALL", "CREATE EXTENSION", "COMMENT ON",
    ]
    assert writes[0].startswith(
        'CREATE ROLE "kerno_test" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '
        "'SCRAM-SHA-256$4096:"
    )
    assert writes[1] == 'CREATE DATABASE "kerno_test" OWNER "kerno_test"'
    assert writes[2] == 'REVOKE ALL ON DATABASE "kerno_test" FROM PUBLIC'
    assert ("kerno_test", "CREATE EXTENSION vector") in server.statements
    assert writes[4] == f"COMMENT ON DATABASE \"kerno_test\" IS '{safety.DISPOSABLE_DATABASE_COMMENT}'"


def test_nothing_is_dropped_granted_or_run_against_the_development_database(tmp_path):
    server = _FakeServer()
    _run(tmp_path, server)
    everything = " ".join(text for _, text in server.statements).upper()
    for forbidden in ("DROP", "GRANT", "ALTER", "TRUNCATE", "DELETE", "SUPERUSER LOGIN", " BYPASSRLS "):
        assert forbidden not in everything.replace("NOSUPERUSER", "").replace("NOBYPASSRLS", ""), forbidden
    assert {database for database, _ in server.statements} <= {"postgres", "kerno_test"}
    assert all(connection.keywords["dbname"] != "kerno_dev" for connection in server.connections)


def test_the_settings_file_holds_exactly_the_two_settings_and_the_password_only_there(tmp_path, capsys):
    server = _FakeServer()
    code, env_file = _run(tmp_path, server)
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert code == PROVISION.EXIT_DONE and len(lines) == 2
    url = lines[0].split("=", 1)[1]
    assert lines[1] == "KERNO_TEST_DATABASE_APPROVAL=kerno_test@127.0.0.1:5432/kerno_test"
    password = safety.effective_parameters(url)["password"]
    target = safety.resolve_target(url, "kerno_test@127.0.0.1:5432/kerno_test", {})
    assert target.identity == "kerno_test@127.0.0.1:5432/kerno_test"
    assert password not in " ".join(text for _, text in server.statements)
    printed = capsys.readouterr().out
    assert password not in printed and _ADMIN_SECRET not in printed and "postgresql://" not in printed


def test_administrator_connections_close_before_the_restricted_login_check(tmp_path):
    server = _FakeServer()
    _, env_file = _run(tmp_path, server)
    password = safety.effective_parameters(env_file.read_text().splitlines()[0].split("=", 1)[1])["password"]
    last_admin_close = max(i for i, event in enumerate(server.events) if event.startswith("close postgres@"))
    test_login = server.events.index("open kerno_test@kerno_test")
    assert last_admin_close < test_login
    assert all(connection.closed for connection in server.connections)
    login = next(c for c in server.connections if c.keywords["user"] == "kerno_test")
    assert login.keywords["password"] == password and login.keywords["host"] == "127.0.0.1"
    assert all(c.keywords["password"] == _ADMIN_SECRET for c in server.connections if c.keywords["user"] == "postgres")


# ── Partial failure ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("fail_on, created", [
    ("CREATE ROLE", [".env.test"]),
    ("CREATE DATABASE", [".env.test", "role kerno_test"]),
    ("CREATE EXTENSION", [".env.test", "role kerno_test", "database kerno_test", "PUBLIC access"]),
])
def test_a_partial_failure_reports_what_exists_and_deletes_nothing(tmp_path, capsys, fail_on, created):
    server = _FakeServer(fail_on=fail_on)
    code, env_file = _run(tmp_path, server)
    printed = capsys.readouterr().out
    report = next(line for line in printed.splitlines() if line.startswith("Created before stopping:"))
    assert code == PROVISION.EXIT_STOPPED and env_file.exists()
    assert all(fragment in report for fragment in created)
    assert "DROP" not in " ".join(text for _, text in server.statements).upper()
    assert _writes(server)[-1].startswith(fail_on)
    assert all(connection.closed for connection in server.connections)


def test_a_statement_that_reports_the_wrong_result_stops_the_run(tmp_path, capsys):
    server = _FakeServer(odd_status_on="CREATE DATABASE")
    code, _ = _run(tmp_path, server)
    printed = capsys.readouterr().out
    assert code == PROVISION.EXIT_STOPPED and "expected 'CREATE DATABASE'" in printed
    assert not any(text.startswith("REVOKE") for text in _writes(server))


def test_a_failed_verification_stops_and_reports(tmp_path, capsys):
    server = _FakeServer(acl_after="{=Tc/kerno_dev,kerno_dev=CTc/kerno_dev,kerno_test=c/kerno_dev}")
    code, _ = _run(tmp_path, server)
    printed = capsys.readouterr().out
    assert code == PROVISION.EXIT_STOPPED and "kerno_dev access list unchanged" in printed


# ── Secrets and separation ──────────────────────────────────────────────────


def test_the_scram_verifier_matches_the_rfc_7677_test_vector():
    # RFC 7677 §3: password "pencil", salt W22ZaJ0SNY7soEsUEjb6gQ==, 4096
    # iterations. The server signature and client proof it publishes pin
    # ServerKey and StoredKey exactly.
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    verifier = PROVISION.scram_verifier("pencil", salt, 4096)
    keys = verifier.split("$", 2)[2]
    stored_key, server_key = (base64.b64decode(part) for part in keys.split(":"))
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth_message = (
        f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r={nonce}"
    ).encode()
    server_signature = hmac.new(server_key, auth_message, "sha256").digest()
    assert base64.b64encode(server_signature).decode() == "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client_signature = hmac.new(stored_key, auth_message, "sha256").digest()
    client_key = bytes(a ^ b for a, b in zip(proof, client_signature))
    assert hashlib.sha256(client_key).digest() == stored_key
    assert verifier.startswith("SCRAM-SHA-256$4096:W22ZaJ0SNY7soEsUEjb6gQ==$")


def test_generated_passwords_need_no_url_encoding():
    url = PROVISION.settings_url("abc-DEF_123")
    assert safety.effective_parameters(url)["password"] == "abc-DEF_123"


def test_the_script_never_installs_or_changes_the_test_connection_guard():
    source = _SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("bootstrap_test_process", "install_connection_guard", "prepare_migration_process",
                      "_guarded_connect", "load_dotenv", "dotenv_values", "PYTHON_DOTENV_DISABLED"):
        assert forbidden not in source, forbidden
