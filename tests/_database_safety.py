"""_database_safety.py — the one boundary between a test process and any database.

What:  Decides whether a test or test-migration process may touch a database
       at all, and if so which one. It reads only the dedicated test settings
       (KERNO_TEST_DATABASE_URL and KERNO_TEST_DATABASE_APPROVAL, from the
       process environment or a deliberately loaded .env.test), validates the
       effective connection parameters, disables python-dotenv so the ordinary
       .env can never leak in, replaces psycopg2.connect with a guard that
       refuses every other target, verifies the real connection read-only
       before anything is written, and holds one session-level advisory lock
       so exactly one destructive workflow owns the database at a time.
Why:   Integration fixtures delete rows and disable audit triggers. Until
       TEST-SAFETY-001 they ran against whatever DATABASE_URL happened to be
       set, and src/api/app.py loads .env at import time, so an ordinary pytest
       run always reached the development database. The approved target is a
       disposable database named kerno_test, owned by a restricted kerno_test
       role, provisioned and approved by the owner
       (docs/test_database_runbook.md). Nothing here provisions, drops or
       repairs a database.
How:   tests/conftest.py calls bootstrap_test_process() before any repository
       import; scripts/migrate_test_database.py calls
       prepare_migration_process(). Both then call ensure_exclusive_session()
       before the first write. Offline proof:
       pytest tests/unit/test_database_safety.py -v

This module imports nothing from src/ or config/: it must be safe to run
before the application has been imported, and must not load application
configuration itself.
"""

from __future__ import annotations

import atexit
import dataclasses
import io
import ipaddress
import os
import pathlib
import sys

import dotenv
import psycopg2
import psycopg2.extensions

# ---------------------------------------------------------------------------
# The approved target and its settings
# ---------------------------------------------------------------------------

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]

TEST_DATABASE_URL_VARIABLE = "KERNO_TEST_DATABASE_URL"
TEST_DATABASE_APPROVAL_VARIABLE = "KERNO_TEST_DATABASE_APPROVAL"
_SETTINGS_VARIABLES = (TEST_DATABASE_URL_VARIABLE, TEST_DATABASE_APPROVAL_VARIABLE)

# Where the gitignored settings file lives. Overridable only so the safety
# tests can point a child process at a temporary or missing file; whatever
# file is used obeys exactly the same rules.
TEST_ENV_FILE_VARIABLE = "KERNO_TEST_ENV_FILE"
DEFAULT_TEST_ENV_FILE = REPOSITORY_ROOT / ".env.test"

# The only database and role a test process may ever touch. Fixed here, not
# configurable: a setting that could name another database is the hazard.
TEST_DATABASE_NAME = "kerno_test"
TEST_ROLE_NAME = "kerno_test"

# Written onto the database by the owner's administrator when the target is
# provisioned and approved (COMMENT ON DATABASE). Checked on the live
# connection, so approval is a property of the database, not only of a file.
DISPOSABLE_DATABASE_COMMENT = "kerno:disposable-test-database"

LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

# Connection-string keys a test URL may carry. Anything else — hostaddr,
# service, options, target_session_attrs, passfile, … — can change what a
# connection reaches or how it behaves, so it is refused rather than parsed.
_ALLOWED_CONNECTION_KEYS = frozenset(
    {"host", "port", "dbname", "user", "password", "sslmode", "connect_timeout", "application_name"}
)
_REQUIRED_CONNECTION_KEYS = ("host", "port", "dbname", "user")

# libpq environment variables that can redirect or reshape a connection even
# when host, port, dbname and user are all explicit in the URL. (PGHOST,
# PGPORT, PGDATABASE and PGUSER cannot: explicit URL values take precedence
# over them, and the URL must state all four.)
TARGET_CHANGING_LIBPQ_VARIABLES = (
    "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR",
    "PGOPTIONS", "PGTARGETSESSIONATTRS", "PGLOADBALANCEHOSTS",
)

_HIGHEST_TCP_PORT = 65535

# ---------------------------------------------------------------------------
# One workflow at a time: the session-level, two-integer advisory lock
# ---------------------------------------------------------------------------

