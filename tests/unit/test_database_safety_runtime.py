"""Offline proof of the C2 runtime-identity guard and its behaviour table — in-process checks.

What:  Proves, with no real database, how a pytest process treats the second,
       non-owner login kerno_test_app (docs/pilot_readiness_kickoff.md §2.3
       items 1-9): the runtime pair comes from one source and is validated
       statically against the owner target without echoing a credential;
       an inherited KERNO_MIGRATION_DATABASE_URL is refused unless it names the
       owner target, and is then pinned to it; the live check reads the
       identity and the guard session's lock read-only on a one-off connection
       that is always closed; a login the server refuses is "not ready" and is
       remembered without reconnecting; the runtime endpoint is authorised only
       after that check, only in the pytest workflow, and every runtime connect
       re-proves the lock; and the conftest helpers, fixtures and hooks follow
       every row of the item 8 behaviour table. For PILOT-READY-003 it also
       proves the --runtime-profile rules: collection keeps only the tests of
       the selected profile and reports the rest as deselected; the runtime
       login's assigned grant profile is read once, as that login, only after
       its identity is verified, and any mismatch, missing assignment or
       failed read stops the run without echoing a credential; and a required
       run that never verified the profile fails.
Why:   PILOT-READY-002 and PILOT-READY-003 require an offline unit test for
       each refusal and each row of that table. Everything here uses in-memory
       fakes, temporary files and monkeypatched conftest state, so the real
       run's skip accounting and runtime-profile record are never touched.
       Child-process proofs of the same rules are in
       tests/unit/test_database_safety_processes.py.
How:   pytest tests/unit/test_database_safety_runtime.py -v
"""

from __future__ import annotations

import types

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import pytest

from tests import _database_safety as safety
from tests import conftest
from tests.unit.test_database_safety import (
    FAKE_DEVELOPMENT_URL,
    GOOD_APPROVAL,
    GOOD_IDENTITY,
    GOOD_URL,
    SECRET,
    FakeConnection,
    FakeCursor,
    FakeLockServer,
    _must_not_reach_libpq,
    _only_reads,
    _outcome_of,
    _select_list,
    isolate_skip_accounting,
    make_target,
)

URL_VARIABLE = safety.TEST_RUNTIME_DATABASE_URL_VARIABLE
APPROVAL_VARIABLE = safety.TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE
RUNTIME_FILE_VARIABLE = safety.TEST_RUNTIME_ENV_FILE_VARIABLE
MIGRATION_VARIABLE = safety.MIGRATION_DATABASE_URL_VARIABLE

RUNTIME_SECRET = "runtime-secret-qq"
RUNTIME_URL = f"postgresql://kerno_test_app:{RUNTIME_SECRET}@127.0.0.1:5432/kerno_test"
RUNTIME_APPROVAL = "kerno_test_app@127.0.0.1:5432/kerno_test"
OWNER_SETTINGS = {safety.TEST_DATABASE_URL_VARIABLE: GOOD_URL, safety.TEST_DATABASE_APPROVAL_VARIABLE: GOOD_APPROVAL}
RUNTIME_SETTINGS = {URL_VARIABLE: RUNTIME_URL, APPROVAL_VARIABLE: RUNTIME_APPROVAL}
GOOD_RUNTIME_IDENTITY = (
    "kerno_test", "kerno_test_app", "kerno_test_app", "127.0.0.1", 5432,
    False, False, False, False, False, 0, "kerno_test", safety.DISPOSABLE_DATABASE_COMMENT,
    0, False, False, False,
)
# The literal key in the specification's same-lock query (§2.3 item 4).
_SPECIFIED_LOCK_KEY = (1262834254, 1413829460)
# What a driver error might quote back: both URLs and the runtime password.
_FOREIGN_ERROR = f"FATAL: refused for {RUNTIME_URL} (password {RUNTIME_SECRET}); owner {GOOD_URL}"
_RUNTIME_KEYWORDS = {"runtime_identity": 1, "integration": 1}


def _credential_free(text: str) -> bool:
    """True when text carries neither password and no connection URL."""
    return SECRET not in text and RUNTIME_SECRET not in text and "postgresql://" not in text


def make_runtime_target(url: str = RUNTIME_URL) -> safety.DatabaseTarget:
    """Resolve the runtime target against the default owner target, with its canonical approval."""
    return safety.resolve_runtime_target(url, RUNTIME_APPROVAL, {}, make_target())


# ── Fakes: the guard connection, the runtime check connection, the workflow ──


class GuardConnection(FakeConnection):
    """The owner's guard connection, which also reports its server pid as psycopg2's does."""

    def get_backend_pid(self) -> int:
        """Return this fake backend's pid."""
        return self.pid


def guard_connect(server: FakeLockServer, opened: list):
    """Return a connect function that opens guard connections with the owner's identity on one fake server."""
    def connect(url, application_name=None):
        """Open one guard connection."""
        connection = GuardConnection(server, GOOD_IDENTITY)
        opened.append(connection)
        return connection
    return connect


class RuntimeConnection(FakeConnection):
    """A runtime check connection that remembers how it was opened and can fail one statement."""

    def __init__(self, server: FakeLockServer, identity: tuple, url: str, application_name, fail_on=None) -> None:
        """Open on the fake server, reporting identity for the runtime identity query."""
        super().__init__(server, identity)
        self.url = url
        self.application_name = application_name
        self.fail_on = fail_on
        self.lock_probes: list[tuple] = []

    def cursor(self):
        """Return a cursor that also answers the same-lock proof."""
        return RuntimeCursor(self)


class RuntimeCursor(FakeCursor):
    """Adds the same-lock proof to the statements FakeCursor answers, plus a scripted driver error."""

    def _answer(self, text: str, params: tuple, connection: RuntimeConnection) -> list:
        """Answer the same-lock proof from the fake server's locks; defer everything else to FakeCursor."""
        if connection.fail_on and text.startswith(connection.fail_on):
            raise psycopg2.OperationalError(_FOREIGN_ERROR)
        if "AND pid = %s AND granted" in text:
            connection.lock_probes.append(params)
            held_by_that_pid = connection.server.locks.get(params[:2]) == params[3]
            return [(1 if held_by_that_pid and params[2] == 2 else 0,)]
        return super()._answer(text, params, connection)


def runtime_connect(server: FakeLockServer, opened: list, identity: tuple = GOOD_RUNTIME_IDENTITY, fail_on=None):
    """Return a connect function that opens runtime check connections on one fake server."""
    def connect(url, application_name=None):
        """Open one runtime check connection."""
        connection = RuntimeConnection(server, identity, url, application_name, fail_on)
        opened.append(connection)
        return connection
    return connect


def start_workflow(purpose: str = "pytest") -> types.SimpleNamespace:
    """Hold a fake guard session for the named workflow, with the runtime pair adopted but not yet verified."""
    server, guard_opened = FakeLockServer(), []
    state = safety.ProcessState(target=make_target(), runtime_target=make_runtime_target())
    safety.ensure_exclusive_session(purpose, state=state, connect=guard_connect(server, guard_opened))
    return types.SimpleNamespace(server=server, state=state, guard=guard_opened[0])


@pytest.fixture
def workflow():
    """A held pytest workflow on a fake server, released afterwards."""
    held = start_workflow()
    yield held
    safety.release_exclusive_session(held.state)


def verify(held: types.SimpleNamespace) -> None:
    """Pass the live runtime check against a matching identity."""
    safety.verify_runtime_identity(held.state, connect=runtime_connect(held.server, []))


def _lock_checks(guard: FakeConnection) -> int:
    """How many times the guard connection has re-proved the lock."""
    return sum("pid = pg_backend_pid()" in text for text in guard.executed)


class _PurposeSession:
    """A session that reports itself active and held for a named workflow, with no connection behind it."""

    active = True

    def __init__(self, purpose: str) -> None:
        """Remember the workflow name."""
        self.purpose = purpose

    def assert_held(self) -> None:
        """Always held."""
        return None


# ── Static settings: one source, the fixed role, the owner's endpoint ───────


def _bootstrap(environ: dict, tmp_path, read_runtime: bool = True) -> safety.ProcessState:
    """Bootstrap a fresh state against a fake environment; both settings files default to missing ones."""
    environ.setdefault(safety.TEST_ENV_FILE_VARIABLE, str(tmp_path / "absent.env.test"))
    environ.setdefault(RUNTIME_FILE_VARIABLE, str(tmp_path / "absent.env.test.runtime"))
    return safety.bootstrap_test_process(environ, safety.ProcessState(), {}, read_runtime=read_runtime)


def _runtime_file(tmp_path, text: str) -> str:
    """Write a .env.test.runtime holding text and return its path."""
    path = tmp_path / ".env.test.runtime"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _assignments(settings: dict) -> str:
    """Render settings as NAME=value lines."""
    return "".join(f"{name}={value}\n" for name, value in settings.items())


def test_an_absent_runtime_pair_adopts_no_runtime_target_and_leaves_the_owner_unchanged(tmp_path):
    environ = dict(OWNER_SETTINGS)
    state = _bootstrap(environ, tmp_path)
    assert state.problem is None and state.runtime_target is None and state.runtime_source is None
    assert state.target.identity == GOOD_APPROVAL and state.source == "the process environment"
    owner_pinned = environ["DATABASE_URL"] == GOOD_URL and environ[MIGRATION_VARIABLE] == GOOD_URL
    assert owner_pinned


@pytest.mark.parametrize("present", [URL_VARIABLE, APPROVAL_VARIABLE])
def test_half_a_runtime_pair_in_the_environment_is_a_problem(tmp_path, present):
    state = _bootstrap({**OWNER_SETTINGS, present: RUNTIME_SETTINGS[present]}, tmp_path)
    assert state.runtime_target is None and state.target is None
    assert "must be set, non-empty, in the process environment" in state.problem
    assert _credential_free(state.problem)


@pytest.mark.parametrize(
    "url, approval", [("", RUNTIME_APPROVAL), ("   ", RUNTIME_APPROVAL), ("", "")], ids=["empty url", "blank url", "both empty"]
)
def test_blank_runtime_values_are_a_problem_not_an_absent_pair(tmp_path, url, approval):
    state = _bootstrap({**OWNER_SETTINGS, URL_VARIABLE: url, APPROVAL_VARIABLE: approval}, tmp_path)
    assert state.runtime_target is None and "non-empty" in state.problem


@pytest.mark.parametrize("present", [URL_VARIABLE, APPROVAL_VARIABLE])
def test_half_a_runtime_pair_in_the_runtime_file_is_a_problem(tmp_path, present):
    runtime_file = _runtime_file(tmp_path, f"{present}={RUNTIME_SETTINGS[present]}\n")
    state = _bootstrap({**OWNER_SETTINGS, RUNTIME_FILE_VARIABLE: runtime_file}, tmp_path)
    assert state.runtime_target is None
    assert "must be set, non-empty, in .env.test.runtime" in state.problem
    assert _credential_free(state.problem)


