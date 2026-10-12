"""Offline tests for the grant-runtime and revoke-runtime commands of scripts/migrate_test_database.py.

What:  Drives the two C2 owner commands against a scripted safety module and a
       fake owner connection, and checks their routing, their fixed
       statements, every refusal, the order of checks, statements, lock
       re-proofs and commit, the rollback and exit code on each failure, and
       includes_revision against the real Alembic history.
Why:   PILOT-READY-002 (docs/pilot_readiness_kickoff.md §2.7 step 5). These
       commands change what the runtime login may do, so a refusal must leave
       nothing changed and success must commit only while the workflow lock is
       still held. Nothing here opens a database connection.
How:   pytest tests/unit/test_runtime_grant_commands.py -v
"""

from __future__ import annotations

import re
import shutil

import psycopg2
import pytest

from tests import _database_safety as safety
from tests.unit.test_database_safety import GOOD_URL, SECRET, make_target
from tests.unit.test_database_safety_processes import _FakeSafety, _load_wrapper, _RecordingSession

GRANT = "grant-runtime"
REVOKE = "revoke-runtime"
RUNTIME_ROLE = "kerno_test_app"
MECHANISM_REVISION = "d5e6f7g8"
PREVIOUS_REVISION = "c4d5e6f7"

# (can_login, superuser, bypass_rls, create_database, create_role, replication, memberships, owned_objects)
CLEAN_ROLE = (True, False, False, False, False, False, 0, 0)
_ROLE_FIELDS = (
    "can_login", "superuser", "bypass_rls", "create_database", "create_role", "replication",
    "memberships", "owned_objects",
)
_DIRTY_VALUES = {"can_login": False, "memberships": 1, "owned_objects": 2}

_ROLE_OR_DATABASE_DDL = re.compile(r"\b(CREATE|ALTER|DROP)\s+(ROLE|USER|DATABASE)\b", re.IGNORECASE)


@pytest.fixture
def wrapper():
    """A freshly imported wrapper module, so monkeypatched attributes never leak between tests."""
    return _load_wrapper()


class _GuardedSafety(_FakeSafety):
    """The scripted safety module, which also records any attribute the wrapper should never reach for."""

    def __getattr__(self, name):
        """Record an unexpected attribute lookup (a runtime-settings read, say) and fail it."""
        self.__dict__.setdefault("unexpected", []).append(name)
        raise AttributeError(name)


class _FakeCursor:
    """Answers the wrapper's three checks from its connection's script and records every statement."""

    def __init__(self, connection) -> None:
        """Attach to the scripted connection."""
        self.connection = connection
        self.last_sql = None

    def execute(self, sql, parameters=None) -> None:
        """Record the statement under a readable label, failing it when the script says so."""
        self.connection.calls.append(self.connection.labels.get(sql, sql))
        self.connection.executed.append((sql, parameters))
        if sql == self.connection.fail_on:
            raise psycopg2.OperationalError(f"server closed the connection for {GOOD_URL} (password {SECRET})")
        self.last_sql = sql

    def fetchone(self):
        """Return the scripted answer to the last check."""
        return self.connection.answers[self.last_sql]

    def fetchall(self):
        """Return the scripted assignment rows."""
        return self.connection.assignment


class _FakeConnection:
    """An owner connection whose catalog answers are scripted; records commit, rollback and close."""

    def __init__(self, wrapper, calls, *, mechanism=True, role=CLEAN_ROLE, assignment=(), fail_on=None) -> None:
        """Script the answers to the mechanism, role and assignment checks."""
        self.calls = calls
        self.labels = {
            wrapper._MECHANISM_PRESENT_SQL: "check:mechanism",
            wrapper._RUNTIME_ROLE_SQL: "check:role",
            wrapper._ASSIGNMENT_SQL: "check:assignment",
        }
        self.answers = {wrapper._MECHANISM_PRESENT_SQL: (mechanism,), wrapper._RUNTIME_ROLE_SQL: role}
        self.assignment = list(assignment)
        self.fail_on = fail_on
        self.autocommit = True
        self.executed: list[tuple] = []

    def cursor(self):
        """Return a cursor over this script."""
        return _FakeCursor(self)

    def commit(self) -> None:
        """Record the commit."""
        self.calls.append("commit")

    def rollback(self) -> None:
        """Record the rollback."""
        self.calls.append("rollback")

    def close(self) -> None:
        """Record the close."""
        self.calls.append("close")