# pg_try_advisory_lock(int4, int4) — the two-integer form lives in a separate
# lock space from the bigint keys the audit ledger uses, so it cannot collide.
# The values spell ASCII "KERN" and "TEST".
EXCLUSIVE_LOCK_CLASS_KEY = 0x4B45524E
EXCLUSIVE_LOCK_OBJECT_KEY = 0x54455354
# pg_locks reports a two-integer advisory lock with objsubid = 2.
_TWO_INTEGER_ADVISORY_LOCK_SUBID = 2

_TRY_LOCK_SQL = "SELECT pg_try_advisory_lock(%s, %s)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(%s, %s)"
_LOCK_HELD_BY_ME_SQL = """
SELECT count(*) FROM pg_locks
WHERE locktype = 'advisory' AND classid = %s::oid AND objid = %s::oid
  AND objsubid = %s AND pid = pg_backend_pid() AND granted
"""
_LOCK_HOLDERS_SQL = """
SELECT activity.pid, activity.application_name
FROM pg_locks AS locks JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
WHERE locks.locktype = 'advisory' AND locks.classid = %s::oid AND locks.objid = %s::oid
  AND locks.objsubid = %s AND locks.granted
"""

# What the live connection really is — read-only, no tenant context needed.
_IDENTITY_SQL = """
SELECT current_database(), session_user, current_user,
       host(inet_server_addr()), inet_server_port(),
       roles.rolsuper, roles.rolbypassrls, roles.rolcreatedb,
       roles.rolcreaterole, roles.rolreplication,
       (SELECT count(*) FROM pg_auth_members WHERE member = roles.oid),
       pg_get_userbyid(databases.datdba),
       shobj_description(databases.oid, 'pg_database')
FROM pg_roles AS roles, pg_database AS databases
WHERE roles.rolname = session_user AND databases.datname = current_database()
"""
_IDENTITY_FIELDS = (
    "database", "session_user", "current_user", "server_address", "server_port",
    "superuser", "bypass_rls", "create_database", "create_role", "replication",
    "memberships", "database_owner", "database_comment",
)

_DOTENV_PROBE_VARIABLE = "KERNO_DOTENV_DISABLED_PROBE"
_HARMLESS_JWT_SECRET = "test-secret-for-unit-tests"


# ---------------------------------------------------------------------------
# Errors — every message is written so it cannot carry a credential
# ---------------------------------------------------------------------------


class KernoTestDatabaseError(RuntimeError):
    """Base for every refusal. Messages name keys and identities, never passwords or URLs."""


class DatabaseTargetNotConfigured(KernoTestDatabaseError):
    """No test database is configured, so this process may not open any database connection."""


class DatabaseTargetInvalid(KernoTestDatabaseError):
    """Test settings are present but unusable; the run must fail, never skip or fall back."""


class DatabaseTargetRejected(KernoTestDatabaseError):
    """A connection, or the live database behind it, is not the approved disposable target."""


class DatabaseTargetBusy(KernoTestDatabaseError):
    """Another workflow already holds the exclusive lock on the test database."""