@pytest.mark.parametrize(
    "in_environment",
    [RUNTIME_SETTINGS, {URL_VARIABLE: RUNTIME_URL}, {APPROVAL_VARIABLE: RUNTIME_APPROVAL}],
    ids=["both values", "url only", "approval only"],
)
def test_runtime_values_in_both_the_environment_and_the_file_are_a_problem(tmp_path, in_environment):
    runtime_file = _runtime_file(tmp_path, _assignments(RUNTIME_SETTINGS))
    state = _bootstrap({**OWNER_SETTINGS, **in_environment, RUNTIME_FILE_VARIABLE: runtime_file}, tmp_path)
    assert state.runtime_target is None
    assert "both in the process environment and in .env.test.runtime" in state.problem
    assert _credential_free(state.problem)


@pytest.mark.parametrize("extra", ["DATABASE_URL", safety.TEST_DATABASE_URL_VARIABLE, "KERNO_JWT_SECRET"])
def test_the_runtime_file_refuses_any_other_key_and_names_it_without_its_value(tmp_path, extra):
    text = _assignments(RUNTIME_SETTINGS) + f"{extra}=postgresql://kerno_dev:{SECRET}@127.0.0.1:5432/kerno_dev\n"
    state = _bootstrap({**OWNER_SETTINGS, RUNTIME_FILE_VARIABLE: _runtime_file(tmp_path, text)}, tmp_path)
    assert state.runtime_target is None and repr(extra) in state.problem
    assert _credential_free(state.problem)


def test_a_runtime_file_line_that_is_not_an_assignment_is_counted_never_echoed(tmp_path):
    # python-dotenv reads a bare line as a key named after the whole line —
    # here a pasted runtime URL with its password.
    text = _assignments(RUNTIME_SETTINGS) + f"{RUNTIME_URL}\n"
    state = _bootstrap({**OWNER_SETTINGS, RUNTIME_FILE_VARIABLE: _runtime_file(tmp_path, text)}, tmp_path)
    assert state.runtime_target is None
    assert "1 line(s) that are not NAME=value assignments (not shown)" in state.problem
    assert _credential_free(state.problem)


_UNSAFE_RUNTIME_SETTINGS = [
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@[::1]:5432/kerno_test", RUNTIME_APPROVAL,
        "same literal host, port and database", id="::1 while the owner names 127.0.0.1",
    ),
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@127.0.0.1:5433/kerno_test", RUNTIME_APPROVAL,
        "same literal host, port and database", id="another port",
    ),
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@127.0.0.1:5432/kerno_dev", RUNTIME_APPROVAL,
        "same literal host, port and database", id="another database",
    ),
    pytest.param(
        RUNTIME_URL + "?dbname=kerno_dev", RUNTIME_APPROVAL, "same literal host, port and database",
        id="database overridden in the query",
    ),
    pytest.param(
        f"postgresql://kerno_test:{RUNTIME_SECRET}@127.0.0.1:5432/kerno_test", "kerno_test@127.0.0.1:5432/kerno_test",
        "runtime role must be exactly 'kerno_test_app'", id="the owner role kerno_test",
    ),
    pytest.param(
        f"postgresql://kerno_dev:{RUNTIME_SECRET}@127.0.0.1:5432/kerno_test", RUNTIME_APPROVAL,
        "runtime role must be exactly 'kerno_test_app'", id="role kerno_dev",
    ),
    pytest.param(
        RUNTIME_URL + "?user=kerno_test", RUNTIME_APPROVAL, "runtime role must be exactly 'kerno_test_app'",
        id="role overridden in the query",
    ),
    pytest.param(
        RUNTIME_URL, "kerno_test_app@127.0.0.1:5433/kerno_test", "does not match the configured runtime target",
        id="approval names another port",
    ),
    pytest.param(RUNTIME_URL, GOOD_APPROVAL, "does not match the configured runtime target", id="owner's approval"),
    pytest.param(RUNTIME_URL, RUNTIME_URL, "does not match the configured runtime target", id="url as approval"),
    pytest.param(RUNTIME_URL + "?hostaddr=10.0.0.5", RUNTIME_APPROVAL, "may not set hostaddr", id="hostaddr"),
    pytest.param(RUNTIME_URL + "?options=-c%20role%3Dkerno_test", RUNTIME_APPROVAL, "may not set options", id="options"),
    pytest.param(RUNTIME_URL + "?service=dev", RUNTIME_APPROVAL, "may not set service", id="service"),
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@localhost:5432/kerno_test", RUNTIME_APPROVAL,
        "literal loopback address", id="localhost",
    ),
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@10.0.0.5:5432/kerno_test", RUNTIME_APPROVAL,
        "literal loopback address", id="remote host",
    ),
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@127.0.0.1/kerno_test", RUNTIME_APPROVAL, "state port",
        id="no port",
    ),
    pytest.param("::: not a url :::", RUNTIME_APPROVAL, "not a parseable", id="unparseable"),
]


@pytest.mark.parametrize("url, approval, reason", _UNSAFE_RUNTIME_SETTINGS)
def test_an_unsafe_runtime_url_or_approval_is_a_problem_that_echoes_no_credential(tmp_path, monkeypatch, url, approval, reason):
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    state = _bootstrap({**OWNER_SETTINGS, URL_VARIABLE: url, APPROVAL_VARIABLE: approval}, tmp_path)
    assert state.runtime_target is None and state.target is None
    assert reason in state.problem and _credential_free(state.problem)


def test_a_refused_runtime_bootstrap_leaves_no_pinned_owner_url_behind(tmp_path):
    environ = {**OWNER_SETTINGS, URL_VARIABLE: GOOD_URL, APPROVAL_VARIABLE: GOOD_APPROVAL}
    state = _bootstrap(environ, tmp_path)
    assert state.problem and state.target is None and state.runtime_target is None
    assert "DATABASE_URL" not in environ and MIGRATION_VARIABLE not in environ


@pytest.mark.parametrize("source", ["environment", "file"])
def test_runtime_settings_without_an_owner_target_are_a_problem(tmp_path, source):
    if source == "file":
        environ = {RUNTIME_FILE_VARIABLE: _runtime_file(tmp_path, _assignments(RUNTIME_SETTINGS))}
    else:
        environ = dict(RUNTIME_SETTINGS)
    state = _bootstrap(environ, tmp_path)
    assert state.runtime_target is None and "the runtime identity needs the owner target" in state.problem
    assert _credential_free(state.problem)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGOPTIONS", "PGTARGETSESSIONATTRS"])
def test_a_redirecting_libpq_variable_refuses_the_runtime_target(tmp_path, variable):
    with pytest.raises(safety.DatabaseTargetInvalid, match=variable):
        safety.resolve_runtime_target(RUNTIME_URL, RUNTIME_APPROVAL, {variable: "10.0.0.5"}, make_target())
    state = _bootstrap({**OWNER_SETTINGS, **RUNTIME_SETTINGS, variable: "10.0.0.5"}, tmp_path)
    assert state.runtime_target is None and variable in state.problem


@pytest.mark.parametrize("source", ["the process environment", ".env.test.runtime"])
def test_a_valid_runtime_pair_is_adopted_and_never_becomes_a_variable_the_application_reads(tmp_path, source):
    environ = dict(OWNER_SETTINGS)
    if source == ".env.test.runtime":
        environ[RUNTIME_FILE_VARIABLE] = _runtime_file(tmp_path, _assignments(RUNTIME_SETTINGS))
    else:
        environ.update(RUNTIME_SETTINGS)
    state = _bootstrap(environ, tmp_path)
    assert state.problem is None and state.runtime_source == source and not state.runtime_verified
    assert state.runtime_target.identity == RUNTIME_APPROVAL and state.target.identity == GOOD_APPROVAL
    runtime_url_kept = state.runtime_target.url == RUNTIME_URL
    owner_pinned = environ["DATABASE_URL"] == GOOD_URL and environ[MIGRATION_VARIABLE] == GOOD_URL
    assert runtime_url_kept and owner_pinned
    carriers = sorted(name for name, value in environ.items() if RUNTIME_SECRET in value)
    assert carriers == ([URL_VARIABLE] if source == "the process environment" else [])


@pytest.mark.parametrize(
    "runtime_settings",
    [RUNTIME_SETTINGS, {URL_VARIABLE: RUNTIME_URL}, {URL_VARIABLE: FAKE_DEVELOPMENT_URL, APPROVAL_VARIABLE: "x"}],
    ids=["valid pair", "half pair", "unsafe pair"],
)
def test_a_bootstrap_that_does_not_read_runtime_settings_ignores_them_and_refuses_the_runtime_endpoint(
    tmp_path, runtime_settings
):
    state = _bootstrap({**OWNER_SETTINGS, **runtime_settings}, tmp_path, read_runtime=False)
    assert state.problem is None and state.runtime_target is None and state.target.identity == GOOD_APPROVAL
    state.session = _PurposeSession("pytest")
    with pytest.raises(safety.DatabaseTargetRejected, match="something other than the approved") as refused:
        safety.authorize_connection(RUNTIME_URL, {}, state)
    assert _credential_free(str(refused.value))


def test_the_migration_bootstrap_never_reads_runtime_settings_and_cannot_reach_the_runtime_endpoint(tmp_path):
    runtime_file = _runtime_file(tmp_path, _assignments(RUNTIME_SETTINGS) + f"DATABASE_URL={FAKE_DEVELOPMENT_URL}\n")
    environ = {
        **OWNER_SETTINGS, URL_VARIABLE: RUNTIME_URL, RUNTIME_FILE_VARIABLE: runtime_file,
        safety.TEST_ENV_FILE_VARIABLE: str(tmp_path / "absent.env.test"),
    }
    state = safety.ProcessState()
    target = safety.prepare_migration_process(environ, state, {})
    assert target.identity == GOOD_APPROVAL and state.problem is None and state.runtime_target is None
    state.session = _PurposeSession("migrate")
    with pytest.raises(safety.DatabaseTargetRejected, match="something other than the approved") as refused:
        safety.authorize_connection(RUNTIME_URL, {}, state)
    assert _credential_free(str(refused.value))
    safety.authorize_connection(GOOD_URL, {}, state)


# ── KERNO_MIGRATION_DATABASE_URL: refused unless it names the owner target ──


_CONFLICTING_MIGRATION_URLS = [
    pytest.param(FAKE_DEVELOPMENT_URL, id="development database"),
    pytest.param(f"postgresql://kerno_dev:{SECRET}@127.0.0.1:5432/kerno_dev", id="kerno_dev on the approved endpoint"),
    pytest.param(f"postgresql://kerno_dev:{SECRET}@127.0.0.1:5432/kerno_test", id="role kerno_dev"),
    pytest.param(RUNTIME_URL, id="the runtime user"),
    pytest.param(f"postgresql://kerno_test:{SECRET}@127.0.0.1:5433/kerno_test", id="another port"),
    pytest.param(f"postgresql://kerno_test:{SECRET}@[::1]:5432/kerno_test", id="another loopback literal"),
    pytest.param(f"postgresql://kerno_test:{SECRET}@10.0.0.5:5432/kerno_test", id="remote host"),
    pytest.param(GOOD_URL + "?options=-c%20role%3Dpostgres", id="extra key options"),
    pytest.param(GOOD_URL + "?hostaddr=10.0.0.5", id="extra key hostaddr"),
    pytest.param("::: not a url :::", id="unparseable"),
]


