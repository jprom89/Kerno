"""Offline tests for the grant-runtime and revoke-runtime commands of scripts/migrate_test_database.py.

What:  Drives the two C2 owner commands against a scripted safety module and a
       fake owner connection, and checks their routing, the allow-listed
       grant-runtime --profile option, their fixed statements per profile,
       every refusal, the order of checks, statements, lock re-proofs and
       commit, the rollback and exit code on each failure, and
       includes_revision against the real Alembic history.
Why:   PILOT-READY-002 (docs/pilot_readiness_kickoff.md §2.7 step 5) and
       PILOT-READY-003 (issue #20). These commands change what the runtime
       login may do, so a refusal must leave nothing changed, success must
       commit only while the workflow lock is still held, and a profile must
       be one of two fixed keys that only choose between fixed statements,
       never text that reaches SQL. Nothing here opens a database connection.
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
LOOKUP_REVISION = "e6f7g8h9"
PREVIOUS_REVISION = "c4d5e6f7"
PROFILES = ("full", "pilot")
TARGET_IDENTITY = "kerno_test@127.0.0.1:5432/kerno_test"

# grant-runtime's statements before profiles existed (PILOT-READY-002); the
# default and "--profile full" must still run exactly these.
PREVIOUS_FULL_STATEMENTS = (
    "GRANT CONNECT ON DATABASE kerno_test TO kerno_test_app",
    "CALL kerno_bootstrap.apply_runtime_grants('kerno_test_app', 'full')",
)

# (can_login, superuser, bypass_rls, create_database, create_role, replication, memberships, owned_objects)
CLEAN_ROLE = (True, False, False, False, False, False, 0, 0)
_ROLE_FIELDS = (
    "can_login", "superuser", "bypass_rls", "create_database", "create_role", "replication",
    "memberships", "owned_objects",
)
_DIRTY_VALUES = {"can_login": False, "memberships": 1, "owned_objects": 2}

_ROLE_OR_DATABASE_DDL = re.compile(r"\b(CREATE|ALTER|DROP)\s+(ROLE|USER|DATABASE)\b", re.IGNORECASE)

# The words a fixed statement may contain outside its quoted literals.
_STATEMENT_VOCABULARY = {
    "GRANT", "CONNECT", "ON", "DATABASE", "kerno_test", "TO", "kerno_test_app", "CALL",
    "kerno_bootstrap.apply_runtime_grants",
}

# Each runnable command line, as run_runtime_grant_command receives it: the
# profile argument, or None when the caller leaves it out.
COMMANDS_AND_PROFILES = [
    pytest.param(GRANT, None, id="grant-runtime"),
    pytest.param(GRANT, "full", id="grant-runtime --profile full"),
    pytest.param(GRANT, "pilot", id="grant-runtime --profile pilot"),
    pytest.param(REVOKE, None, id="revoke-runtime"),
]


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
    """Answers the wrapper's four checks from its connection's script and records every statement."""

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

    def __init__(
        self, wrapper, calls, *, mechanism=True, lookup=True, role=CLEAN_ROLE, assignment=(), fail_on=None
    ) -> None:
        """Script the answers to the mechanism, profile-lookup, role and assignment checks."""
        self.calls = calls
        self.labels = {
            wrapper._MECHANISM_PRESENT_SQL: "check:mechanism",
            wrapper._PROFILE_LOOKUP_PRESENT_SQL: "check:lookup",
            wrapper._RUNTIME_ROLE_SQL: "check:role",
            wrapper._ASSIGNMENT_SQL: "check:assignment",
        }
        self.answers = {
            wrapper._MECHANISM_PRESENT_SQL: (mechanism,),
            wrapper._PROFILE_LOOKUP_PRESENT_SQL: (lookup,),
            wrapper._RUNTIME_ROLE_SQL: role,
        }
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


def _effective(profile: str | None) -> str:
    """The profile a grant applies: the one given, or "full" when the argument is left out."""
    return profile or "full"


def _expected_assignment(command: str, profile: str | None = None) -> list[tuple[str, str]]:
    """What the assignment record holds after a successful command."""
    return [(RUNTIME_ROLE, _effective(profile))] if command == GRANT else []


def _scripted_connection(wrapper, calls, command, profile=None, **script) -> _FakeConnection:
    """A connection whose final assignment matches the command and profile unless the script overrides it."""
    script.setdefault("assignment", _expected_assignment(command, profile))
    return _FakeConnection(wrapper, calls, **script)


def _at_revision(monkeypatch, wrapper, revision) -> None:
    """Make the target report revision as its current Alembic revision, without connecting."""
    def current_revision(target):
        """Return the scripted revision."""
        return revision

    monkeypatch.setattr(wrapper, "current_revision", current_revision)


def _run(wrapper, command, connection, calls, lose_after=None, profile=None) -> int:
    """Run the command directly with the real safety module, a recording session and a scripted connect.

    profile=None leaves the argument out, so the function's own default applies.
    """
    session = _RecordingSession(calls, lose_after)
    connect = _RecordingConnect(calls, connection)
    if profile is None:
        return wrapper.run_runtime_grant_command(command, session, make_target(), safety, connect=connect)
    return wrapper.run_runtime_grant_command(command, session, make_target(), safety, connect=connect, profile=profile)


def _statements(wrapper, command, profile=None) -> tuple[str, ...]:
    """The fixed statements the command runs."""
    if command == GRANT:
        return wrapper.GRANT_RUNTIME_STATEMENTS[_effective(profile)]
    return wrapper.REVOKE_RUNTIME_STATEMENTS


def _checks(command) -> list[str]:
    """The precondition checks the command runs before its statements: grant-runtime also checks the lookup."""
    if command == GRANT:
        return ["check:mechanism", "check:lookup", "check:role"]
    return ["check:mechanism", "check:role"]


def _success_calls(wrapper, command, profile=None) -> list[str]:
    """The exact order of a successful command: checks, statements, assignment, re-proof, commit, close, re-proof."""
    return [
        "held?", "connect", *_checks(command), *_statements(wrapper, command, profile),
        "check:assignment", "held?", "commit", "close", "held?",
    ]


def _done_line(command, profile=None) -> str:
    """The exact success line: grant-runtime names the profile it applied, revoke-runtime names none."""
    if command == GRANT:
        return f"done: {GRANT} {RUNTIME_ROLE} on {TARGET_IDENTITY} (profile {_effective(profile)})"
    return f"done: {REVOKE} {RUNTIME_ROLE} on {TARGET_IDENTITY}"


def _route_connections_to(monkeypatch, wrapper, connect) -> None:
    """Keep open_owner_connection's real behaviour but hand it the recording connect instead of psycopg2's."""
    original = wrapper.open_owner_connection

    def open_owner_connection(target, command, connect_override=None):
        """Call the real opener with the recording connect."""
        return original(target, command, connect)

    monkeypatch.setattr(wrapper, "open_owner_connection", open_owner_connection)


def _main_run(wrapper, monkeypatch, argv, command, profile=None):
    """Run main(argv) at the lookup revision with a scripted safety module and connection; return what it did."""
    fake = _GuardedSafety()
    connection = _scripted_connection(wrapper, fake.calls, command, profile)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    _route_connections_to(monkeypatch, wrapper, _RecordingConnect(fake.calls, connection))
    code = wrapper.main(argv, safety=fake)
    return code, fake, connection


def _descendant_history(tmp_path):
    """An Alembic config over a copy of the history with one extra revision on top of the current head, 030."""
    from alembic.config import Config

    migrations = tmp_path / "migrations"
    shutil.copytree(safety.REPOSITORY_ROOT / "migrations", migrations, ignore=shutil.ignore_patterns("__pycache__"))
    (migrations / "versions" / "999_descendant_probe.py").write_text(
        '"""Probe revision on top of 030."""\n'
        'revision = "z9descnd"\n'
        f'down_revision = "{LOOKUP_REVISION}"\n'
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


@pytest.mark.parametrize("argv, expected", [([GRANT], (GRANT, "full")), ([REVOKE], (REVOKE, None))])
def test_parse_arguments_accepts_each_runtime_command_alone(wrapper, argv, expected):
    assert wrapper.parse_arguments(argv) == expected


@pytest.mark.parametrize("profile", PROFILES)
def test_parse_arguments_accepts_an_allow_listed_profile_for_grant_runtime(wrapper, profile):
    assert wrapper.parse_arguments([GRANT, "--profile", profile]) == (GRANT, profile)


@pytest.mark.parametrize(
    "argv",
    [
        [GRANT, "full"], [REVOKE, RUNTIME_ROLE], [GRANT, ""], [GRANT, GRANT], [REVOKE, "pilot", "x"],
        ["GRANT-RUNTIME"], ["grant_runtime"], ["revoke"],
        [GRANT, "--profile", "Pilot"], [GRANT, "--profile", "FULL"], [GRANT, "--profile", ""],
        [GRANT, "--profile", " pilot"], [GRANT, "--profile", "staging"],
        [GRANT, "--profile", "full; DROP ROLE kerno_test_app"], [GRANT, "--profile", "pilot", "x"],
        [GRANT, "--profile", "pilot", "--profile", "full"], [GRANT, "--profile"], [GRANT, "--profile=pilot"],
        [GRANT, "-p", "pilot"], [GRANT, "pilot", "--profile"], ["--profile", "pilot", GRANT],
        [GRANT, "--role", "x"], [GRANT, "--role", RUNTIME_ROLE], [GRANT, "--target", "x"],
        [GRANT, "--profile", "pilot", "--role", "x"],
        [REVOKE, "--profile", "pilot"], [REVOKE, "--profile", "full"], [REVOKE, "--role", "x"],
    ],
)
def test_parse_arguments_rejects_runtime_commands_with_extra_or_altered_arguments(wrapper, argv):
    assert wrapper.parse_arguments(argv) is None


@pytest.mark.parametrize(
    "argv", [[GRANT, "pilot"], [GRANT, "--profile", "staging"], [GRANT, "--profile"], [REVOKE, "--profile", "pilot"]]
)
def test_a_malformed_runtime_command_is_refused_before_any_bootstrap(wrapper, capsys, argv):
    fake = _GuardedSafety()
    assert wrapper.main(argv, safety=fake) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert fake.calls == []
    assert "grant-runtime [--profile full|pilot]" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv, command, profile",
    [
        pytest.param([GRANT], GRANT, None, id="grant-runtime"),
        pytest.param([GRANT, "--profile", "full"], GRANT, "full", id="grant-runtime --profile full"),
        pytest.param([GRANT, "--profile", "pilot"], GRANT, "pilot", id="grant-runtime --profile pilot"),
        pytest.param([REVOKE], REVOKE, None, id="revoke-runtime"),
    ],
)
def test_main_routes_the_command_through_the_migration_bootstrap_and_the_migrate_lock(
    wrapper, monkeypatch, capsys, argv, command, profile
):
    code, fake, connection = _main_run(wrapper, monkeypatch, argv, command, profile)
    assert code == wrapper.EXIT_DONE
    assert fake.calls == ["prepare", "lock:migrate", *_success_calls(wrapper, command, profile), "release"]
    assert fake.__dict__.get("unexpected") is None
    printed = capsys.readouterr().out
    assert printed.splitlines()[-1] == _done_line(command, profile)
    assert SECRET not in printed and "postgresql://" not in printed


def test_grant_runtime_without_the_option_runs_exactly_the_explicit_full_path_and_the_previous_statements(
    wrapper, monkeypatch, capsys
):
    _, default_fake, default_connection = _main_run(wrapper, monkeypatch, [GRANT], GRANT)
    default_output = capsys.readouterr().out
    # A second, freshly imported module: the first one's openers are already rerouted.
    explicit_wrapper = _load_wrapper()
    _, explicit_fake, explicit_connection = _main_run(
        explicit_wrapper, monkeypatch, [GRANT, "--profile", "full"], GRANT, "full"
    )
    assert default_fake.calls == explicit_fake.calls
    assert default_connection.executed == explicit_connection.executed
    assert default_output == capsys.readouterr().out
    executed = [sql for sql, _ in default_connection.executed]
    assert [sql for sql in executed if sql in PREVIOUS_FULL_STATEMENTS] == list(PREVIOUS_FULL_STATEMENTS)


@pytest.mark.parametrize("argv", [[GRANT], [GRANT, "--profile", "pilot"], [REVOKE]])
def test_main_releases_the_lock_when_the_command_is_refused(wrapper, monkeypatch, capsys, argv):
    fake = _GuardedSafety()
    _at_revision(monkeypatch, wrapper, PREVIOUS_REVISION)
    assert wrapper.main(argv, safety=fake) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert fake.calls == ["prepare", "lock:migrate", "held?", "release"]


@pytest.mark.parametrize("argv", [[GRANT], [GRANT, "--profile", "pilot"], [REVOKE]])
def test_main_releases_the_lock_when_the_lock_cannot_be_taken(wrapper, argv):
    fake = _GuardedSafety(session_error=safety.DatabaseTargetBusy("held by pytest"))
    assert wrapper.main(argv, safety=fake) == wrapper.EXIT_TARGET_REFUSED
    assert fake.calls == ["prepare", "lock:migrate", "release"]


@pytest.mark.parametrize("argv", [[GRANT], [GRANT, "--profile", "pilot"], [REVOKE]])
def test_main_without_configuration_never_takes_the_lock(wrapper, argv):
    fake = _GuardedSafety(prepare_error=safety.DatabaseTargetNotConfigured("not configured"))
    assert wrapper.main(argv, safety=fake) == wrapper.EXIT_CONFIGURATION_REFUSED
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
    assert wrapper.GRANT_RUNTIME_STATEMENTS == {
        "full": (
            "GRANT CONNECT ON DATABASE kerno_test TO kerno_test_app",
            "CALL kerno_bootstrap.apply_runtime_grants('kerno_test_app', 'full')",
        ),
        "pilot": (
            "GRANT CONNECT ON DATABASE kerno_test TO kerno_test_app",
            "CALL kerno_bootstrap.apply_runtime_grants('kerno_test_app', 'pilot')",
        ),
    }
    assert wrapper.GRANT_RUNTIME_STATEMENTS["full"] == PREVIOUS_FULL_STATEMENTS
    assert wrapper.REVOKE_RUNTIME_STATEMENTS == (
        "CALL kerno_bootstrap.revoke_runtime_grants('kerno_test_app')",
        "REVOKE CONNECT ON DATABASE kerno_test FROM kerno_test_app",
    )
    assert (wrapper.RUNTIME_PROFILES, wrapper.DEFAULT_RUNTIME_PROFILE, wrapper.PROFILE_OPTION) == (
        PROFILES, "full", "--profile",
    )
    revisions = (wrapper.RUNTIME_MECHANISM_REVISION, wrapper.PROFILE_LOOKUP_REVISION)
    assert revisions == (MECHANISM_REVISION, LOOKUP_REVISION)


@pytest.mark.parametrize("profile", PROFILES)
def test_each_profile_statement_names_only_the_fixed_role_database_and_that_profile(wrapper, profile):
    statements = wrapper.GRANT_RUNTIME_STATEMENTS[profile]
    assert set(re.findall(r"'([^']*)'", " ".join(statements))) == {RUNTIME_ROLE, profile}
    for statement in statements:
        unquoted = re.sub(r"'[^']*'", " ", statement)
        assert set(re.findall(r"[\w.]+", unquoted)) <= _STATEMENT_VOCABULARY, statement


@pytest.mark.parametrize("command, profile", COMMANDS_AND_PROFILES)
def test_nothing_the_command_executes_creates_alters_or_drops_a_role_or_database(
    wrapper, monkeypatch, command, profile
):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, command, connection, calls, profile=profile) == wrapper.EXIT_DONE
    executed = [sql for sql, _ in connection.executed]
    assert len(executed) == len(_checks(command)) + len(_statements(wrapper, command, profile)) + 1
    for sql in executed:
        assert not _ROLE_OR_DATABASE_DDL.search(sql), sql
        assert "kerno_dev" not in sql


# ── Refusals: nothing changes ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "command, profile, revision, required",
    [
        pytest.param(GRANT, None, MECHANISM_REVISION, LOOKUP_REVISION, id="grant-runtime at 029"),
        pytest.param(GRANT, "pilot", MECHANISM_REVISION, LOOKUP_REVISION, id="grant-runtime pilot at 029"),
        pytest.param(GRANT, None, PREVIOUS_REVISION, LOOKUP_REVISION, id="grant-runtime at 028"),
        pytest.param(GRANT, "pilot", None, LOOKUP_REVISION, id="grant-runtime pilot on an empty database"),
        pytest.param(REVOKE, None, PREVIOUS_REVISION, MECHANISM_REVISION, id="revoke-runtime at 028"),
        pytest.param(REVOKE, None, None, MECHANISM_REVISION, id="revoke-runtime on an empty database"),
    ],
)
def test_a_history_without_the_required_migration_is_refused_before_any_owner_connection(
    wrapper, monkeypatch, capsys, command, profile, revision, required
):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile)
    _at_revision(monkeypatch, wrapper, revision)
    assert _run(wrapper, command, connection, calls, profile=profile) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?"]
    assert f"migration {required} is not applied" in capsys.readouterr().out


@pytest.mark.parametrize("command, required", [(GRANT, LOOKUP_REVISION), (REVOKE, MECHANISM_REVISION)])
def test_the_history_check_asks_for_the_required_migration_by_its_revision(wrapper, monkeypatch, command, required):
    asked: list[tuple] = []

    def includes_revision(config, current, required_revision):
        """Record the question and answer no."""
        asked.append((current, required_revision))
        return False

    _at_revision(monkeypatch, wrapper, "y0z1a2b3")
    monkeypatch.setattr(wrapper, "includes_revision", includes_revision)
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command)
    assert _run(wrapper, command, connection, calls) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert asked == [("y0z1a2b3", required)]
    assert "connect" not in calls


def test_revoke_runtime_needs_only_029_and_never_checks_the_profile_lookup(wrapper, monkeypatch, capsys):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, REVOKE, lookup=False)
    _at_revision(monkeypatch, wrapper, MECHANISM_REVISION)
    assert _run(wrapper, REVOKE, connection, calls) == wrapper.EXIT_DONE
    assert calls == _success_calls(wrapper, REVOKE) and "check:lookup" not in calls
    assert capsys.readouterr().out.splitlines()[-1] == _done_line(REVOKE)


@pytest.mark.parametrize("command, profile", COMMANDS_AND_PROFILES)
def test_missing_mechanism_objects_are_refused_and_rolled_back(wrapper, monkeypatch, capsys, command, profile):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile, mechanism=False)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, command, connection, calls, profile=profile) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?", "connect", "check:mechanism", "rollback", "close"]
    assert "procedures are missing" in capsys.readouterr().out


@pytest.mark.parametrize("profile", [None, *PROFILES])
def test_a_missing_profile_lookup_refuses_grant_runtime_and_rolls_back(wrapper, monkeypatch, capsys, profile):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, GRANT, profile, lookup=False)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, GRANT, connection, calls, profile=profile) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?", "connect", "check:mechanism", "check:lookup", "rollback", "close"]
    assert "the kerno_bootstrap profile lookup is missing; apply migration 030 first." in capsys.readouterr().out


@pytest.mark.parametrize("command, profile", COMMANDS_AND_PROFILES)
def test_a_missing_runtime_role_is_refused_and_rolled_back(wrapper, monkeypatch, capsys, command, profile):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile, role=None)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, command, connection, calls, profile=profile) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?", "connect", *_checks(command), "rollback", "close"]
    assert f"role {RUNTIME_ROLE} does not exist" in capsys.readouterr().out


@pytest.mark.parametrize("profile", PROFILES)
def test_the_role_check_asks_about_the_fixed_role_only(wrapper, monkeypatch, profile):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, GRANT, profile)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, GRANT, connection, calls, profile=profile) == wrapper.EXIT_DONE
    role_checks = [parameters for sql, parameters in connection.executed if sql == wrapper._RUNTIME_ROLE_SQL]
    assert role_checks == [(RUNTIME_ROLE,)]


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("field", _ROLE_FIELDS)
def test_grant_runtime_refuses_a_role_with_one_invalid_attribute(wrapper, monkeypatch, capsys, field, profile):
    position = _ROLE_FIELDS.index(field)
    dirty_role = list(CLEAN_ROLE)
    dirty_role[position] = _DIRTY_VALUES.get(field, True)
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, GRANT, profile, role=tuple(dirty_role))
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, GRANT, connection, calls, profile=profile) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?", "connect", *_checks(GRANT), "rollback", "close"]
    expected_problem = "cannot log in" if field == "can_login" else field
    assert expected_problem in capsys.readouterr().out


def test_grant_runtime_names_every_invalid_attribute_at_once(wrapper, monkeypatch, capsys):
    calls: list[str] = []
    every_problem = (False, True, True, True, True, True, 3, 4)
    connection = _scripted_connection(wrapper, calls, GRANT, role=every_problem)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
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
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, REVOKE, connection, calls) == wrapper.EXIT_DONE
    assert calls == _success_calls(wrapper, REVOKE)


@pytest.mark.parametrize("command", [GRANT, REVOKE])
@pytest.mark.parametrize(
    "profile", ["Pilot", "FULL", "", " full", "staging", "full; DROP ROLE kerno_test_app", None],
    ids=["Pilot", "FULL", "empty", "padded", "staging", "injection", "None"],
)
def test_a_profile_outside_the_allow_list_is_refused_before_any_history_check_or_connection(
    wrapper, monkeypatch, capsys, command, profile
):
    def current_revision(target):
        """Fail loudly: the profile must be refused before the history is read."""
        raise AssertionError("the history was read for a profile outside the allow-list")

    monkeypatch.setattr(wrapper, "current_revision", current_revision)
    calls: list[str] = []
    session = _RecordingSession(calls)
    connect = _RecordingConnect(calls, _scripted_connection(wrapper, calls, command))
    code = wrapper.run_runtime_grant_command(command, session, make_target(), safety, connect=connect, profile=profile)
    assert code == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls == ["held?"] and connect.requests == []
    printed = capsys.readouterr().out
    assert "refused: the grant profile must be 'full' or 'pilot'." in printed and "DROP" not in printed


# ── Success, verification and the lock ──────────────────────────────────────


@pytest.mark.parametrize("command, profile", COMMANDS_AND_PROFILES)
def test_success_checks_then_runs_the_statements_verifies_and_commits_once_under_the_lock(
    wrapper, monkeypatch, capsys, command, profile
):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, command, connection, calls, profile=profile) == wrapper.EXIT_DONE
    assert calls == _success_calls(wrapper, command, profile)
    assert calls.count("commit") == 1 and "rollback" not in calls
    assert connection.autocommit is False
    assert capsys.readouterr().out.splitlines() == [_done_line(command, profile)]


@pytest.mark.parametrize(
    "command, profile, wrong_assignment",
    [
        (GRANT, None, []),
        (GRANT, None, [(RUNTIME_ROLE, "pilot")]),
        (GRANT, None, [("someone_else", "full")]),
        (GRANT, None, [(RUNTIME_ROLE, "full"), ("someone_else", "full")]),
        (GRANT, "full", [(RUNTIME_ROLE, "pilot")]),
        (GRANT, "pilot", []),
        (GRANT, "pilot", [(RUNTIME_ROLE, "full")]),
        (GRANT, "pilot", [("someone_else", "pilot")]),
        (GRANT, "pilot", [(RUNTIME_ROLE, "pilot"), ("someone_else", "full")]),
        (REVOKE, None, [(RUNTIME_ROLE, "full")]),
        (REVOKE, None, [(RUNTIME_ROLE, "pilot")]),
    ],
)
def test_an_assignment_that_does_not_match_the_command_is_rolled_back_and_refused(
    wrapper, monkeypatch, capsys, command, profile, wrong_assignment
):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile, assignment=wrong_assignment)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, command, connection, calls, profile=profile) == wrapper.EXIT_CONFIGURATION_REFUSED
    assert calls[-3:] == ["check:assignment", "rollback", "close"]
    assert "commit" not in calls
    printed = capsys.readouterr().out
    assert "is not what was applied" in printed and "done:" not in printed


@pytest.mark.parametrize("command, profile", COMMANDS_AND_PROFILES)
def test_a_lock_lost_before_the_commit_rolls_back_and_stops(wrapper, monkeypatch, capsys, command, profile):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, command, connection, calls, lose_after=1, profile=profile) == wrapper.EXIT_TARGET_REFUSED
    assert calls[-4:] == ["check:assignment", "held?", "rollback", "close"]
    assert "commit" not in calls
    printed = capsys.readouterr().out
    assert "stopped:" in printed and "done:" not in printed


@pytest.mark.parametrize("command, profile", COMMANDS_AND_PROFILES)
def test_a_lock_lost_after_the_commit_is_not_reported_as_success(wrapper, monkeypatch, capsys, command, profile):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, command, connection, calls, lose_after=2, profile=profile) == wrapper.EXIT_TARGET_REFUSED
    assert calls[-3:] == ["commit", "close", "held?"]
    assert "done:" not in capsys.readouterr().out


@pytest.mark.parametrize("command, profile", COMMANDS_AND_PROFILES)
def test_a_driver_error_rolls_back_and_prints_no_credential(wrapper, monkeypatch, capsys, command, profile):
    calls: list[str] = []
    failing_statement = _statements(wrapper, command, profile)[-1]
    connection = _scripted_connection(wrapper, calls, command, profile, fail_on=failing_statement)
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    assert _run(wrapper, command, connection, calls, profile=profile) == wrapper.EXIT_MIGRATION_FAILED
    assert calls[-3:] == [failing_statement, "rollback", "close"]
    assert "commit" not in calls
    printed = capsys.readouterr().out
    assert f"{command} failed: OperationalError" in printed
    assert SECRET not in printed and "postgresql://" not in printed


@pytest.mark.parametrize("profile", [None, *PROFILES])
def test_a_guard_refusal_while_connecting_is_a_target_refusal(wrapper, monkeypatch, capsys, profile):
    calls: list[str] = []
    session = _RecordingSession(calls)
    connect = _RecordingConnect(calls, error=safety.DatabaseTargetRejected("lock is not held by this session"))
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    code = wrapper.run_runtime_grant_command(
        GRANT, session, make_target(), safety, connect=connect, profile=_effective(profile)
    )
    assert code == wrapper.EXIT_TARGET_REFUSED
    assert calls == ["held?", "connect"]


# ── The owner connection ────────────────────────────────────────────────────


@pytest.mark.parametrize("command, profile", COMMANDS_AND_PROFILES)
def test_the_owner_connection_targets_the_owner_url_with_a_guard_application_name(
    wrapper, monkeypatch, command, profile
):
    calls: list[str] = []
    connection = _scripted_connection(wrapper, calls, command, profile)
    connect = _RecordingConnect(calls, connection)
    target = make_target()
    _at_revision(monkeypatch, wrapper, LOOKUP_REVISION)
    session = _RecordingSession(calls)
    code = wrapper.run_runtime_grant_command(
        command, session, target, safety, connect=connect, profile=_effective(profile)
    )
    assert code == wrapper.EXIT_DONE
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


def test_includes_revision_is_true_for_the_profile_lookup_only_from_030_onwards_in_the_real_history(wrapper):
    from alembic.script import ScriptDirectory

    config = wrapper.alembic_config()
    script = ScriptDirectory.from_config(config)
    assert wrapper.includes_revision(config, LOOKUP_REVISION, LOOKUP_REVISION) is True
    assert wrapper.includes_revision(config, script.get_current_head(), LOOKUP_REVISION) is True
    assert wrapper.includes_revision(config, MECHANISM_REVISION, LOOKUP_REVISION) is False
    assert wrapper.includes_revision(config, None, LOOKUP_REVISION) is False
    from_030_up = {revision.revision for revision in script.iterate_revisions("heads", MECHANISM_REVISION)}
    assert LOOKUP_REVISION in from_030_up and MECHANISM_REVISION not in from_030_up
    for revision in script.walk_revisions():
        expected = revision.revision in from_030_up
        assert wrapper.includes_revision(config, revision.revision, LOOKUP_REVISION) is expected, revision.revision


def test_includes_revision_is_true_for_a_descendant_of_029_and_030(wrapper, tmp_path):
    config = _descendant_history(tmp_path)
    assert wrapper.includes_revision(config, "z9descnd", MECHANISM_REVISION) is True
    assert wrapper.includes_revision(config, "z9descnd", LOOKUP_REVISION) is True
    assert wrapper.includes_revision(config, MECHANISM_REVISION, "z9descnd") is False
    assert wrapper.includes_revision(config, LOOKUP_REVISION, "z9descnd") is False