class DatabaseExclusivityLost(KernoTestDatabaseError):
    """This workflow no longer provably holds the exclusive lock; destructive work must stop."""


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DatabaseTarget:
    """The validated test database. The URL (which may hold a password) is never printed."""

    host: str
    port: int
    dbname: str
    user: str
    url: str = dataclasses.field(repr=False)

    @property
    def identity(self) -> str:
        """Return role@host:port/database — the exact string the owner approves."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.user}@{host}:{self.port}/{self.dbname}"

    def __str__(self) -> str:
        """Print as the credential-free identity."""
        return self.identity


@dataclasses.dataclass
class ProcessState:
    """What the bootstrap decided for this process, and the exclusive session once held."""

    target: DatabaseTarget | None = None
    problem: str | None = None
    source: str | None = None
    session: "ExclusiveSession | None" = None


_STATE = ProcessState()
# Captured once, before the guard replaces it. Re-imports keep the original.
_ORIGINAL_CONNECT = getattr(psycopg2.connect, "kerno_original_connect", psycopg2.connect)


# ---------------------------------------------------------------------------
# Settings and URL validation
# ---------------------------------------------------------------------------


def settings_file(environ) -> pathlib.Path:
    """Return the .env.test path this process would read."""
    override = environ.get(TEST_ENV_FILE_VARIABLE)
    return pathlib.Path(override) if override else DEFAULT_TEST_ENV_FILE


def read_settings(environ, env_file: pathlib.Path) -> tuple[str, str, str] | None:
    """Return (url, approval, source) or None when no test database is configured.

    The two settings are read as a pair from ONE source. If either is present
    in the process environment, both must be, and .env.test is not read. Only
    otherwise is .env.test consulted, and it may define those two keys and
    nothing else — a copied DATABASE_URL or secret is refused, not ignored.
    The ordinary .env is never read. Raises DatabaseTargetInvalid.
    """
    if any(name in environ for name in _SETTINGS_VARIABLES):
        return _settings_pair(environ, "the process environment")
    if not env_file.is_file():
        return None
    values = dotenv.dotenv_values(env_file, encoding="utf-8-sig")
    unexpected = sorted(set(values) - set(_SETTINGS_VARIABLES))
    if unexpected:
        raise DatabaseTargetInvalid(
            f"{env_file.name} may define only {', '.join(_SETTINGS_VARIABLES)}; it also defines "
            f"{', '.join(repr(name) for name in unexpected)}."
        )
    return _settings_pair(values, env_file.name)


def _settings_pair(values, source: str) -> tuple[str, str, str]:
    """Return the non-blank (url, approval) pair from one source, or raise DatabaseTargetInvalid."""
    url = (values.get(TEST_DATABASE_URL_VARIABLE) or "").strip()
    approval = (values.get(TEST_DATABASE_APPROVAL_VARIABLE) or "").strip()
    missing = [name for name, value in zip(_SETTINGS_VARIABLES, (url, approval)) if not value]
    if missing:
        raise DatabaseTargetInvalid(f"{' and '.join(missing)} must be set, non-empty, in {source}.")
    return url, approval, source


def effective_parameters(dsn: str) -> dict[str, str]:
    """Return the connection parameters libpq would use for this string, query overrides applied.

    parse_dsn runs libpq's own parser, so ?dbname=… or ?host=… in a URI is
    resolved exactly as a real connection would resolve it. Raises
    DatabaseTargetInvalid for an unparseable string without echoing it.
    """
    try:
        return psycopg2.extensions.parse_dsn(dsn)
    except psycopg2.ProgrammingError:
        raise DatabaseTargetInvalid("the test database URL is not a parseable connection string.") from None


def resolve_target(url: str, approval: str, environ) -> DatabaseTarget:
    """Validate the configured URL and approval and return the target, or raise DatabaseTargetInvalid.

    Requires explicit host, port, dbname and user (no libpq defaults), a
    single loopback host, exactly kerno_test for database and role, no keys
    that can redirect a connection, no redirecting libpq environment
    variables, and an approval string equal to the target's identity.
    """
    parameters = effective_parameters(url)
    _reject_unsupported_keys(parameters)
    missing = [key for key in _REQUIRED_CONNECTION_KEYS if not parameters.get(key)]
    if missing:
        raise DatabaseTargetInvalid(
            f"the test database URL must state {', '.join(missing)} explicitly; no default may choose the target."
        )
    target = DatabaseTarget(
        host=_loopback_host(parameters["host"]), port=_single_port(parameters["port"]),
        dbname=parameters["dbname"], user=parameters["user"], url=url,
    )
    _require_the_test_database(target)
    reject_target_changing_environment(environ)
    if approval != target.identity:
        raise DatabaseTargetInvalid(
            f"{TEST_DATABASE_APPROVAL_VARIABLE} does not match the configured target; the owner's "
            f"approval must be exactly {target.identity!r}."
        )
    return target


def _reject_unsupported_keys(parameters: dict[str, str]) -> None:
    """Refuse any connection key outside the allow-list (keys are named, values are not)."""
    unsupported = sorted(set(parameters) - _ALLOWED_CONNECTION_KEYS)
    if unsupported:
        raise DatabaseTargetInvalid(
            f"the test database URL may not set {', '.join(unsupported)}: those parameters can "
            "redirect or reshape the connection."
        )


def _loopback_host(host: str) -> str:
    """Return the single loopback host name, or raise DatabaseTargetInvalid."""
    if "," in host:
        raise DatabaseTargetInvalid("the test database URL must name exactly one host.")
    if host.lower() not in LOOPBACK_HOST_NAMES:
        raise DatabaseTargetInvalid(
            f"the test database must be on this machine ({', '.join(sorted(LOOPBACK_HOST_NAMES))}); "
            f"the URL names host {host!r}."
        )
    return host.lower()


def _single_port(port: str) -> int:
    """Return the one explicit TCP port as an int, or raise DatabaseTargetInvalid."""
    if not port.isdigit() or not 0 < int(port) <= _HIGHEST_TCP_PORT:
        raise DatabaseTargetInvalid("the test database URL must name exactly one numeric port.")
    return int(port)


def _require_the_test_database(target: DatabaseTarget) -> None:
    """Refuse any database or role other than kerno_test — by name, before any connection."""
    if target.dbname != TEST_DATABASE_NAME:
        raise DatabaseTargetInvalid(
            f"the test database must be exactly {TEST_DATABASE_NAME!r}; the URL selects {target.dbname!r}."
        )
    if target.user != TEST_ROLE_NAME:
        raise DatabaseTargetInvalid(
            f"the test role must be exactly {TEST_ROLE_NAME!r}; the URL logs in as {target.user!r}."
        )


def reject_target_changing_environment(environ) -> None:
    """Refuse to run while a libpq variable that can redirect a connection is set (names only)."""
    present = [name for name in TARGET_CHANGING_LIBPQ_VARIABLES if environ.get(name)]
    if present:
        raise DatabaseTargetInvalid(
            f"unset {', '.join(present)} before running against the test database: "
            "libpq would apply them to the connection."
        )


def redact(text: str, target: DatabaseTarget | None) -> str:
    """Return text with the target's URL and password replaced, for any message printed from a foreign error."""
    if target is None:
        return text
    redacted = text.replace(target.url, "<test-database-url>")
    password = effective_parameters(target.url).get("password")
    if password:
        redacted = redacted.replace(password, "<password>")
    return redacted