@pytest.mark.parametrize("inherited", _CONFLICTING_MIGRATION_URLS)
def test_an_inherited_migration_url_naming_anything_but_the_owner_target_is_refused_not_overridden(inherited):
    environ = {MIGRATION_VARIABLE: inherited}
    with pytest.raises(safety.DatabaseTargetInvalid, match="unset KERNO_MIGRATION_DATABASE_URL") as refused:
        safety.pin_migration_database_url(environ, make_target())
    assert _credential_free(str(refused.value))
    left_as_inherited = environ[MIGRATION_VARIABLE] == inherited
    assert left_as_inherited


@pytest.mark.parametrize(
    "inherited",
    [
        pytest.param(GOOD_URL, id="the owner url"),
        pytest.param("host=127.0.0.1 port=5432 dbname=kerno_test user=kerno_test password=other-zz", id="keyword form"),
        pytest.param(GOOD_URL + "?application_name=alembic&sslmode=disable", id="allowed extra keys"),
        pytest.param("postgresql://kerno_test@127.0.0.1:5432/kerno_test", id="no password"),
    ],
)
def test_an_inherited_migration_url_naming_the_owner_target_is_accepted_and_pinned_to_exactly_the_owner_url(inherited):
    environ = {MIGRATION_VARIABLE: inherited}
    safety.pin_migration_database_url(environ, make_target())
    pinned = environ[MIGRATION_VARIABLE] == GOOD_URL
    assert pinned


def test_an_absent_migration_url_is_set_to_the_owner_url_and_stays_absent_without_a_target():
    environ: dict = {}
    safety.pin_migration_database_url(environ, make_target())
    pinned = environ.get(MIGRATION_VARIABLE) == GOOD_URL
    assert pinned
    unconfigured: dict = {}
    safety.pin_migration_database_url(unconfigured, None)
    assert MIGRATION_VARIABLE not in unconfigured


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_migration_url_counts_as_absent_and_is_replaced_by_the_owner_url(blank):
    # migrations/env.py falls back to DATABASE_URL on an empty value, so a
    # blank one names no destination; pinning still overwrites it.
    environ = {MIGRATION_VARIABLE: blank}
    safety.pin_migration_database_url(environ, make_target())
    pinned = environ[MIGRATION_VARIABLE] == GOOD_URL
    assert pinned
    safety.pin_migration_database_url({MIGRATION_VARIABLE: blank}, None)


@pytest.mark.parametrize("inherited", [GOOD_URL, FAKE_DEVELOPMENT_URL], ids=["owner-shaped", "development"])
def test_an_inherited_migration_url_is_refused_while_no_test_database_is_configured(tmp_path, inherited):
    with pytest.raises(safety.DatabaseTargetInvalid, match="set while no test database is configured") as refused:
        safety.pin_migration_database_url({MIGRATION_VARIABLE: inherited}, None)
    assert _credential_free(str(refused.value))
    state = _bootstrap({MIGRATION_VARIABLE: inherited}, tmp_path)
    assert state.target is None and "set while no test database is configured" in state.problem


@pytest.mark.parametrize("inherited", [FAKE_DEVELOPMENT_URL, RUNTIME_URL], ids=["development", "runtime user"])
def test_both_bootstraps_refuse_a_conflicting_migration_url_before_any_connection(tmp_path, monkeypatch, inherited):
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    pytest_state = _bootstrap({**OWNER_SETTINGS, MIGRATION_VARIABLE: inherited}, tmp_path)
    assert pytest_state.target is None and "unset KERNO_MIGRATION_DATABASE_URL" in pytest_state.problem
    assert _credential_free(pytest_state.problem)
    environ = {
        **OWNER_SETTINGS, MIGRATION_VARIABLE: inherited, safety.TEST_ENV_FILE_VARIABLE: str(tmp_path / "absent"),
    }
    with pytest.raises(safety.DatabaseTargetInvalid, match="unset KERNO_MIGRATION_DATABASE_URL") as refused:
        safety.prepare_migration_process(environ, safety.ProcessState(), {})
    assert _credential_free(str(refused.value))


def test_a_matching_inherited_migration_url_passes_both_bootstraps_and_becomes_the_owner_url(tmp_path):
    matching = "host=127.0.0.1 port=5432 dbname=kerno_test user=kerno_test"
    environ = {**OWNER_SETTINGS, MIGRATION_VARIABLE: matching}
    assert _bootstrap(environ, tmp_path).problem is None
    migration = {**OWNER_SETTINGS, MIGRATION_VARIABLE: matching, safety.TEST_ENV_FILE_VARIABLE: str(tmp_path / "absent")}
    safety.prepare_migration_process(migration, safety.ProcessState(), {})
    pinned = environ[MIGRATION_VARIABLE] == GOOD_URL and migration[MIGRATION_VARIABLE] == GOOD_URL
    assert pinned


# ── Live verification: identity, same-lock proof, read-only, once ───────────


def test_the_runtime_identity_query_columns_line_up_with_the_field_names():
    expected_fragments = [
        "current_database()", "session_user", "current_user", "inet_server_addr()", "inet_server_port()",
        "rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolreplication",
        "OR roleid = roles.oid", "datdba", "shobj_description", "deptype = 'o'",
        "has_database_privilege(current_database(), 'CREATE')", "has_database_privilege(current_database(), 'TEMP')",
        "has_schema_privilege('public', 'CREATE')",
    ]
    columns = _select_list(safety._RUNTIME_IDENTITY_SQL)
    assert len(columns) == len(safety._RUNTIME_IDENTITY_FIELDS) == len(expected_fragments) == len(GOOD_RUNTIME_IDENTITY)
    for column, fragment in zip(columns, expected_fragments):
        assert fragment in column, (column, fragment)


def test_the_same_lock_query_is_the_specified_one():
    text = " ".join(safety._GUARD_LOCK_VISIBLE_SQL.split())
    assert "locktype = 'advisory'" in text and "AND pid = %s AND granted" in text
    assert "database = (SELECT oid FROM pg_database WHERE datname = current_database())" in text
    assert (safety.EXCLUSIVE_LOCK_CLASS_KEY, safety.EXCLUSIVE_LOCK_OBJECT_KEY) == _SPECIFIED_LOCK_KEY


@pytest.mark.parametrize("server_address", ["127.0.0.1", "::1"])
def test_a_matching_runtime_identity_has_no_problems(server_address):
    identity = dict(zip(safety._RUNTIME_IDENTITY_FIELDS, GOOD_RUNTIME_IDENTITY))
    identity["server_address"] = server_address
    assert safety.runtime_identity_problems(identity, make_runtime_target()) == []


def test_a_matching_runtime_identity_is_verified_read_only_on_a_check_connection_that_is_closed(workflow):
    opened: list = []
    runtime = safety.verify_runtime_identity(workflow.state, connect=runtime_connect(workflow.server, opened))
    assert runtime is workflow.state.runtime_target and workflow.state.runtime_verified
    assert len(opened) == 1
    check = opened[0]
    opened_as_the_runtime = check.url == RUNTIME_URL
    assert opened_as_the_runtime and check.application_name == "kerno-test-runtime-check"
    assert check.executed[0] == "SET TRANSACTION READ ONLY" and _only_reads(check)
    assert check.rollbacks == 1 and check.closed
    assert check.lock_probes == [(*_SPECIFIED_LOCK_KEY, 2, workflow.guard.pid)]


def test_once_verified_later_calls_only_reprove_the_lock_and_open_nothing(workflow):
    opened: list = []
    connect = runtime_connect(workflow.server, opened)
    safety.verify_runtime_identity(workflow.state, connect=connect)
    checks_before = _lock_checks(workflow.guard)
    safety.verify_runtime_identity(workflow.state, connect=connect)
    assert len(opened) == 1 and _lock_checks(workflow.guard) == checks_before + 1


_RUNTIME_MISMATCHES = [
    ("session_user", "kerno_test", "session_user is 'kerno_test'"),
    ("session_user", "kerno_dev", "session_user is 'kerno_dev'"),
    ("current_user", "kerno_test", "current_user is 'kerno_test'"),
    ("database_owner", "kerno_test_app", "database owner is 'kerno_test_app'"),
    ("database_owner", "kerno_dev", "database owner is 'kerno_dev'"),
    ("database", "kerno_dev", "connected database is 'kerno_dev'"),
    ("server_port", 5433, "server port is 5433"),
    ("server_address", "10.0.0.5", "not a TCP loopback address"),
    ("server_address", None, "not a TCP loopback address"),
    ("superuser", True, "superuser privilege"),
    ("bypass_rls", True, "bypass_rls privilege"),
    ("create_database", True, "create_database privilege"),
    ("create_role", True, "create_role privilege"),
    ("replication", True, "replication privilege"),
    ("memberships", 1, "role memberships"),
    ("database_comment", None, "approval comment"),
    ("database_comment", "kerno:something-else", "approval comment"),
    ("owned_objects", 2, "owns database objects"),
    ("can_create_in_database", True, "create in database rights"),
    ("can_create_temporary", True, "create temporary rights"),
    ("can_create_in_public", True, "create in public rights"),
]


@pytest.mark.parametrize("field, value, expected", _RUNTIME_MISMATCHES)
def test_every_runtime_identity_mismatch_is_named_and_refused(workflow, field, value, expected):
    identity = dict(zip(safety._RUNTIME_IDENTITY_FIELDS, GOOD_RUNTIME_IDENTITY))
    identity[field] = value
    opened: list = []
    connect = runtime_connect(workflow.server, opened, tuple(identity.values()))
    with pytest.raises(safety.DatabaseTargetRejected) as refused:
        safety.verify_runtime_identity(workflow.state, connect=connect)
    assert expected in str(refused.value) and _credential_free(str(refused.value))
    assert not workflow.state.runtime_verified and workflow.state.runtime_not_ready is None
    assert opened[0].closed and _only_reads(opened[0])


def _assert_lock_not_visible(held: types.SimpleNamespace, connect, opened: list) -> None:
    """Verification must refuse because the guard's lock is not visible, and must leave nothing open."""
    with pytest.raises(safety.DatabaseTargetRejected, match="workflow lock is not visible") as refused:
        safety.verify_runtime_identity(held.state, connect=connect)
    assert _credential_free(str(refused.value))
    assert not held.state.runtime_verified and opened[0].closed and opened[0].lock_probes


def test_a_guard_pid_that_does_not_hold_the_lock_is_refused(workflow, monkeypatch):
    def another_pid() -> int:
        """A pid other than the lock holder's."""
        return workflow.guard.pid + 1000

    monkeypatch.setattr(workflow.state.session, "backend_pid", another_pid)
    opened: list = []
    _assert_lock_not_visible(workflow, runtime_connect(workflow.server, opened), opened)


def test_a_guard_lock_held_in_another_database_is_not_visible_and_is_refused(workflow):
    # A separate fake server stands in for pg_locks filtered to the runtime
    # session's own database: the guard's lock exists, but not there.
    opened: list = []
    _assert_lock_not_visible(workflow, runtime_connect(FakeLockServer(), opened), opened)