class _RecordingConnect:
    """Stands in for psycopg2.connect: records the URL and keywords, then hands out one scripted connection."""

    def __init__(self, calls, connection=None, error=None) -> None:
        """Hand out connection, or raise error, on the first call."""
        self.calls = calls
        self.connection = connection
        self.error = error
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, **keywords):
        """Record one connection request."""
        self.calls.append("connect")
        self.requests.append((url, keywords))
        if self.error:
            raise self.error
        return self.connection


def _expected_assignment(command: str) -> list[tuple[str, str]]:
    """What the assignment record holds after a successful command."""
    return [(RUNTIME_ROLE, "full")] if command == GRANT else []


def _scripted_connection(wrapper, calls, command, **script) -> _FakeConnection:
    """A connection whose final assignment matches the command unless the script overrides it."""
    script.setdefault("assignment", _expected_assignment(command))
    return _FakeConnection(wrapper, calls, **script)


def _at_revision(monkeypatch, wrapper, revision) -> None:
    """Make the target report revision as its current Alembic revision, without connecting."""
    def current_revision(target):
        """Return the scripted revision."""
        return revision

    monkeypatch.setattr(wrapper, "current_revision", current_revision)


def _run(wrapper, command, connection, calls, lose_after=None) -> int:
    """Run the command directly with the real safety module, a recording session and a scripted connect."""
    session = _RecordingSession(calls, lose_after)
    connect = _RecordingConnect(calls, connection)
    return wrapper.run_runtime_grant_command(command, session, make_target(), safety, connect=connect)


def _statements(wrapper, command) -> tuple[str, ...]:
    """The fixed statements the command runs."""
    return wrapper.GRANT_RUNTIME_STATEMENTS if command == GRANT else wrapper.REVOKE_RUNTIME_STATEMENTS


def _success_calls(wrapper, command) -> list[str]:
    """The exact order of a successful command: checks, statements, assignment, re-proof, commit, close, re-proof."""
    return [
        "held?", "connect", "check:mechanism", "check:role", *_statements(wrapper, command),
        "check:assignment", "held?", "commit", "close", "held?",
    ]


def _route_connections_to(monkeypatch, wrapper, connect) -> None:
    """Keep open_owner_connection's real behaviour but hand it the recording connect instead of psycopg2's."""
    original = wrapper.open_owner_connection

    def open_owner_connection(target, command, connect_override=None):
        """Call the real opener with the recording connect."""
        return original(target, command, connect)

    monkeypatch.setattr(wrapper, "open_owner_connection", open_owner_connection)


def _descendant_history(tmp_path):
    """An Alembic config over a copy of the history with one extra revision on top of 029."""
    from alembic.config import Config

    migrations = tmp_path / "migrations"
    shutil.copytree(safety.REPOSITORY_ROOT / "migrations", migrations, ignore=shutil.ignore_patterns("__pycache__"))
    (migrations / "versions" / "999_descendant_probe.py").write_text(
        '"""Probe revision on top of 029."""\n'
        'revision = "z9descnd"\n'
        f'down_revision = "{MECHANISM_REVISION}"\n'
        "branch_labels = None\n"
        "depends_on = None\n\n\n"
        "def upgrade():\n    pass\n\n\n"
        "def downgrade():\n    pass\n",
        encoding="utf-8",
    )
    config = Config()
    config.set_main_option("script_location", str(migrations))
    return config


# ── Command line and routing ────────────────────────────────────────────────


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_parse_arguments_accepts_each_runtime_command_alone(wrapper, command):
    assert wrapper.parse_arguments([command]) == (command, None)


@pytest.mark.parametrize(
    "argv",
    [
        [GRANT, "full"], [REVOKE, RUNTIME_ROLE], [GRANT, ""], [GRANT, GRANT], [REVOKE, "pilot", "x"],
        ["GRANT-RUNTIME"], ["grant_runtime"], ["revoke"],
    ],
)
def test_parse_arguments_rejects_runtime_commands_with_extra_or_altered_arguments(wrapper, argv):
    assert wrapper.parse_arguments(argv) is None