# ---------------------------------------------------------------------------
# The live connection: read-only identity check
# ---------------------------------------------------------------------------


def identity_problems(identity: dict, target: DatabaseTarget) -> list[str]:
    """Return every way the live connection differs from the approved target (empty when it matches)."""
    problems = []
    if identity["database"] != TEST_DATABASE_NAME:
        problems.append(f"connected database is {identity['database']!r}, not {TEST_DATABASE_NAME!r}")
    for field in ("session_user", "current_user", "database_owner"):
        if identity[field] != TEST_ROLE_NAME:
            problems.append(f"{field} is {identity[field]!r}, not {TEST_ROLE_NAME!r}")
    if not _is_loopback_address(identity["server_address"]):
        problems.append("the server address is not a TCP loopback address")
    if identity["server_port"] != target.port:
        problems.append(f"the server port is {identity['server_port']}, not the approved {target.port}")
    for flag in ("superuser", "bypass_rls", "create_database", "create_role", "replication"):
        if identity[flag]:
            problems.append(f"the test role has the {flag} privilege")
    if identity["memberships"]:
        problems.append("the test role is a member of other roles")
    if identity["database_comment"] != DISPOSABLE_DATABASE_COMMENT:
        problems.append("the database does not carry the owner's disposable-target approval comment")
    return problems


def _is_loopback_address(address: str | None) -> bool:
    """True only for a real TCP loopback address (a Unix socket reports None and is refused)."""
    if not address:
        return False
    return ipaddress.ip_address(address).is_loopback