def test_a_refused_runtime_login_is_not_ready_redacted_and_remembered_without_reconnecting(workflow):
    attempts: list = []

    def refuse(url, application_name=None):
        """Refuse the login the way a server reports a bad password, quoting the URL back."""
        attempts.append(application_name)
        raise psycopg2.OperationalError(_FOREIGN_ERROR)

    with pytest.raises(safety.RuntimeIdentityNotReady) as first:
        safety.verify_runtime_identity(workflow.state, connect=refuse)
    message = str(first.value)
    assert "could not log in" in message and RUNTIME_APPROVAL in message and _credential_free(message)
    assert workflow.state.runtime_not_ready == message and not workflow.state.runtime_verified
    reopened: list = []
    with pytest.raises(safety.RuntimeIdentityNotReady) as again:
        safety.verify_runtime_identity(workflow.state, connect=runtime_connect(workflow.server, reopened))
    assert str(again.value) == message and attempts == ["kerno-test-runtime-check"] and reopened == []


@pytest.mark.parametrize(
    "failing", ["SELECT current_database()", "SELECT count(*) FROM pg_locks"], ids=["identity query", "same-lock query"]
)
def test_a_driver_error_during_the_runtime_check_is_a_redacted_refusal_and_closes_the_connection(workflow, failing):
    opened: list = []
    connect = runtime_connect(workflow.server, opened, fail_on=failing)
    with pytest.raises(safety.DatabaseTargetRejected, match="could not verify") as refused:
        safety.verify_runtime_identity(workflow.state, connect=connect)
    assert _credential_free(str(refused.value))
    assert opened[0].closed and opened[0].rollbacks == 1
    assert not workflow.state.runtime_verified and workflow.state.runtime_not_ready is None


def _no_session(server: FakeLockServer) -> None:
    """No exclusive session at all."""
    return None


def _session_never_opened(server: FakeLockServer) -> safety.ExclusiveSession:
    """A pytest session object that was never acquired."""
    return safety.ExclusiveSession(make_target(), "pytest", connect=guard_connect(server, []))


def _session_that_lost_its_lock(server: FakeLockServer) -> safety.ExclusiveSession:
    """A pytest session that held the lock and then saw it lost, so it is no longer active."""
    session = _session_never_opened(server)
    session.acquire()
    server.locks.clear()
    with pytest.raises(safety.DatabaseExclusivityLost):
        session.assert_held()
    return session


def _held_migrate_session(server: FakeLockServer) -> safety.ExclusiveSession:
    """A held session for the migrate workflow."""
    session = safety.ExclusiveSession(make_target(), "migrate", connect=guard_connect(server, []))
    session.acquire()
    return session


@pytest.mark.parametrize(
    "make_session",
    [_no_session, _session_never_opened, _session_that_lost_its_lock, _held_migrate_session],
    ids=["no session", "never opened", "lock lost", "migrate workflow"],
)
def test_verification_is_refused_outside_a_held_pytest_session_and_opens_nothing(make_session):
    state = safety.ProcessState(
        target=make_target(), runtime_target=make_runtime_target(), session=make_session(FakeLockServer())
    )
    with pytest.raises(safety.DatabaseTargetRejected, match="only inside the held 'pytest' workflow session"):
        safety.verify_runtime_identity(state, connect=_must_not_reach_libpq)
    assert not state.runtime_verified
    safety.release_exclusive_session(state)


def test_verification_without_runtime_settings_is_not_configured(workflow):
    workflow.state.runtime_target = None
    with pytest.raises(safety.DatabaseTargetNotConfigured, match="runtime identity not configured"):
        safety.verify_runtime_identity(workflow.state, connect=_must_not_reach_libpq)


def test_verification_with_a_settings_problem_is_invalid():
    state = safety.ProcessState(problem="bad runtime settings", runtime_target=make_runtime_target())
    with pytest.raises(safety.DatabaseTargetInvalid, match="bad runtime settings"):
        safety.verify_runtime_identity(state, connect=_must_not_reach_libpq)


@pytest.mark.parametrize("earlier", ["nothing", "verified", "not ready"])
def test_a_lost_lock_stops_verification_before_any_runtime_connection_whatever_happened_earlier(workflow, earlier):
    workflow.state.runtime_verified = earlier == "verified"
    workflow.state.runtime_not_ready = "an earlier refusal" if earlier == "not ready" else None
    workflow.server.locks.clear()
    with pytest.raises(safety.DatabaseExclusivityLost):
        safety.verify_runtime_identity(workflow.state, connect=_must_not_reach_libpq)


def test_a_redirecting_libpq_variable_set_after_bootstrap_stops_verification(workflow, monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c role=kerno_test")
    with pytest.raises(safety.DatabaseTargetInvalid, match="PGOPTIONS"):
        safety.verify_runtime_identity(workflow.state, connect=_must_not_reach_libpq)
    assert not workflow.state.runtime_verified


# ── Authorisation: the runtime endpoint only after the check, only in pytest ─


_RUNTIME_KEYWORD_ARGUMENTS = {"host": "127.0.0.1", "port": 5432, "dbname": "kerno_test", "user": "kerno_test_app"}


def test_the_runtime_endpoint_is_refused_until_it_is_verified(workflow):
    with pytest.raises(safety.DatabaseTargetRejected, match="only after its live verification") as refused:
        safety.authorize_connection(RUNTIME_URL, {}, workflow.state)
    assert _credential_free(str(refused.value))
    with pytest.raises(safety.DatabaseTargetRejected, match="only after its live verification"):
        safety.authorize_connection(None, dict(_RUNTIME_KEYWORD_ARGUMENTS), workflow.state)


def test_after_verification_the_runtime_endpoint_is_allowed_in_the_pytest_workflow(workflow):
    verify(workflow)
    safety.authorize_connection(RUNTIME_URL, {}, workflow.state)
    safety.authorize_connection(RUNTIME_URL, {"application_name": "kerno-test-runtime"}, workflow.state)
    safety.authorize_connection(None, {**_RUNTIME_KEYWORD_ARGUMENTS, "password": "x"}, workflow.state)


def test_a_verified_runtime_endpoint_is_still_refused_in_the_migrate_workflow():
    state = safety.ProcessState(
        target=make_target(), runtime_target=make_runtime_target(),
        session=_PurposeSession("migrate"), runtime_verified=True,
    )
    with pytest.raises(safety.DatabaseTargetRejected, match="runtime-identity connection") as refused:
        safety.authorize_connection(RUNTIME_URL, {}, state)
    assert _credential_free(str(refused.value))
    safety.authorize_connection(GOOD_URL, {}, state)


_OTHER_ENDPOINTS = [
    pytest.param(FAKE_DEVELOPMENT_URL, {}, id="development database"),
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@127.0.0.1:5432/kerno_dev", {}, id="runtime user on another database"
    ),
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@127.0.0.1:5433/kerno_test", {}, id="runtime user on another port"
    ),
    pytest.param(
        f"postgresql://kerno_test_app:{RUNTIME_SECRET}@[::1]:5432/kerno_test", {}, id="runtime user on another literal"
    ),
    pytest.param(RUNTIME_URL, {"options": "-c role=kerno_test"}, id="runtime with an extra keyword"),
    pytest.param(RUNTIME_URL + "?target_session_attrs=any", {}, id="runtime with an extra query key"),
    pytest.param(None, {**_RUNTIME_KEYWORD_ARGUMENTS, "user": "postgres"}, id="another role"),
]


@pytest.mark.parametrize("dsn, keywords", _OTHER_ENDPOINTS)
def test_after_verification_every_other_endpoint_is_still_refused(workflow, dsn, keywords):
    verify(workflow)
    with pytest.raises(safety.DatabaseTargetRejected, match="something other than the approved") as refused:
        safety.authorize_connection(dsn, keywords, workflow.state)
    assert _credential_free(str(refused.value))


def test_every_runtime_connect_reproves_the_lock_and_none_is_allowed_after_it_is_lost(workflow):
    verify(workflow)
    checks_before = _lock_checks(workflow.guard)
    safety.authorize_connection(RUNTIME_URL, {}, workflow.state)
    assert _lock_checks(workflow.guard) == checks_before + 1
    workflow.server.locks.clear()
    with pytest.raises(safety.DatabaseExclusivityLost):
        safety.authorize_connection(RUNTIME_URL, {}, workflow.state)
    with pytest.raises(safety.DatabaseTargetRejected, match="outside the exclusive session"):
        safety.authorize_connection(RUNTIME_URL, {}, workflow.state)


def test_the_owner_endpoint_stays_allowed_before_and_after_runtime_verification(workflow):
    safety.authorize_connection(GOOD_URL, {}, workflow.state)
    verify(workflow)
    safety.authorize_connection(GOOD_URL, {}, workflow.state)


def test_the_process_wide_psycopg2_connect_applies_the_same_runtime_rule(workflow, monkeypatch):
    delegated: list = []

    def record(dsn, connection_factory=None, cursor_factory=None, **keyword_arguments):
        """Stand in for libpq: remember the delegated request instead of connecting."""
        delegated.append(keyword_arguments)
        return "fake-connection"

    monkeypatch.setattr(safety, "_STATE", workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", record)
    with pytest.raises(safety.DatabaseTargetRejected, match="only after its live verification"):
        psycopg2.connect(RUNTIME_URL, application_name="kerno-test-runtime")
    assert delegated == []
    verify(workflow)
    assert psycopg2.connect(RUNTIME_URL, application_name="kerno-test-runtime") == "fake-connection"
    assert delegated == [{"application_name": "kerno-test-runtime"}]


def test_redact_all_hides_both_urls_and_both_passwords():
    message = f"owner {GOOD_URL} password {SECRET}; runtime {RUNTIME_URL} password {RUNTIME_SECRET}"
    redacted = safety.redact_all(message, make_target(), make_runtime_target())
    assert _credential_free(redacted)
    assert redacted.count("<test-database-url>") == 2 and redacted.count("<password>") == 2
    untouched = safety.redact_all(message, None, None) == message
    assert untouched


# ── The conftest behaviour table (§2.3 item 8), helpers, fixtures and hooks ──


class _DeselectionHook:
    """The one hook pytest_collection_modifyitems calls on config.hook, recording each batch it deselects."""

    def __init__(self) -> None:
        """Nothing deselected yet."""
        self.deselected: list[list] = []

    def pytest_deselected(self, items) -> None:
        """Record one batch of deselected items."""
        self.deselected.append(list(items))


class _Options:
    """A pytest config fake whose database flags and runtime profile are set independently, with no parallel workers."""

    def __init__(
        self, require_live: bool = False, require_runtime: bool = False, runtime_profile: str = "full"
    ) -> None:
        """Remember the two flags and the --runtime-profile value."""
        self._flags = {
            "require_live_database": require_live, "require_runtime_identity": require_runtime,
            "runtime_profile": runtime_profile,
        }
        self.option = types.SimpleNamespace(numprocesses=None, dist="no")
        self.hook = _DeselectionHook()

    def getoption(self, name: str):
        """Return one option; an option name conftest should not ask for fails loudly."""
        return self._flags[name]


def isolate_conftest(
    monkeypatch, require_live=False, require_runtime=False, state=None, runtime_profile="full"
) -> None:
    """Give conftest fresh accounting, flags, runtime-profile records, final problems and process state for one test."""
    isolate_skip_accounting(monkeypatch, require_live, require_runtime, runtime_profile)
    monkeypatch.setattr(conftest, "_TEST_DATABASE", state or safety.ProcessState())
    monkeypatch.setattr(conftest, "_FINAL_EXCLUSIVITY_PROBLEMS", [])
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)