def test_a_malformed_runtime_command_is_refused_before_any_bootstrap(wrapper, capsys):
    fake = _GuardedSafety()
    assert wrapper.main([GRANT, "pilot"], safety=fake) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert fake.calls == []
    assert "grant-runtime" in capsys.readouterr().out


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_main_routes_the_command_through_the_migration_bootstrap_and_the_migrate_lock(
    wrapper, monkeypatch, capsys, command
):
    fake = _GuardedSafety()
    connection = _scripted_connection(wrapper, fake.calls, command)
    connect = _RecordingConnect(fake.calls, connection)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    _route_connections_to(monkeypatch, wrapper, connect)
    assert wrapper.main([command], safety=fake) == wrapper.EXIT_DONE
    assert fake.calls == ["prepare", "lock:migrate", *_success_calls(wrapper, command), "release"]
    assert fake.__dict__.get("unexpected") is None
    printed = capsys.readouterr().out
    assert f"done: {command} {RUNTIME_ROLE}" in printed
    assert SECRET not in printed and "postgresql://" not in printed


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_main_releases_the_lock_when_the_command_is_refused(wrapper, monkeypatch, capsys, command):
    fake = _GuardedSafety()
    _at_revision(monkeypatch, wrapper, PREVIOUS_REVISION)
    assert wrapper.main([command], safety=fake) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert fake.calls == ["prepare", "lock:migrate", "held?", "release"]


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_main_releases_the_lock_when_the_lock_cannot_be_taken(wrapper, command):
    fake = _GuardedSafety(session_error=safety.DatabaseTargetBusy("held by pytest"))
    assert wrapper.main([command], safety=fake) == wrapper.EXIT_TARGET_REFUSED
    assert fake.calls == ["prepare", "lock:migrate", "release"]


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_main_without_configuration_never_takes_the_lock(wrapper, command):
    fake = _GuardedSafety(prepare_error=safety.DatabaseTargetNotConfigured("not configured"))
    assert wrapper.main([command], safety=fake) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert fake.calls == ["prepare"]


def test_the_wrapper_names_no_runtime_settings_reader():
    source = (safety.REPOSITORY_ROOT / "scripts" / "migrate_test_database.py").read_text(encoding="utf-8")
    for name in (
        "read_runtime_settings", "runtime_settings_file", "resolve_runtime_target", "verify_runtime_identity",
        "bootstrap_test_process", "read_runtime", "TEST_RUNTIME_DATABASE_URL", "KERNO_TEST_RUNTIME",
    ):
        assert name not in source, name


def test_the_migration_bootstrap_ignores_even_invalid_runtime_settings(tmp_path):
    runtime_file = tmp_path / "present.env.test.runtime"
    runtime_file.write_text("not a settings file\n", encoding="utf-8")
    environ = {
        safety.TEST_ENV_FILE_VARIABLE: str(tmp_path / "absent.env.test"),
        safety.TEST_DATABASE_URL_VARIABLE: GOOD_URL,
        safety.TEST_DATABASE_APPROVAL_VARIABLE: make_target().identity,
        safety.TEST_RUNTIME_ENV_FILE_VARIABLE: str(runtime_file),
        safety.TEST_RUNTIME_DATABASE_URL_VARIABLE: f"postgresql://kerno_dev:{SECRET}@127.0.0.1:1/kerno_dev",
        safety.TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE: "wrong",
    }
    state = safety.ProcessState()
    target = safety.prepare_migration_process(environ, state, {})
    assert target.identity == make_target().identity
    assert state.problem is None and state.runtime_target is None


# ── The fixed statements ────────────────────────────────────────────────────


def test_the_grant_and_revoke_statements_are_exactly_the_fixed_literals(wrapper):
    assert wrapper.GRANT_RUNTIME_STATEMENTS == (
        "GRANT CONNECT ON DATABASE kerno_test TO kerno_test_app",
        "CALL kerno_bootstrap.apply_runtime_grants('kerno_test_app', 'full')",
    )
    assert wrapper.REVOKE_RUNTIME_STATEMENTS == (
        "CALL kerno_bootstrap.revoke_runtime_grants('kerno_test_app')",
        "REVOKE CONNECT ON DATABASE kerno_test FROM kerno_test_app",
    )
    assert (wrapper.RUNTIME_PROFILE, wrapper.RUNTIME_MECHANISM_REVISION) == ("full", MECHANISM_REVISION)


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_nothing_the_command_executes_creates_alters_or_drops_a_role_or_database(wrapper, monkeypatch, command):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, command, connection, calls) == wrapper.EXIT_DONE
    executed = [sql for sql, _ in connection.executed]
    assert len(executed) == len(_statements(wrapper, command)) + 3
    for sql in executed:
        assert not _ROLE_OR_DATABASE_DDL.search(sql), sql
        assert "kerno_dev" not in sql


# ── Refusals: nothing changes ───────────────────────────────────────────────