def verify_live_connection(connection, target: DatabaseTarget) -> None:
    """Check, in a read-only transaction, that this connection really is the approved target.

    Runs before any write. Leaves no transaction open. Raises
    DatabaseTargetRejected listing every mismatch.
    """
    cursor = connection.cursor()
    try:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(_IDENTITY_SQL)
        row = cursor.fetchone()
    finally:
        connection.rollback()
    if row is None:
        raise DatabaseTargetRejected("could not read the connected role and database from the catalog.")
    problems = identity_problems(dict(zip(_IDENTITY_FIELDS, row)), target)
    if problems:
        raise DatabaseTargetRejected(
            f"the live connection is not the approved {target.identity}: {'; '.join(problems)}."
        )


# ---------------------------------------------------------------------------
# Exclusive session
# ---------------------------------------------------------------------------


class ExclusiveSession:
    """One dedicated connection that verifies the target and then holds the workflow lock.

    The lock is session-level, not transaction-level, so no commit anywhere
    releases it; it lasts until release() or until the connection ends. The
    connection is never pooled or shared.
    """

    def __init__(self, target: DatabaseTarget, purpose: str, connect=None) -> None:
        """Remember the target and workflow name; nothing is opened yet."""
        self.target = target
        self.purpose = purpose
        self._connect = connect or _ORIGINAL_CONNECT
        self._connection = None

    @property
    def active(self) -> bool:
        """True while this session holds an open guard connection."""
        return self._connection is not None

    def acquire(self) -> None:
        """Connect, verify the live target read-only, then take the lock without waiting.

        Raises DatabaseTargetRejected (unreachable or wrong target) or
        DatabaseTargetBusy (another workflow holds the lock). On any failure
        the connection is closed and nothing has been written.
        """
        connection = self._open()
        try:
            verify_live_connection(connection, self.target)
            connection.autocommit = True
            cursor = connection.cursor()
            cursor.execute(_TRY_LOCK_SQL, (EXCLUSIVE_LOCK_CLASS_KEY, EXCLUSIVE_LOCK_OBJECT_KEY))
            if not cursor.fetchone()[0]:
                raise DatabaseTargetBusy(
                    f"another workflow holds the exclusive lock on {self.target.identity} "
                    f"({_describe_holders(cursor)}); run one test workflow at a time."
                )
        except BaseException:
            connection.close()
            raise
        self._connection = connection

    def _open(self):
        """Open the guard connection, turning a driver failure into a credential-free refusal."""
        try:
            return self._connect(self.target.url, application_name=f"kerno-test-guard:{self.purpose}")
        except psycopg2.Error as exc:
            raise DatabaseTargetRejected(
                f"could not connect to the approved test database {self.target.identity}: "
                f"{redact(str(exc).strip(), self.target)}"
            ) from None

    def assert_held(self) -> None:
        """Prove the lock is still held on a live guard connection, or raise DatabaseExclusivityLost."""
        if self._connection is None:
            raise DatabaseExclusivityLost("the exclusive test-database session is not open.")
        try:
            cursor = self._connection.cursor()
            cursor.execute(
                _LOCK_HELD_BY_ME_SQL,
                (EXCLUSIVE_LOCK_CLASS_KEY, EXCLUSIVE_LOCK_OBJECT_KEY, _TWO_INTEGER_ADVISORY_LOCK_SUBID),
            )
            held = cursor.fetchone()[0] == 1
        except psycopg2.Error:
            held = False
        if not held:
            raise DatabaseExclusivityLost(
                f"the exclusive lock on {self.target.identity} is no longer held; stopping before any "
                "further destructive work."
            )

    def release(self) -> None:
        """Unlock and close the guard connection. Safe to call more than once and after a failure."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.cursor().execute(_UNLOCK_SQL, (EXCLUSIVE_LOCK_CLASS_KEY, EXCLUSIVE_LOCK_OBJECT_KEY))
        except psycopg2.Error:
            pass
        finally:
            connection.close()


def _describe_holders(cursor) -> str:
    """Name the backend(s) holding the lock by pid and application name — nothing secret."""
    cursor.execute(
        _LOCK_HOLDERS_SQL,
        (EXCLUSIVE_LOCK_CLASS_KEY, EXCLUSIVE_LOCK_OBJECT_KEY, _TWO_INTEGER_ADVISORY_LOCK_SUBID),
    )
    holders = [f"pid {pid} {name or '(unnamed)'}" for pid, name in cursor.fetchall()]
    return ", ".join(holders) or "holder not visible"


def ensure_exclusive_session(purpose: str, state: ProcessState | None = None, connect=None) -> ExclusiveSession:
    """Return this process's exclusive session, acquiring it on first use, after checking it is still held.

    Raises DatabaseTargetInvalid or DatabaseTargetNotConfigured when there is
    no valid target, and the acquire/assert errors otherwise.
    """
    state = state or _STATE
    if state.problem:
        raise DatabaseTargetInvalid(state.problem)
    if state.target is None:
        raise DatabaseTargetNotConfigured(_not_configured_message())
    if state.session is None:
        session = ExclusiveSession(state.target, purpose, connect=connect)
        session.acquire()
        state.session = session
        atexit.register(session.release)
    state.session.assert_held()
    return state.session


def release_exclusive_session(state: ProcessState | None = None) -> None:
    """Release this process's exclusive session, if any."""
    state = state or _STATE
    session = state.session
    state.session = None
    if session is not None:
        session.release()