class _AssignedProfile:
    """A read_profile stand-in that reports one assigned grant profile and records each runtime it was asked about."""

    def __init__(self, assigned) -> None:
        """Script the profile the database would report."""
        self.assigned = assigned
        self.asked: list = []

    def __call__(self, runtime):
        """Record the question and return the scripted profile."""
        self.asked.append(runtime)
        return self.assigned


def _must_not_read_profile(runtime):
    """Fail loudly: the assigned profile may be read only after the runtime identity is verified."""
    raise AssertionError("the runtime profile was read although the runtime identity was not verified")


def record_verified_profile(state, profile: str = "full") -> None:
    """Verify profile for state's runtime via require_runtime_profile and a matching stub; isolate conftest first."""
    conftest.require_runtime_profile(
        _Options(runtime_profile=profile), state.runtime_target, state, _AssignedProfile(profile)
    )


def _runtime_outcome(config, state, read_profile=_must_not_read_profile) -> BaseException | None:
    """What require_runtime_identity did: returned (None), skipped, failed or stopped the run."""
    return _outcome_of(lambda: conftest.require_runtime_identity(config, state, read_profile))


def _report(keywords: dict, skipped: bool = True, nodeid: str = "probe::test") -> types.SimpleNamespace:
    """A test report as pytest_runtest_logreport sees it."""
    return types.SimpleNamespace(skipped=skipped, keywords=keywords, nodeid=nodeid)


def _finish(exitstatus=pytest.ExitCode.OK) -> pytest.ExitCode:
    """Run the session-finish hook on a fake session and return the exit status it leaves."""
    session = types.SimpleNamespace(exitstatus=exitstatus)
    conftest.pytest_sessionfinish(session, exitstatus)
    return session.exitstatus


def test_row_1_an_absent_runtime_pair_skips_runtime_tests_by_name(monkeypatch):
    state = safety.ProcessState(target=make_target())
    isolate_conftest(monkeypatch, state=state)
    conftest.pytest_configure(_Options())
    conftest.pytest_configure(_Options(require_live=True))
    for config in (_Options(), _Options(require_live=True)):
        outcome = _runtime_outcome(config, state)
        assert isinstance(outcome, pytest.skip.Exception), f"expected a skip, got {outcome!r}"
        assert str(outcome) == "runtime identity not configured"


def test_row_2_an_absent_runtime_pair_under_the_runtime_flag_is_refused_and_fails(monkeypatch):
    state = safety.ProcessState(target=make_target())
    isolate_conftest(monkeypatch, state=state)
    with pytest.raises(pytest.UsageError, match="no runtime identity is configured") as refused:
        conftest.pytest_configure(_Options(require_live=True, require_runtime=True))
    assert URL_VARIABLE in str(refused.value) and APPROVAL_VARIABLE in str(refused.value)
    outcome = _runtime_outcome(_Options(require_live=True, require_runtime=True), state)
    assert isinstance(outcome, pytest.fail.Exception), f"expected a failure, got {outcome!r}"
    assert "runtime identity required but not configured" in str(outcome)


_UNSAFE_RUNTIME_ENVIRONMENTS = [
    pytest.param({URL_VARIABLE: RUNTIME_URL}, id="one variable without the other"),
    pytest.param({URL_VARIABLE: "::: not a url :::", APPROVAL_VARIABLE: RUNTIME_APPROVAL}, id="malformed"),
    pytest.param(
        {URL_VARIABLE: f"postgresql://kerno_test:{RUNTIME_SECRET}@127.0.0.1:5432/kerno_test",
         APPROVAL_VARIABLE: "kerno_test@127.0.0.1:5432/kerno_test"},
        id="the owner role",
    ),
    pytest.param(
        {URL_VARIABLE: f"postgresql://kerno_test_app:{RUNTIME_SECRET}@127.0.0.1:5433/kerno_test",
         APPROVAL_VARIABLE: "kerno_test_app@127.0.0.1:5433/kerno_test"},
        id="another endpoint",
    ),
    pytest.param({**RUNTIME_SETTINGS, APPROVAL_VARIABLE: GOOD_APPROVAL}, id="wrong approval"),
    pytest.param({**RUNTIME_SETTINGS, "PGHOSTADDR": "10.0.0.5"}, id="redirecting setting"),
    pytest.param({**RUNTIME_SETTINGS, MIGRATION_VARIABLE: RUNTIME_URL}, id="migration url naming the runtime"),
]


@pytest.mark.parametrize("unsafe", _UNSAFE_RUNTIME_ENVIRONMENTS)
@pytest.mark.parametrize("flags", [(False, False), (True, False), (True, True)], ids=["no flag", "live", "live+runtime"])
def test_row_3_unsafe_runtime_configuration_stops_the_run_with_or_without_flags(monkeypatch, tmp_path, unsafe, flags):
    state = _bootstrap({**OWNER_SETTINGS, **unsafe}, tmp_path)
    isolate_conftest(monkeypatch, state=state)
    with pytest.raises(pytest.UsageError, match="test database configuration refused") as refused:
        conftest.pytest_configure(_Options(*flags))
    assert _credential_free(str(refused.value))


@pytest.mark.parametrize("require_runtime", [False, True], ids=["without the flag", "with the flag"])
def test_row_3_a_live_runtime_identity_that_is_really_the_owner_stops_the_run(workflow, monkeypatch, require_runtime):
    owner_reached = GOOD_IDENTITY + (0, False, False, False)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", runtime_connect(workflow.server, [], owner_reached))
    outcome = _runtime_outcome(_Options(require_live=True, require_runtime=require_runtime), workflow.state)
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == pytest.ExitCode.USAGE_ERROR
    assert "runtime identity refused" in str(outcome) and _credential_free(str(outcome))


def test_row_3_a_lock_lost_before_the_runtime_check_stops_the_run_as_interrupted(workflow, monkeypatch):
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    workflow.server.locks.clear()
    outcome = _runtime_outcome(_Options(require_live=True, require_runtime=True), workflow.state)
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == pytest.ExitCode.INTERRUPTED


@pytest.mark.parametrize(
    "error, code",
    [
        (safety.DatabaseTargetRejected("wrong identity"), pytest.ExitCode.USAGE_ERROR),
        (safety.DatabaseTargetInvalid("bad settings"), pytest.ExitCode.USAGE_ERROR),
        (safety.DatabaseTargetNotConfigured("no target"), pytest.ExitCode.USAGE_ERROR),
        (safety.DatabaseExclusivityLost("lost"), pytest.ExitCode.INTERRUPTED),
    ],
    ids=["rejected", "invalid", "not configured", "exclusivity lost"],
)
def test_require_runtime_identity_turns_every_refusal_into_a_stop_never_a_skip(monkeypatch, error, code):
    def refuse(state=None, connect=None):
        """Raise the scripted refusal."""
        raise error

    monkeypatch.setattr(safety, "verify_runtime_identity", refuse)
    state = safety.ProcessState(target=make_target(), runtime_target=make_runtime_target())
    outcome = _runtime_outcome(_Options(), state)
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == code


@pytest.mark.parametrize("require_runtime", [False, True], ids=["without the flag", "with the flag"])
def test_row_4_a_runtime_login_the_server_refuses_fails_each_runtime_test_and_never_skips(
    workflow, monkeypatch, require_runtime
):
    attempts: list = []

    def refuse(url, application_name=None):
        """Refuse the runtime login as the server does when the role is missing."""
        attempts.append(application_name)
        raise psycopg2.OperationalError(_FOREIGN_ERROR)

    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", refuse)
    config = _Options(require_live=True, require_runtime=require_runtime)
    first = _runtime_outcome(config, workflow.state)
    second = _runtime_outcome(config, workflow.state)
    for outcome in (first, second):
        assert isinstance(outcome, pytest.fail.Exception), f"expected a failure, got {outcome!r}"
        assert "runtime identity setup failure" in str(outcome) and _credential_free(str(outcome))
    assert len(attempts) == 1