@pytest.mark.parametrize("command", [GRANT, REVOKE])
@pytest.mark.parametrize("revision", [PREVIOUS_REVISION, None])
def test_a_history_without_029_is_refused_before_any_owner_connection(wrapper, monkeypatch, capsys, command, revision):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command)
    _at_revision(monkeypatch, wrapper, revision)
    assert _run(wrapper, command, connection, calls) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?"]
    assert "is not applied" in capsys.readouterr().out


def test_the_history_check_asks_for_migration_029_by_its_revision(wrapper, monkeypatch):
    asked: list[tuple] = []

    def includes_revision(config, current, required):
        """Record the question and answer no."""
        asked.append((current, required))
        return False

    _at_revision(monkeypatch, wrapper, "y0z1a2b3")
    monkeypatch.setattr(wrapper, "includes_revision", includes_revision)
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, GRANT)
    assert _run(wrapper, GRANT, connection, calls) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert asked == [("y0z1a2b3", MECHANISM_REVISION)]
    assert "connect" not in calls


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_missing_mechanism_objects_are_refused_and_rolled_back(wrapper, monkeypatch, capsys, command):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, mechanism=False)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, command, connection, calls) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?", "connect", "check:mechanism", "rollback", "close"]
    assert "procedures are missing" in capsys.readouterr().out


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_a_missing_runtime_role_is_refused_and_rolled_back(wrapper, monkeypatch, capsys, command):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, role=None)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, command, connection, calls) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?", "connect", "check:mechanism", "check:role", "rollback", "close"]
    assert f"role {RUNTIME_ROLE} does not exist" in capsys.readouterr().out


def test_the_role_check_asks_about_the_fixed_role_only(wrapper, monkeypatch):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, GRANT)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, GRANT, connection, calls) == wrapper.EXIT_DONE
    role_checks = [parameters for sql, parameters in connection.executed if sql == wrapper._RUNTIME_ROLE_SQL]
    assert role_checks == [(RUNTIME_ROLE,)]


@pytest.mark.parametrize("field", _ROLE_FIELDS)
def test_grant_runtime_refuses_a_role_with_one_invalid_attribute(wrapper, monkeypatch, capsys, field):
    position = _ROLE_FIELDS.index(field)
    dirty_role = list(CLEAN_ROLE)
    dirty_role[position] = _DIRTY_VALUES.get(field, True)
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, GRANT, role=tuple(dirty_role))
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, GRANT, connection, calls) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?", "connect", "check:mechanism", "check:role", "rollback", "close"]
    expected_problem = "cannot log in" if field == "can_login" else field
    assert expected_problem in capsys.readouterr().out


def test_grant_runtime_names_every_invalid_attribute_at_once(wrapper, monkeypatch, capsys):
    calls: list[str] = []
    every_problem = (False, True, True, True, True, True, 3, 4)
    connection = _scripted_connection(wrapper, calls, GRANT, role=every_problem)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, GRANT, connection, calls) == wrapper.EXIT_CONFIGURATION_REFUSED
    printed = capsys.readouterr().out
    assert (
        "cannot log in, superuser, bypass_rls, create_database, create_role, replication, memberships, owned_objects"
        in printed
    )


def test_revoke_runtime_does_not_require_the_attribute_checks(wrapper, monkeypatch):
    calls: list[str] = []
    elevated = (False, True, True, True, True, True, 3, 4)
    connection = _scripted_connection(wrapper, calls, REVOKE, role=elevated)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, REVOKE, connection, calls) == wrapper.EXIT_DONE
    assert calls == _success_calls(wrapper, REVOKE)


# ── Success, verification and the lock ──────────────────────────────────────


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_success_checks_then_runs_the_statements_verifies_and_commits_once_under_the_lock(
    wrapper, monkeypatch, command
):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, command, connection, calls) == wrapper.EXIT_DONE
    assert calls == _success_calls(wrapper, command)
    assert calls.count("commit") == 1 and "rollback" not in calls
    assert connection.autocommit is False