def _not_configured_message() -> str:
    """The refusal given whenever a connection is attempted with no test database configured."""
    return (
        f"no test database is configured, so this process may not open database connections. Set "
        f"{TEST_DATABASE_URL_VARIABLE} and {TEST_DATABASE_APPROVAL_VARIABLE} (see docs/test_database_runbook.md)."
    )


# ---------------------------------------------------------------------------
# psycopg2.connect guard — the rule every connection path in the process obeys
# ---------------------------------------------------------------------------


def authorize_connection(dsn, keyword_arguments: dict, state: ProcessState | None = None) -> None:
    """Refuse any connection that is not to the approved target inside the held exclusive session.

    Covers the fixtures, the tests' own psycopg2 sessions, SQLAlchemy engines
    and the application's connection pool, because all of them end in
    psycopg2.connect. Raises before libpq sees the request, so a refused
    target is never contacted.
    """
    state = state or _STATE
    if state.problem:
        raise DatabaseTargetInvalid(state.problem)
    if state.target is None:
        raise DatabaseTargetNotConfigured(_not_configured_message())
    if state.session is None or not state.session.active:
        raise DatabaseTargetRejected(
            "a test-database connection was requested outside the exclusive session; "
            "use the db_connection fixture or the test-migration wrapper."
        )
    reject_target_changing_environment(os.environ)
    try:
        requested_dsn = psycopg2.extensions.make_dsn(dsn or "", **keyword_arguments)
    except psycopg2.ProgrammingError:
        raise DatabaseTargetRejected("a connection was requested with an unparseable connection string.") from None
    requested = effective_parameters(requested_dsn)
    if _endpoint(requested) != _endpoint_of(state.target) or set(requested) - _ALLOWED_CONNECTION_KEYS:
        raise DatabaseTargetRejected(
            f"a connection was requested to something other than the approved {state.target.identity}."
        )


def _endpoint(parameters: dict[str, str]) -> tuple[str, str, str, str]:
    """The four values that decide where a connection goes, normalised for comparison."""
    return (
        (parameters.get("host") or "").lower(), parameters.get("port") or "",
        parameters.get("dbname") or "", parameters.get("user") or "",
    )


def _endpoint_of(target: DatabaseTarget) -> tuple[str, str, str, str]:
    """The target's four deciding values in the same shape as _endpoint."""
    return (target.host, str(target.port), target.dbname, target.user)


def _guarded_connect(dsn=None, connection_factory=None, cursor_factory=None, **keyword_arguments):
    """Replacement for psycopg2.connect in test processes: authorise, then delegate unchanged."""
    authorize_connection(dsn, keyword_arguments)
    return _ORIGINAL_CONNECT(
        dsn, connection_factory=connection_factory, cursor_factory=cursor_factory, **keyword_arguments
    )