def test_final_acceptance_a_verified_runtime_identity_is_returned_to_the_fixture(workflow, monkeypatch):
    isolate_conftest(monkeypatch, require_live=True, require_runtime=True, state=workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", runtime_connect(workflow.server, []))
    read_profile = _AssignedProfile("full")
    config = _Options(require_live=True, require_runtime=True)
    runtime = conftest.require_runtime_identity(config, workflow.state, read_profile)
    assert runtime is workflow.state.runtime_target and workflow.state.runtime_verified
    assert read_profile.asked == [runtime] and conftest._VERIFIED_RUNTIME_PROFILE == {"value": "full"}


def test_require_runtime_identity_reads_the_profile_once_and_only_after_the_identity_is_verified(workflow, monkeypatch):
    isolate_conftest(monkeypatch, require_live=True, state=workflow.state)
    events: list[str] = []
    real_verify = safety.verify_runtime_identity

    def verify_and_record(state=None, connect=None):
        """Run the real live check on the fake server, then record that it passed."""
        runtime = real_verify(state, connect=runtime_connect(workflow.server, []))
        events.append("identity verified")
        return runtime

    def read_profile(runtime):
        """Record the profile read and report the selected profile."""
        events.append("profile read")
        return "full"

    monkeypatch.setattr(safety, "verify_runtime_identity", verify_and_record)
    for _ in range(2):
        conftest.require_runtime_identity(_Options(require_live=True), workflow.state, read_profile)
    assert events == ["identity verified", "profile read", "identity verified"]


def _refuse_with(error):
    """Return a verify_runtime_identity stand-in that raises error."""
    def refuse(state=None, connect=None):
        """Raise the scripted refusal."""
        raise error
    return refuse


@pytest.mark.parametrize(
    "error, expected",
    [
        pytest.param(None, pytest.skip.Exception, id="not configured"),
        pytest.param(safety.RuntimeIdentityNotReady("login refused"), pytest.fail.Exception, id="not ready"),
        pytest.param(safety.DatabaseTargetRejected("wrong identity"), pytest.exit.Exception, id="refused"),
        pytest.param(safety.DatabaseExclusivityLost("lost"), pytest.exit.Exception, id="exclusivity lost"),
    ],
)
def test_an_unverified_runtime_identity_keeps_its_outcome_and_never_reaches_the_profile_read(
    monkeypatch, error, expected
):
    state = safety.ProcessState(target=make_target(), runtime_target=None if error is None else make_runtime_target())
    isolate_conftest(monkeypatch, require_live=True, state=state)
    if error is not None:
        monkeypatch.setattr(safety, "verify_runtime_identity", _refuse_with(error))
    outcome = _runtime_outcome(_Options(require_live=True), state, _must_not_read_profile)
    assert isinstance(outcome, expected), f"expected {expected.__name__}, got {outcome!r}"
    assert conftest._VERIFIED_RUNTIME_PROFILE == {"value": None}


# ── The assigned grant profile (PILOT-READY-003): read once, as the runtime ──


_PROFILES = ["full", "pilot"]


@pytest.mark.parametrize("profile", _PROFILES)
def test_a_matching_assigned_profile_passes_is_recorded_and_is_not_read_again(monkeypatch, profile):
    state = _configured_but_unproved()
    isolate_conftest(monkeypatch, require_live=True, require_runtime=True, state=state, runtime_profile=profile)
    read_profile = _AssignedProfile(profile)
    config = _Options(require_live=True, require_runtime=True, runtime_profile=profile)
    for _ in range(2):
        conftest.require_runtime_profile(config, state.runtime_target, state, read_profile)
    assert read_profile.asked == [state.runtime_target]
    assert conftest._VERIFIED_RUNTIME_PROFILE == {"value": profile}


@pytest.mark.parametrize(
    "selected, assigned",
    [
        pytest.param("full", "pilot", id="full run, pilot assigned"),
        pytest.param("pilot", "full", id="pilot run, full assigned"),
        pytest.param("full", None, id="full run, nothing assigned to this login"),
        pytest.param("pilot", None, id="pilot run, nothing assigned to this login"),
        pytest.param("full", "staging", id="unrecognised profile"),
    ],
)
def test_an_assigned_profile_other_than_the_selected_one_stops_the_run(monkeypatch, selected, assigned):
    state = _configured_but_unproved()
    isolate_conftest(monkeypatch, require_live=True, state=state, runtime_profile=selected)
    read_profile = _AssignedProfile(assigned)
    config = _Options(require_live=True, runtime_profile=selected)
    outcome = _outcome_of(lambda: conftest.require_runtime_profile(config, state.runtime_target, state, read_profile))
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == pytest.ExitCode.USAGE_ERROR
    message = str(outcome)
    assert message.startswith("runtime profile mismatch: the database assigns")
    assert repr(assigned) in message and RUNTIME_APPROVAL in message and f"--runtime-profile {selected}" in message
    assert _credential_free(message) and len(read_profile.asked) == 1
    assert conftest._VERIFIED_RUNTIME_PROFILE == {"value": None}


@pytest.mark.parametrize(
    "error_class",
    [psycopg2.OperationalError, psycopg2.errors.UndefinedFunction, psycopg2.errors.InsufficientPrivilege],
    ids=["connection refused", "lookup missing", "lookup not granted"],
)
def test_a_failed_profile_read_stops_the_run_without_echoing_a_credential(monkeypatch, error_class):
    state = _configured_but_unproved()
    isolate_conftest(monkeypatch, require_live=True, state=state)

    def fail(runtime):
        """Fail the read as the driver does, quoting both URLs and the runtime password back."""
        raise error_class(_FOREIGN_ERROR)

    config = _Options(require_live=True)
    outcome = _outcome_of(lambda: conftest.require_runtime_profile(config, state.runtime_target, state, fail))
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == pytest.ExitCode.USAGE_ERROR
    assert str(outcome).startswith("runtime profile could not be read: ") and _credential_free(str(outcome))
    assert "<test-database-url>" in str(outcome)
    assert conftest._VERIFIED_RUNTIME_PROFILE == {"value": None}


@pytest.mark.parametrize(
    "error, code, prefix",
    [
        (safety.DatabaseExclusivityLost("lost"), pytest.ExitCode.INTERRUPTED, "test database exclusivity lost: "),
        (safety.DatabaseTargetRejected("refused by the guard"), pytest.ExitCode.USAGE_ERROR, "runtime profile refused: "),
    ],
    ids=["lock lost", "guard refusal"],
)
def test_a_guard_error_while_reading_the_profile_stops_the_run_like_the_identity_check(monkeypatch, error, code, prefix):
    state = _configured_but_unproved()
    isolate_conftest(monkeypatch, require_live=True, state=state)

    def refuse(runtime):
        """Fail the read the way the guarded psycopg2.connect does."""
        raise error

    config = _Options(require_live=True)
    outcome = _outcome_of(lambda: conftest.require_runtime_profile(config, state.runtime_target, state, refuse))
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == code and str(outcome).startswith(prefix)
    assert conftest._VERIFIED_RUNTIME_PROFILE == {"value": None}


class _ProfileConnection:
    """A raw runtime connection that answers only the read-only profile lookup and records what happened to it."""

    def __init__(self, row, fail: bool = False) -> None:
        """Script the row the lookup returns, or a driver error on the lookup itself."""
        self.row = row
        self.fail = fail
        self.statements: list[str] = []
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> "_ProfileCursor":
        """Return a cursor over this connection."""
        return _ProfileCursor(self)

    def rollback(self) -> None:
        """Count the rollback."""
        self.rollbacks += 1

    def close(self) -> None:
        """Mark the connection closed."""
        self.closed = True


class _ProfileCursor:
    """Answers the two statements read_assigned_runtime_profile issues; any other statement fails the test."""

    def __init__(self, connection: _ProfileConnection) -> None:
        """Remember the connection."""
        self._connection = connection

    def execute(self, sql: str, params=None) -> None:
        """Record the statement, and raise on the lookup when scripted to."""
        assert sql in ("SET TRANSACTION READ ONLY", "SELECT kerno_bootstrap.session_runtime_profile()"), sql
        self._connection.statements.append(sql)
        if self._connection.fail and sql.startswith("SELECT"):
            raise psycopg2.errors.UndefinedFunction(_FOREIGN_ERROR)

    def fetchone(self):
        """Return the scripted row."""
        return self._connection.row


def _profile_connect(requests: list, connection: _ProfileConnection):
    """Return a stand-in for libpq that records each request and hands out connection."""
    def connect(dsn=None, connection_factory=None, cursor_factory=None, **keyword_arguments):
        """Record one request."""
        requests.append((dsn, keyword_arguments))
        return connection
    return connect


@pytest.mark.parametrize(
    "row, expected", [(("pilot",), "pilot"), (("full",), "full"), ((None,), None)], ids=["pilot", "full", "null"]
)
def test_the_profile_is_read_as_the_runtime_through_the_guard_read_only_on_one_closed_connection(
    workflow, monkeypatch, row, expected
):
    verify(workflow)
    requests: list = []
    connection = _ProfileConnection(row)
    monkeypatch.setattr(safety, "_STATE", workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _profile_connect(requests, connection))
    assert conftest.read_assigned_runtime_profile(workflow.state.runtime_target) == expected
    assert requests == [(RUNTIME_URL, {"application_name": "kerno-test-runtime-profile"})]
    assert connection.statements == ["SET TRANSACTION READ ONLY", "SELECT kerno_bootstrap.session_runtime_profile()"]
    assert connection.rollbacks == 1 and connection.closed


def test_a_failed_profile_lookup_still_closes_its_connection(workflow, monkeypatch):
    verify(workflow)
    connection = _ProfileConnection(None, fail=True)
    monkeypatch.setattr(safety, "_STATE", workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _profile_connect([], connection))
    with pytest.raises(psycopg2.Error):
        conftest.read_assigned_runtime_profile(workflow.state.runtime_target)
    assert connection.closed and len(connection.statements) == 2


def test_the_profile_read_is_refused_by_the_guard_before_the_runtime_is_verified(workflow, monkeypatch):
    monkeypatch.setattr(safety, "_STATE", workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    with pytest.raises(safety.DatabaseTargetRejected, match="only after its live verification") as refused:
        conftest.read_assigned_runtime_profile(workflow.state.runtime_target)
    assert _credential_free(str(refused.value))


@pytest.mark.parametrize("configured", [False, True], ids=["nothing configured", "both identities configured"])
def test_the_runtime_flag_without_the_live_flag_is_a_usage_error(monkeypatch, configured):
    state = safety.ProcessState()
    if configured:
        state = safety.ProcessState(target=make_target(), runtime_target=make_runtime_target())
    isolate_conftest(monkeypatch, state=state)
    with pytest.raises(pytest.UsageError, match="must be combined with --require-live-database"):
        conftest.pytest_configure(_Options(require_runtime=True))


@pytest.mark.parametrize("profile", _PROFILES)
def test_configure_accepts_the_final_acceptance_flags_when_both_identities_are_configured(monkeypatch, profile):
    state = safety.ProcessState(target=make_target(), runtime_target=make_runtime_target())
    isolate_conftest(monkeypatch, state=state)
    conftest.pytest_configure(_Options(require_live=True, require_runtime=True, runtime_profile=profile))
    assert conftest._REQUIRE_LIVE == {"enabled": True} and conftest._REQUIRE_RUNTIME == {"enabled": True}
    assert conftest._SELECTED_RUNTIME_PROFILE == {"value": profile}
    assert conftest._VERIFIED_RUNTIME_PROFILE == {"value": None}


_SKIP_KINDS = [
    pytest.param(_RUNTIME_KEYWORDS, "_RUNTIME_TEST_SKIPS", id="runtime"),
    pytest.param({"integration": 1}, "_LIVE_TEST_SKIPS", id="integration"),
    pytest.param({}, "_OTHER_SKIPS", id="other"),
]


@pytest.mark.parametrize("keywords, bucket", _SKIP_KINDS)
def test_each_skip_is_counted_once_under_its_own_kind(monkeypatch, keywords, bucket):
    isolate_conftest(monkeypatch)
    conftest.pytest_runtest_logreport(_report(keywords))
    conftest.pytest_runtest_logreport(_report(keywords, skipped=False, nodeid="probe::passed"))
    buckets = {name: getattr(conftest, name) for name in ("_RUNTIME_TEST_SKIPS", "_LIVE_TEST_SKIPS", "_OTHER_SKIPS")}
    assert buckets.pop(bucket) == ["probe::test"] and list(buckets.values()) == [[], []]


_NEVER_VERIFIED = "the runtime identity was never verified live (no runtime-identity test reached it)"
_NO_SESSION = "exclusive test-database session was never opened"


def _profile_never_verified(profile: str = "full") -> str:
    """The missing-proof entry for a run whose --runtime-profile was never verified against the database."""
    return f"the runtime login's assigned grant profile was never verified as {profile!r} (--runtime-profile)"


def _required_runtime_run(monkeypatch, state, runtime_profile: str = "full") -> None:
    """Isolate conftest for a --require-live-database --require-runtime-identity run over state."""
    isolate_conftest(monkeypatch, require_live=True, require_runtime=True, state=state, runtime_profile=runtime_profile)


def _configured_but_unproved() -> safety.ProcessState:
    """Both identities configured and valid, but no session opened and nothing verified."""
    return safety.ProcessState(target=make_target(), runtime_target=make_runtime_target())


@pytest.mark.parametrize("keywords, bucket", _SKIP_KINDS)
def test_under_the_runtime_flag_any_skip_turns_a_passing_run_into_a_failure(workflow, monkeypatch, keywords, bucket):
    verify(workflow)
    _required_runtime_run(monkeypatch, workflow.state)
    record_verified_profile(workflow.state)
    conftest.pytest_runtest_logreport(_report(keywords))
    assert _finish() == pytest.ExitCode.TESTS_FAILED
    assert conftest._MISSING_REQUIRED_PROOF == []


@pytest.mark.parametrize("profile", _PROFILES)
def test_under_the_runtime_flag_a_verified_run_without_skips_passes_and_a_failure_stays_a_failure(
    workflow, monkeypatch, profile
):
    verify(workflow)
    _required_runtime_run(monkeypatch, workflow.state, runtime_profile=profile)
    record_verified_profile(workflow.state, profile)
    assert _finish() == pytest.ExitCode.OK and conftest._MISSING_REQUIRED_PROOF == []
    conftest.pytest_runtest_logreport(_report({}))
    assert _finish(pytest.ExitCode.TESTS_FAILED) == pytest.ExitCode.TESTS_FAILED


def test_under_the_runtime_flag_an_unverified_runtime_identity_fails_a_run_without_skips(workflow, monkeypatch):
    _required_runtime_run(monkeypatch, workflow.state)
    record_verified_profile(workflow.state)
    assert _finish() == pytest.ExitCode.TESTS_FAILED
    assert conftest._MISSING_REQUIRED_PROOF == [_NEVER_VERIFIED]


def test_under_the_runtime_flag_a_run_that_never_opened_the_exclusive_session_fails(monkeypatch):
    state = safety.ProcessState(target=make_target(), runtime_target=make_runtime_target(), runtime_verified=True)
    _required_runtime_run(monkeypatch, state)
    record_verified_profile(state)
    assert _finish() == pytest.ExitCode.TESTS_FAILED
    assert len(conftest._MISSING_REQUIRED_PROOF) == 1 and _NO_SESSION in conftest._MISSING_REQUIRED_PROOF[0]


@pytest.mark.parametrize("profile", _PROFILES)
def test_under_the_runtime_flag_an_unverified_grant_profile_fails_an_otherwise_complete_run(
    workflow, monkeypatch, profile
):
    verify(workflow)
    _required_runtime_run(monkeypatch, workflow.state, runtime_profile=profile)
    assert _finish() == pytest.ExitCode.TESTS_FAILED
    assert conftest._MISSING_REQUIRED_PROOF == [_profile_never_verified(profile)]


def test_under_the_runtime_flag_a_profile_verified_as_the_other_one_is_still_missing(workflow, monkeypatch):
    verify(workflow)
    _required_runtime_run(monkeypatch, workflow.state, runtime_profile="pilot")
    record_verified_profile(workflow.state, "full")
    assert _finish() == pytest.ExitCode.TESTS_FAILED
    assert conftest._MISSING_REQUIRED_PROOF == [_profile_never_verified("pilot")]


@pytest.mark.parametrize(
    "prior",
    [pytest.ExitCode.TESTS_FAILED, pytest.ExitCode.INTERRUPTED, pytest.ExitCode.USAGE_ERROR,
     pytest.ExitCode.NO_TESTS_COLLECTED],
    ids=["failed", "interrupted", "usage error", "nothing collected"],
)
def test_an_unverified_grant_profile_is_reported_but_never_replaces_a_nonzero_outcome(workflow, monkeypatch, prior):
    verify(workflow)
    _required_runtime_run(monkeypatch, workflow.state)
    assert _finish(prior) == prior
    assert conftest._MISSING_REQUIRED_PROOF == [_profile_never_verified()]


def test_under_the_runtime_flag_configured_targets_alone_name_every_missing_proof(monkeypatch):
    _required_runtime_run(monkeypatch, _configured_but_unproved())
    assert _finish() == pytest.ExitCode.TESTS_FAILED
    missing = conftest._MISSING_REQUIRED_PROOF
    assert len(missing) == 3 and _NO_SESSION in missing[0]
    assert missing[1:] == [_NEVER_VERIFIED, _profile_never_verified()]


def test_under_the_runtime_flag_a_verified_run_whose_lock_was_lost_is_interrupted(workflow, monkeypatch):
    verify(workflow)
    _required_runtime_run(monkeypatch, workflow.state)
    workflow.server.locks.clear()
    assert _finish() == pytest.ExitCode.INTERRUPTED
    assert conftest._FINAL_EXCLUSIVITY_PROBLEMS


@pytest.mark.parametrize(
    "prior",
    [pytest.ExitCode.TESTS_FAILED, pytest.ExitCode.INTERRUPTED, pytest.ExitCode.USAGE_ERROR,
     pytest.ExitCode.NO_TESTS_COLLECTED],
    ids=["failed", "interrupted", "usage error", "nothing collected"],
)
def test_missing_live_proof_is_reported_but_never_replaces_a_nonzero_outcome(monkeypatch, prior):
    _required_runtime_run(monkeypatch, _configured_but_unproved())
    assert _finish(prior) == prior
    assert len(conftest._MISSING_REQUIRED_PROOF) == 3


@pytest.mark.parametrize("require_live", [False, True], ids=["no flag", "live flag only"])
def test_without_the_runtime_flag_no_live_proof_is_required(monkeypatch, require_live):
    isolate_conftest(monkeypatch, require_live=require_live, state=_configured_but_unproved())
    assert _finish() == pytest.ExitCode.OK and conftest._MISSING_REQUIRED_PROOF == []


def test_finishing_a_required_run_opens_no_connection_to_supply_missing_proof(workflow, monkeypatch):
    def must_not_verify(state=None, connect=None):
        """Fail loudly: the finish hook must read recorded state, never prove anything itself."""
        raise AssertionError("the finish hook tried to verify the runtime identity")

    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    monkeypatch.setattr(safety, "verify_runtime_identity", must_not_verify)
    monkeypatch.setattr(conftest, "read_assigned_runtime_profile", _must_not_read_profile)
    _required_runtime_run(monkeypatch, workflow.state)
    already_run = len(workflow.guard.executed)
    assert _finish() == pytest.ExitCode.TESTS_FAILED
    # The only database work at finish is the existing guard connection re-proving its lock.
    at_finish = workflow.guard.executed[already_run:]
    assert at_finish and all("pid = pg_backend_pid()" in text for text in at_finish)


@pytest.mark.parametrize(
    "keywords, expected",
    [
        pytest.param(_RUNTIME_KEYWORDS, pytest.ExitCode.OK, id="runtime skip"),
        pytest.param({"integration": 1}, pytest.ExitCode.TESTS_FAILED, id="integration skip"),
        pytest.param({}, pytest.ExitCode.OK, id="other skip"),
    ],
)
def test_under_the_live_flag_alone_only_an_owner_integration_skip_fails_the_run(monkeypatch, keywords, expected):
    isolate_conftest(monkeypatch, require_live=True)
    conftest.pytest_runtest_logreport(_report(keywords))
    assert _finish() == expected


def test_without_either_flag_no_skip_fails_the_run(monkeypatch):
    isolate_conftest(monkeypatch)
    for keywords in (_RUNTIME_KEYWORDS, {"integration": 1}, {}):
        conftest.pytest_runtest_logreport(_report(keywords))
    assert _finish() == pytest.ExitCode.OK


class _Item:
    """A collected test as pytest_collection_modifyitems sees it."""

    def __init__(self, *fixturenames: str, markers: tuple[str, ...] = ()) -> None:
        """Remember the fixture names and the markers the test was declared with."""
        self.fixturenames = list(fixturenames)
        self.markers: list[str] = list(markers)

    def add_marker(self, marker) -> None:
        """Record the marker's name."""
        self.markers.append(marker.name)

    def get_closest_marker(self, name: str):
        """The marker's name when the item carries it, else None; conftest only asks whether it is present."""
        return name if name in self.markers else None


def test_collection_marks_every_runtime_fixture_user_as_runtime_identity_and_integration():
    items = [
        _Item("runtime_connection", "request"), _Item("runtime_pool", "monkeypatch"), _Item("db_connection"), _Item(),
    ]
    config = _Options()
    conftest.pytest_collection_modifyitems(config, items)
    marked = ["runtime_identity", "integration"]
    assert [item.markers for item in items] == [marked, marked, [], []]
    assert len(items) == 4 and config.hook.deselected == []


def _profile_items() -> dict[str, _Item]:
    """One unmarked and one pilot_profile test, each with and without a runtime fixture, by name."""
    return {
        "runtime": _Item("runtime_pool"), "unit": _Item(),
        "pilot runtime": _Item("runtime_connection", markers=("pilot_profile",)),
        "pilot unit": _Item(markers=("pilot_profile",)),
    }


@pytest.mark.parametrize(
    "profile, kept, deselected",
    [
        ("full", ["runtime", "unit"], ["pilot runtime", "pilot unit"]),
        ("pilot", ["pilot runtime", "pilot unit"], ["runtime", "unit"]),
    ],
)
def test_collection_keeps_only_the_selected_profile_and_reports_the_rest_as_deselected(profile, kept, deselected):
    by_name = _profile_items()
    items = list(by_name.values())
    config = _Options(runtime_profile=profile)
    conftest.pytest_collection_modifyitems(config, items)
    assert items == [by_name[name] for name in kept]
    assert config.hook.deselected == [[by_name[name] for name in deselected]]
    marked = ["runtime_identity", "integration"]
    assert by_name["runtime"].markers == marked and by_name["pilot runtime"].markers == ["pilot_profile", *marked]
    assert by_name["unit"].markers == [] and by_name["pilot unit"].markers == ["pilot_profile"]


def test_a_pilot_collection_with_no_pilot_tests_selects_nothing_and_deselects_everything():
    items = [_Item("runtime_pool"), _Item()]
    everything = list(items)
    config = _Options(runtime_profile="pilot")
    conftest.pytest_collection_modifyitems(config, items)
    assert items == [] and config.hook.deselected == [everything]


class _IdentityConnection:
    """A raw runtime connection that answers SELECT session_user, current_user and records what happened to it."""

    def __init__(self, session_user: str, current_user: str | None = None) -> None:
        """Script the two users the connection reports."""
        self.users = (session_user, current_user or session_user)
        self.statements: list[str] = []
        self.rollbacks = 0
        self.closed = 0
        self.autocommit = True
        self.info = types.SimpleNamespace(transaction_status=psycopg2.extensions.TRANSACTION_STATUS_IDLE)

    def cursor(self) -> "_IdentityCursor":
        """Return a cursor over this connection."""
        return _IdentityCursor(self)

    def rollback(self) -> None:
        """Count the rollback."""
        self.rollbacks += 1

    def close(self) -> None:
        """Mark the connection closed."""
        self.closed = 1


class _IdentityCursor:
    """Answers only the identity statement the runtime fixtures issue."""

    def __init__(self, connection: _IdentityConnection) -> None:
        """Remember the connection."""
        self._connection = connection

    def execute(self, sql: str, params=None) -> None:
        """Record the statement; anything but the identity read fails the test."""
        assert sql == "SELECT session_user, current_user", sql
        self._connection.statements.append(sql)

    def fetchone(self) -> tuple:
        """Return the scripted session and current user."""
        return self._connection.users


def physical_connections(opened: list, session_user: str = "kerno_test_app"):
    """Return a stand-in for libpq that opens identity connections and records them."""
    def connect(dsn=None, connection_factory=None, cursor_factory=None, **keyword_arguments):
        """Open one fake physical connection."""
        connection = _IdentityConnection(session_user)
        opened.append(connection)
        return connection
    return connect


@pytest.mark.parametrize(
    "users", [("kerno_test",), ("kerno_test_app", "kerno_test"), ("postgres",)],
    ids=["owner session", "owner current user", "superuser"],
)
def test_a_runtime_connection_reporting_another_user_fails_the_test_and_leaves_no_transaction(users):
    connection = _IdentityConnection(*users)
    outcome = _outcome_of(lambda: conftest._require_runtime_session_user(connection))
    assert isinstance(outcome, pytest.fail.Exception), f"expected a failure, got {outcome!r}"
    assert connection.rollbacks == 1


def test_a_runtime_connection_reporting_the_runtime_role_passes_and_leaves_no_transaction():
    connection = _IdentityConnection("kerno_test_app")
    assert _outcome_of(lambda: conftest._require_runtime_session_user(connection)) is None
    assert connection.rollbacks == 1 and connection.statements == ["SELECT session_user, current_user"]


def test_the_runtime_pool_checks_each_new_physical_connection_exactly_once(workflow, monkeypatch):
    verify(workflow)
    opened: list = []
    monkeypatch.setattr(safety, "_STATE", workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", physical_connections(opened))
    pool = conftest.RuntimeIdentityPool(1, 3, dsn=RUNTIME_URL)
    first = pool.getconn()
    second = pool.getconn()
    pool.putconn(second)
    again = pool.getconn()
    pool.closeall()
    assert len(opened) == 2 and first is opened[0] and again is second
    assert [len(connection.statements) for connection in opened] == [1, 1]


def test_the_runtime_pool_fails_the_test_when_a_physical_connection_is_not_the_runtime_role(workflow, monkeypatch):
    verify(workflow)
    monkeypatch.setattr(safety, "_STATE", workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", physical_connections([], session_user="kerno_test"))
    outcome = _outcome_of(lambda: conftest.RuntimeIdentityPool(1, 2, dsn=RUNTIME_URL))
    assert isinstance(outcome, pytest.fail.Exception), f"expected a failure, got {outcome!r}"


def test_the_runtime_pool_cannot_open_a_connection_before_verification(workflow, monkeypatch):
    monkeypatch.setattr(safety, "_STATE", workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    with pytest.raises(safety.DatabaseTargetRejected, match="only after its live verification"):
        conftest.RuntimeIdentityPool(1, 2, dsn=RUNTIME_URL)


class _FixtureRequest:
    """The parts of a fixture request the runtime fixtures use, recording which fixtures they asked for."""

    def __init__(self, config) -> None:
        """Remember the config; nothing requested yet."""
        self.config = config
        self.requested: list[str] = []

    def getfixturevalue(self, name: str) -> None:
        """Record the request; the owner fixture itself is not run here."""
        self.requested.append(name)


def _runtime_fixture_ready(held: types.SimpleNamespace, monkeypatch, opened: list) -> _FixtureRequest:
    """Verify the runtime and its "full" profile, point conftest and the guard at this workflow, and return a request.

    The profile is recorded first, so the fixture must not read it again: a
    second read would reach the identity-only fake connection and fail.
    """
    verify(held)
    isolate_conftest(monkeypatch, require_live=True, require_runtime=True, state=held.state)
    record_verified_profile(held.state)
    monkeypatch.setattr(safety, "_STATE", held.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", physical_connections(opened))
    return _FixtureRequest(_Options(require_live=True, require_runtime=True))


def test_the_runtime_pool_fixture_replaces_both_application_pool_seams_and_closes_its_pool(workflow, monkeypatch):
    from src.api import dependencies

    request = _runtime_fixture_ready(workflow, monkeypatch, [])
    fixture = conftest.runtime_pool.__wrapped__(request, monkeypatch)
    pool = next(fixture)
    assert request.requested == ["db_connection"]
    assert dependencies._pool is pool and dependencies._get_pool() is pool
    with pytest.raises(StopIteration):
        next(fixture)
    assert pool.closed


def test_the_runtime_pool_fixture_fails_when_the_application_pool_was_replaced(workflow, monkeypatch):
    from src.api import dependencies

    request = _runtime_fixture_ready(workflow, monkeypatch, [])
    fixture = conftest.runtime_pool.__wrapped__(request, monkeypatch)
    pool = next(fixture)
    monkeypatch.setattr(dependencies, "_pool", None)
    with pytest.raises(AssertionError, match="application pool was replaced"):
        next(fixture)
    assert pool.closed


def test_the_runtime_connection_fixture_asks_for_the_owner_fixture_first_and_closes_its_connection(workflow, monkeypatch):
    opened: list = []
    request = _runtime_fixture_ready(workflow, monkeypatch, opened)
    fixture = conftest.runtime_connection.__wrapped__(request)
    connection = next(fixture)
    assert request.requested == ["db_connection"] and isinstance(connection, conftest._DbConnection)
    raw = opened[0]
    assert raw.autocommit is False and raw.statements == ["SELECT session_user, current_user"]
    with pytest.raises(StopIteration):
        next(fixture)
    assert raw.closed and raw.rollbacks == 2


@pytest.mark.parametrize("fixture_name", ["runtime_connection", "runtime_pool"])
def test_a_runtime_fixture_stops_the_run_before_its_first_connection_when_the_assigned_profile_differs(
    workflow, monkeypatch, fixture_name
):
    verify(workflow)
    isolate_conftest(monkeypatch, require_live=True, require_runtime=True, state=workflow.state)
    monkeypatch.setattr(safety, "_STATE", workflow.state)
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    monkeypatch.setattr(conftest, "read_assigned_runtime_profile", _AssignedProfile("pilot"))
    request = _FixtureRequest(_Options(require_live=True, require_runtime=True))
    fixture_function = getattr(conftest, fixture_name).__wrapped__
    arguments = (request,) if fixture_name == "runtime_connection" else (request, monkeypatch)
    outcome = _outcome_of(lambda: next(fixture_function(*arguments)))
    assert isinstance(outcome, pytest.exit.Exception), f"expected the run to stop, got {outcome!r}"
    assert outcome.returncode == pytest.ExitCode.USAGE_ERROR and "runtime profile mismatch" in str(outcome)
    assert request.requested == ["db_connection"] and conftest._VERIFIED_RUNTIME_PROFILE == {"value": None}


def test_without_runtime_settings_the_runtime_connection_fixture_skips_only_after_asking_for_the_owner_fixture(
    monkeypatch,
):
    isolate_conftest(monkeypatch, require_live=True, state=safety.ProcessState(target=make_target()))
    monkeypatch.setattr(safety, "_ORIGINAL_CONNECT", _must_not_reach_libpq)
    request = _FixtureRequest(_Options(require_live=True))
    outcome = _outcome_of(lambda: next(conftest.runtime_connection.__wrapped__(request)))
    assert isinstance(outcome, pytest.skip.Exception), f"expected a skip, got {outcome!r}"
    assert str(outcome) == "runtime identity not configured" and request.requested == ["db_connection"]


class _Reporter:
    """Collects what the terminal summary writes."""

    def __init__(self) -> None:
        """No lines yet."""
        self.lines: list[tuple[str, bool]] = []

    def write_line(self, line: str, red: bool = False, **markup) -> None:
        """Remember the line and whether it was red."""
        self.lines.append((line, red))


def _summary_cases() -> list:
    """One process state per runtime-identity line the terminal summary can print."""
    owner, runtime = make_target(), make_runtime_target()
    return [
        pytest.param(
            safety.ProcessState(target=owner),
            "runtime identity: not configured - runtime-identity tests were skipped", False, id="not configured",
        ),
        pytest.param(
            safety.ProcessState(
                target=owner, runtime_target=runtime, runtime_source="the process environment", runtime_verified=True,
            ),
            f"runtime identity: {RUNTIME_APPROVAL} verified (settings from the process environment); "
            "grant profile: not verified", False,
            id="verified, profile not yet read",
        ),
        pytest.param(
            safety.ProcessState(target=owner, runtime_target=runtime, runtime_not_ready="the server refused the login"),
            "FAILED: runtime identity setup failure: the server refused the login", True, id="not ready",
        ),
        pytest.param(
            safety.ProcessState(target=owner, runtime_target=runtime, runtime_source=".env.test.runtime"),
            f"runtime identity: {RUNTIME_APPROVAL} configured, not used in this run", False, id="configured, unused",
        ),
    ]


@pytest.mark.parametrize("state, expected, red", _summary_cases())
def test_the_terminal_summary_states_the_runtime_identity_without_credentials(monkeypatch, state, expected, red):
    isolate_conftest(monkeypatch, state=state)
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, pytest.ExitCode.OK, None)
    assert (expected, red) in reporter.lines
    assert _credential_free("\n".join(entry[0] for entry in reporter.lines))


@pytest.mark.parametrize("profile", _PROFILES)
def test_the_terminal_summary_names_the_verified_grant_profile(monkeypatch, profile):
    state = safety.ProcessState(
        target=make_target(), runtime_target=make_runtime_target(), runtime_source=".env.test.runtime",
        runtime_verified=True,
    )
    isolate_conftest(monkeypatch, state=state, runtime_profile=profile)
    record_verified_profile(state, profile)
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, pytest.ExitCode.OK, None)
    expected = (
        f"runtime identity: {RUNTIME_APPROVAL} verified (settings from .env.test.runtime); grant profile: {profile}"
    )
    assert (expected, False) in reporter.lines


def test_the_summary_names_each_live_proof_a_required_runtime_run_never_completed(monkeypatch):
    _required_runtime_run(monkeypatch, _configured_but_unproved())
    _finish()
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, pytest.ExitCode.TESTS_FAILED, None)
    incomplete = [line for line, red in reporter.lines if red and "required runtime-identity run incomplete" in line]
    assert incomplete == [
        f"FAILED: required runtime-identity run incomplete: {problem}" for problem in conftest._MISSING_REQUIRED_PROOF
    ]
    assert len(incomplete) == 3 and _credential_free("\n".join(incomplete))


def test_the_summary_under_the_runtime_flag_lists_every_skip_and_allows_none(monkeypatch):
    isolate_conftest(monkeypatch, require_live=True, require_runtime=True)
    conftest.pytest_runtest_logreport(_report(_RUNTIME_KEYWORDS, nodeid="runtime::test"))
    conftest.pytest_runtest_logreport(_report({}, nodeid="unit::test"))
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, pytest.ExitCode.TESTS_FAILED, None)
    assert ("FAILED: 2 test(s) skipped in a required runtime-identity run (zero allowed):", True) in reporter.lines
    assert ("  runtime::test", True) in reporter.lines and ("  unit::test", True) in reporter.lines


def test_the_summary_under_the_live_flag_alone_lists_only_owner_integration_skips(monkeypatch):
    isolate_conftest(monkeypatch, require_live=True)
    conftest.pytest_runtest_logreport(_report(_RUNTIME_KEYWORDS, nodeid="runtime::test"))
    conftest.pytest_runtest_logreport(_report({"integration": 1}, nodeid="live::test"))
    reporter = _Reporter()
    conftest.pytest_terminal_summary(reporter, pytest.ExitCode.TESTS_FAILED, None)
    assert ("FAILED: 1 integration test(s) skipped in a required live run:", True) in reporter.lines
    assert ("  live::test", True) in reporter.lines
    assert not any("runtime::test" in entry[0] for entry in reporter.lines)