@pytest.mark.parametrize(
    "command, wrong_assignment",
    [
        (GRANT, []),
        (GRANT, [(RUNTIME_ROLE, "pilot")]),
        (GRANT, [("someone_else", "full")]),
        (GRANT, [(RUNTIME_ROLE, "full"), ("someone_else", "full")]),
        (REVOKE, [(RUNTIME_ROLE, "full")]),
    ],
)
def test_an_assignment_that_does_not_match_the_command_is_rolled_back_and_refused(
    wrapper, monkeypatch, capsys, command, wrong_assignment
):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, assignment=wrong_assignment)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, command, connection, calls) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls[-3:] == ["check:assignment", "rollback", "close"]
    assert "commit" not in calls
    assert "is not what was applied" in capsys.readouterr().out


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_a_lock_lost_before_the_commit_rolls_back_and_stops(wrapper, monkeypatch, capsys, command):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, command, connection, calls, lose_after=1) == wrapper.EXIT_TARGET_REFUSED
    assert calls[-4:] == ["check:assignment", "held?", "rollback", "close"]
    assert "commit" not in calls
    printed = capsys.readouterr().out
    assert "stopped:" in printed and "done:" not in printed


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_a_lock_lost_after_the_commit_is_not_reported_as_success(wrapper, monkeypatch, capsys, command):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, command, connection, calls, lose_after=2) == wrapper.EXIT_TARGET_REFUSED
    assert calls[-3:] == ["commit", "close", "held?"]
    assert "done:" not in capsys.readouterr().out


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_a_driver_error_rolls_back_and_prints_no_credential(wrapper, monkeypatch, capsys, command):
    calls: list[str] = []
    failing_statement = _statements(wrapper, command)[-1]
    connection = _scripted_connection(wrapper, calls, command, fail_on=failing_statement)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, command, connection, calls) == wrapper.EXIT_MIGRATION_FAILED
    assert calls[-3:] == [failing_statement, "rollback", "close"]
    assert "commit" not in calls
    printed = capsys.readouterr().out
    assert f"{command} failed: OperationalError" in printed
    assert SECRET not in printed and "postgresql://" not in printed


def test_a_guard_refusal_while_connecting_is_a_target_refusal(wrapper, monkeypatch, capsys):
    calls: list[str] = []
    session = _RecordingSession(calls)
    connect = _RecordingConnect(calls, error=safety.DatabaseTargetRejected("lock is not held by this session"))
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    code = wrapper.run_runtime_grant_command(GRANT, session, make_target(), safety, connect=connect)
    assert code == wrapper.EXIT_TARGET_REFUSED
    assert calls == ["held?", "connect"]


# ── The owner connection ────────────────────────────────────────────────────


@pytest.mark.parametrize("command", [GRANT, REVOKE])
def test_the_owner_connection_targets_the_owner_url_with_a_guard_application_name(wrapper, monkeypatch, command):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command)
    connect = _RecordingConnect(calls, connection)
    target = make_target()
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    session = _RecordingSession(calls)
    assert wrapper.run_runtime_grant_command(command, session, target, safety, connect=connect) == wrapper.EXIT_DONE
    assert connect.requests == [(target.url, {"application_name": f"kerno-test-guard:{command}"})]
    assert target.user == "kerno_test" and RUNTIME_ROLE not in target.url


def test_open_owner_connection_passes_the_target_url_unchanged(wrapper):
    calls: list[str] = []
    sentinel = object()
    connect = _RecordingConnect(calls, sentinel)
    assert wrapper.open_owner_connection(make_target(), REVOKE, connect) is sentinel
    assert connect.requests == [(GOOD_URL, {"application_name": "kerno-test-guard:revoke-runtime"})]


# ── includes_revision against the real history ──────────────────────────────


def test_includes_revision_is_true_only_from_029_onwards_in_the_real_history(wrapper):
    from alembic.script import ScriptDirectory

    config = wrapper.alembic_config()
    script = ScriptDirectory.from_config(config)
    assert wrapper.includes_revision(config, MECHANISM_REVISION, MECHANISM_REVISION) is True
    assert wrapper.includes_revision(config, script.get_current_head(), MECHANISM_REVISION) is True
    assert wrapper.includes_revision(config, PREVIOUS_REVISION, MECHANISM_REVISION) is False
    assert wrapper.includes_revision(config, None, MECHANISM_REVISION) is False
    # iterate_revisions excludes its lower bound, so this is 029 and everything above it.
    from_029_up = {revision.revision for revision in script.iterate_revisions("heads", PREVIOUS_REVISION)}
    assert MECHANISM_REVISION in from_029_up
    for revision in script.walk_revisions():
        expected = revision.revision in from_029_up
        assert wrapper.includes_revision(config, revision.revision, MECHANISM_REVISION) is expected, revision.revision


def test_includes_revision_is_true_for_a_descendant_of_029(wrapper, tmp_path):
    config = _descendant_history(tmp_path)
    assert wrapper.includes_revision(config, "z9descnd", MECHANISM_REVISION) is True
    assert wrapper.includes_revision(config, MECHANISM_REVISION, "z9descnd") is False