_guarded_connect.kerno_original_connect = _ORIGINAL_CONNECT


def install_connection_guard() -> None:
    """Replace psycopg2.connect with the guard (idempotent)."""
    psycopg2.connect = _guarded_connect


# ---------------------------------------------------------------------------
# Process bootstrap
# ---------------------------------------------------------------------------


def disable_dotenv(environ) -> None:
    """Turn off python-dotenv's load_dotenv for this process and its children, and prove it is off.

    src/api/app.py and migrations/env.py call load_dotenv() at import time;
    with PYTHON_DOTENV_DISABLED set they read nothing. An installed
    python-dotenv too old to honour the switch is refused, not trusted.
    Explicit dotenv_values(path) calls are unaffected.
    """
    environ["PYTHON_DOTENV_DISABLED"] = "1"
    loaded = dotenv.load_dotenv(stream=io.StringIO(f"{_DOTENV_PROBE_VARIABLE}=1"))
    leaked = environ.pop(_DOTENV_PROBE_VARIABLE, None) is not None
    if loaded or leaked:
        raise DatabaseTargetInvalid(
            "the installed python-dotenv ignores PYTHON_DOTENV_DISABLED (1.2.2 or later is locked in "
            "uv.lock), so the ordinary .env could leak into the test process."
        )


def application_modules_imported(loaded_modules) -> list[str]:
    """Names of repository application modules (src, config) among the already-loaded modules."""
    return sorted(
        name for name in loaded_modules
        if name in ("src", "config") or name.startswith(("src.", "config."))
    )


def bootstrap_test_process(environ=None, state: ProcessState | None = None, loaded_modules=None) -> ProcessState:
    """Establish the test process's database boundary. Must run before any repository import.

    Disables .env loading, removes any inherited DATABASE_URL (it is never
    test authorisation), installs the connection guard, then reads and
    validates the dedicated settings. A valid target becomes the process's
    DATABASE_URL so existing code paths use it; an invalid one is recorded as
    a problem that fails the run. Never opens a connection itself.
    """
    environ = os.environ if environ is None else environ
    loaded_modules = sys.modules if loaded_modules is None else loaded_modules
    state = state or _STATE
    install_connection_guard()
    environ.pop("DATABASE_URL", None)
    environ.setdefault("KERNO_JWT_SECRET", _HARMLESS_JWT_SECRET)
    try:
        early = application_modules_imported(loaded_modules)
        if early:
            raise DatabaseTargetInvalid(
                f"application modules were imported before the test-database boundary: {', '.join(early)}."
            )
        disable_dotenv(environ)
        _adopt_settings(environ, state)
    except KernoTestDatabaseError as exc:
        state.problem = str(exc)
        state.target = None
    return state


def _adopt_settings(environ, state: ProcessState) -> None:
    """Read, validate and adopt the dedicated settings into the process state and DATABASE_URL."""
    settings = read_settings(environ, settings_file(environ))
    if settings is None:
        return
    url, approval, source = settings
    target = resolve_target(url, approval, environ)
    state.target = target
    state.source = source
    environ["DATABASE_URL"] = target.url


def prepare_migration_process(environ=None, state: ProcessState | None = None, loaded_modules=None) -> DatabaseTarget:
    """Bootstrap a test-migration process and return its target; configuration is mandatory here.

    Raises DatabaseTargetInvalid or DatabaseTargetNotConfigured — the wrapper
    never falls back to .env, DATABASE_URL or libpq defaults.
    """
    state = bootstrap_test_process(environ, state, loaded_modules)
    if state.problem:
        raise DatabaseTargetInvalid(state.problem)
    if state.target is None:
        raise DatabaseTargetNotConfigured(
            f"the test-migration wrapper requires {TEST_DATABASE_URL_VARIABLE} and "
            f"{TEST_DATABASE_APPROVAL_VARIABLE} (see docs/test_database_runbook.md)."
        )
    return state.target


def current_state() -> ProcessState:
    """Return this process's boundary state (read-only use)."""
    return _STATE
