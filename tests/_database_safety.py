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
       The guard covers psycopg2.connect, which is how every connection path
       in this repository opens a connection (fixtures, tests' own sessions,
       SQLAlchemy, psycopg2's and the application's pools). The lower-level
       entry points — psycopg2._connect, constructing
       psycopg2.extensions.connection directly, SQLAlchemy creator=, other
       PostgreSQL drivers — are not wrapped; tests/unit/test_database_safety.py
       forbids them anywhere in src/, tests/, scripts/ and migrations/.
Why:   Integration fixtures delete rows and disable audit triggers. Until
       TEST-SAFETY-001 they ran against whatever DATABASE_URL happened to be
       set, and src/api/app.py loads .env at import time, so an ordinary pytest
       run always reached the development database. The approved target is a
       disposable database named kerno_test, owned by a restricted kerno_test
       role, provisioned and approved by the owner
       (docs/test_database_runbook.md). Nothing here provisions, drops or
       repairs a database.
How:   tests/conftest.py calls bootstrap_test_process(read_runtime=True) before
       any repository import; scripts/migrate_test_database.py calls
       prepare_migration_process(). Both then call ensure_exclusive_session()
       before the first write. Offline proof:
       pytest tests/unit/test_database_safety.py tests/unit/test_database_safety_runtime.py -v

Runtime identity (C2, PILOT-READY-002). A pytest process may additionally hold
the fixed non-owner login kerno_test_app, from KERNO_TEST_RUNTIME_DATABASE_URL /
KERNO_TEST_RUNTIME_DATABASE_APPROVAL (environment or the gitignored
.env.test.runtime, one source). Its settings are checked statically at
bootstrap (same literal endpoint and database as the owner, fixed role,
canonical approval) and live on first use: identity, attributes, memberships,
ownership, CREATE/TEMP rights and proof that the owner's guard session holds
the workflow lock in the same database. Only then are runtime connections
authorised, and only for the pytest workflow. A runtime login that the server
refuses is "not ready" (the runtime tests fail, the run is nonzero); any other
mismatch is a refusal that stops the run. The migration workflow never reads
the runtime settings. KERNO_MIGRATION_DATABASE_URL is refused when inherited
with any other destination and otherwise pinned to the owner target.

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

# The second, non-owner identity (C2). Fixed for the same reason; its settings
# live apart from the owner's so the owner's .env.test is never rewritten.
TEST_RUNTIME_ROLE_NAME = "kerno_test_app"
TEST_RUNTIME_DATABASE_URL_VARIABLE = "KERNO_TEST_RUNTIME_DATABASE_URL"
TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE = "KERNO_TEST_RUNTIME_DATABASE_APPROVAL"
_RUNTIME_SETTINGS_VARIABLES = (TEST_RUNTIME_DATABASE_URL_VARIABLE, TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE)
TEST_RUNTIME_ENV_FILE_VARIABLE = "KERNO_TEST_RUNTIME_ENV_FILE"
DEFAULT_TEST_RUNTIME_ENV_FILE = REPOSITORY_ROOT / ".env.test.runtime"
# The only workflow allowed to use the runtime identity. Migrations and grant
# operations are DDL and always run as the owner.
RUNTIME_WORKFLOW = "pytest"

# migrations/env.py prefers this variable over DATABASE_URL, so an inherited
# value naming another database would bypass the pinned DATABASE_URL.
MIGRATION_DATABASE_URL_VARIABLE = "KERNO_MIGRATION_DATABASE_URL"

# Written onto the database by the owner's administrator when the target is
# provisioned and approved (COMMENT ON DATABASE). Checked on the live
# connection, so approval is a property of the database, not only of a file.
DISPOSABLE_DATABASE_COMMENT = "kerno:disposable-test-database"

# Literal loopback addresses only. The name "localhost" is refused: libpq may
# resolve it to ::1 or 127.0.0.1 per connection, so the verified guard session
# and a later working connection could reach different listeners.
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})

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
  AND locks.database = (SELECT oid FROM pg_database WHERE datname = current_database())
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

# The runtime identity, read on the runtime connection itself. Memberships are
# counted in both directions, and pg_shdepend owner rows cover every object the
# role owns, so "owns nothing" is checked, not inferred from the role name.
_RUNTIME_IDENTITY_SQL = """
SELECT current_database(), session_user, current_user,
       host(inet_server_addr()), inet_server_port(),
       roles.rolsuper, roles.rolbypassrls, roles.rolcreatedb,
       roles.rolcreaterole, roles.rolreplication,
       (SELECT count(*) FROM pg_auth_members WHERE member = roles.oid OR roleid = roles.oid),
       pg_get_userbyid(databases.datdba),
       shobj_description(databases.oid, 'pg_database'),
       (SELECT count(*) FROM pg_shdepend
        WHERE refclassid = 'pg_authid'::regclass AND refobjid = roles.oid AND deptype = 'o'),
       has_database_privilege(current_database(), 'CREATE'),
       has_database_privilege(current_database(), 'TEMP'),
       has_schema_privilege('public', 'CREATE')
FROM pg_roles AS roles, pg_database AS databases
WHERE roles.rolname = session_user AND databases.datname = current_database()
"""
_RUNTIME_IDENTITY_FIELDS = _IDENTITY_FIELDS + (
    "owned_objects", "can_create_in_database", "can_create_temporary", "can_create_in_public",
)

# Run on the runtime connection: is the owner's guard session (by pid) holding
# the workflow lock in the database this runtime session is connected to?
_GUARD_LOCK_VISIBLE_SQL = """
SELECT count(*) FROM pg_locks
WHERE locktype = 'advisory' AND classid = %s::oid AND objid = %s::oid
  AND objsubid = %s AND pid = %s AND granted
  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
"""

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


class RuntimeIdentityNotReady(KernoTestDatabaseError):
    """The runtime settings are valid but the server refused the login: the role is missing,
    the password is wrong, or CONNECT has not been granted yet. Never a reason to fall back to the owner."""


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
    runtime_target: DatabaseTarget | None = None
    runtime_source: str | None = None
    runtime_verified: bool = False
    runtime_not_ready: str | None = None


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
            f"{env_file.name} may define only {', '.join(_SETTINGS_VARIABLES)}; it also contains "
            f"{_describe_unexpected_entries(unexpected)}."
        )
    return _settings_pair(values, env_file.name)


def _describe_unexpected_entries(names: list[str]) -> str:
    """Name unexpected keys only when they are plain variable names; count the rest without echoing them.

    A line without "=" (a pasted URL, say) is parsed as a key whose name is the
    whole line, so printing it could print a password.
    """
    plain = [name for name in names if name.isidentifier()]
    others = len(names) - len(plain)
    parts = [repr(name) for name in plain]
    if others:
        parts.append(f"{others} line(s) that are not NAME=value assignments (not shown)")
    return ", ".join(parts)


def _settings_pair(values, source: str, names: tuple[str, str] = _SETTINGS_VARIABLES) -> tuple[str, str, str]:
    """Return the non-blank (url, approval) pair named by names from one source, or raise DatabaseTargetInvalid."""
    url = (values.get(names[0]) or "").strip()
    approval = (values.get(names[1]) or "").strip()
    missing = [name for name, value in zip(names, (url, approval)) if not value]
    if missing:
        raise DatabaseTargetInvalid(f"{' and '.join(missing)} must be set, non-empty, in {source}.")
    return url, approval, source


def runtime_settings_file(environ) -> pathlib.Path:
    """Return the .env.test.runtime path this process would read."""
    override = environ.get(TEST_RUNTIME_ENV_FILE_VARIABLE)
    return pathlib.Path(override) if override else DEFAULT_TEST_RUNTIME_ENV_FILE


def read_runtime_settings(environ, env_file: pathlib.Path) -> tuple[str, str, str] | None:
    """Return the runtime (url, approval, source), or None when no runtime identity is configured.

    One source only, more strictly than the owner pair: the process
    environment or .env.test.runtime, and having runtime values in both is
    refused rather than resolved by precedence. The file may define the two
    runtime keys and nothing else. Raises DatabaseTargetInvalid.
    """
    in_environment = [name for name in _RUNTIME_SETTINGS_VARIABLES if name in environ]
    if in_environment and env_file.is_file():
        raise DatabaseTargetInvalid(
            f"runtime settings are present both in the process environment and in {env_file.name}; keep one source."
        )
    if in_environment:
        return _settings_pair(environ, "the process environment", _RUNTIME_SETTINGS_VARIABLES)
    if not env_file.is_file():
        return None
    values = dotenv.dotenv_values(env_file, encoding="utf-8-sig")
    unexpected = sorted(set(values) - set(_RUNTIME_SETTINGS_VARIABLES))
    if unexpected:
        raise DatabaseTargetInvalid(
            f"{env_file.name} may define only {', '.join(_RUNTIME_SETTINGS_VARIABLES)}; it also contains "
            f"{_describe_unexpected_entries(unexpected)}."
        )
    return _settings_pair(values, env_file.name, _RUNTIME_SETTINGS_VARIABLES)


def resolve_runtime_target(url: str, approval: str, environ, owner: DatabaseTarget) -> DatabaseTarget:
    """Validate the runtime URL and approval against the owner target and return the runtime target.

    The runtime must name the owner's literal host, port and database and log
    in as exactly kerno_test_app; its approval must equal its identity. Checks
    only the settings themselves — nothing here depends on database state, so
    correct settings always pass. Raises DatabaseTargetInvalid.
    """
    try:
        parameters = effective_parameters(url)
        _reject_unsupported_keys(parameters)
        missing = [key for key in _REQUIRED_CONNECTION_KEYS if not parameters.get(key)]
        if missing:
            raise DatabaseTargetInvalid(f"the URL must state {', '.join(missing)} explicitly.")
        target = DatabaseTarget(
            host=_loopback_host(parameters["host"]), port=_single_port(parameters["port"]),
            dbname=parameters["dbname"], user=parameters["user"], url=url,
        )
    except DatabaseTargetInvalid as exc:
        raise DatabaseTargetInvalid(f"{TEST_RUNTIME_DATABASE_URL_VARIABLE}: {exc}") from None
    _require_runtime_endpoint(target, owner)
    reject_target_changing_environment(environ)
    if approval != target.identity:
        raise DatabaseTargetInvalid(
            f"{TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE} does not match the configured runtime target; the owner's "
            f"approval must be exactly {target.identity!r}."
        )
    return target


def _require_runtime_endpoint(target: DatabaseTarget, owner: DatabaseTarget) -> None:
    """Refuse a runtime URL that is not the owner's exact endpoint and database, or not the fixed runtime role."""
    if (target.host, target.port, target.dbname) != (owner.host, owner.port, owner.dbname):
        raise DatabaseTargetInvalid(
            f"{TEST_RUNTIME_DATABASE_URL_VARIABLE} must name the same literal host, port and database as "
            f"{TEST_DATABASE_URL_VARIABLE}; it names a different endpoint (not shown)."
        )
    if target.user != TEST_RUNTIME_ROLE_NAME:
        raise DatabaseTargetInvalid(
            f"the runtime role must be exactly {TEST_RUNTIME_ROLE_NAME!r}; the runtime URL logs in as a different role."
        )


def pin_migration_database_url(environ, owner: DatabaseTarget | None) -> None:
    """Refuse an inherited KERNO_MIGRATION_DATABASE_URL naming anything but the owner target; then pin it to the owner.

    migrations/env.py prefers this variable over DATABASE_URL, so an ambient
    value pointing at a development, runtime or production database would
    otherwise decide where a guarded migration goes. It is refused, never
    silently overridden, so the operator sees the conflict. A blank value is
    treated as absent, as migrations/env.py treats it. Raises
    DatabaseTargetInvalid.
    """
    inherited = (environ.get(MIGRATION_DATABASE_URL_VARIABLE) or "").strip()
    if inherited:
        if owner is None:
            raise DatabaseTargetInvalid(
                f"unset {MIGRATION_DATABASE_URL_VARIABLE}: it is set while no test database is configured."
            )
        try:
            requested = effective_parameters(inherited)
        except DatabaseTargetInvalid:
            raise DatabaseTargetInvalid(f"unset {MIGRATION_DATABASE_URL_VARIABLE}: it is not parseable.") from None
        if _endpoint(requested) != _endpoint_of(owner) or set(requested) - _ALLOWED_CONNECTION_KEYS:
            raise DatabaseTargetInvalid(
                f"unset {MIGRATION_DATABASE_URL_VARIABLE}: it names a destination other than the approved "
                f"{owner.identity} (not shown)."
            )
    if owner is not None:
        environ[MIGRATION_DATABASE_URL_VARIABLE] = owner.url


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
    """Return the single loopback address, or raise DatabaseTargetInvalid.

    The refused value is not echoed: an unencoded "@" in a password makes
    libpq read the password's tail as the host.
    """
    if "," in host:
        raise DatabaseTargetInvalid("the test database URL must name exactly one host.")
    if host not in LOOPBACK_ADDRESSES:
        raise DatabaseTargetInvalid(
            f"the test database host must be a literal loopback address ({', '.join(sorted(LOOPBACK_ADDRESSES))}); "
            "the URL names another host (not shown)."
        )
    return host


def _single_port(port: str) -> int:
    """Return the one explicit TCP port as an int, or raise DatabaseTargetInvalid.

    The port must be written canonically (5432, not 05432): the connection
    guard compares requested ports as text.
    """
    if not port.isdigit() or port != str(int(port)) or not 0 < int(port) <= _HIGHEST_TCP_PORT:
        raise DatabaseTargetInvalid("the test database URL must name exactly one port, written as a plain number.")
    return int(port)


def _require_the_test_database(target: DatabaseTarget) -> None:
    """Refuse any database or role other than kerno_test — by name, before any connection.

    The refused names are not echoed, for the same reason as hosts.
    """
    if target.dbname != TEST_DATABASE_NAME:
        raise DatabaseTargetInvalid(
            f"the test database must be exactly {TEST_DATABASE_NAME!r}; the URL selects a different database."
        )
    if target.user != TEST_ROLE_NAME:
        raise DatabaseTargetInvalid(
            f"the test role must be exactly {TEST_ROLE_NAME!r}; the URL logs in as a different role."
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


def redact_all(text: str, *targets: DatabaseTarget | None) -> str:
    """Return text with every given target's URL and password replaced (owner and runtime together)."""
    for target in targets:
        text = redact(text, target)
    return text


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


def runtime_identity_problems(identity: dict, runtime: DatabaseTarget) -> list[str]:
    """Return every way the live runtime connection differs from the fixed non-owner identity (empty when it matches)."""
    problems = []
    if identity["database"] != TEST_DATABASE_NAME:
        problems.append(f"connected database is {identity['database']!r}, not {TEST_DATABASE_NAME!r}")
    for field in ("session_user", "current_user"):
        if identity[field] != TEST_RUNTIME_ROLE_NAME:
            problems.append(f"{field} is {identity[field]!r}, not {TEST_RUNTIME_ROLE_NAME!r}")
    if identity["database_owner"] != TEST_ROLE_NAME:
        problems.append(f"the database owner is {identity['database_owner']!r}, not {TEST_ROLE_NAME!r}")
    if not _is_loopback_address(identity["server_address"]):
        problems.append("the server address is not a TCP loopback address")
    if identity["server_port"] != runtime.port:
        problems.append(f"the server port is {identity['server_port']}, not the approved {runtime.port}")
    for flag in ("superuser", "bypass_rls", "create_database", "create_role", "replication"):
        if identity[flag]:
            problems.append(f"the runtime role has the {flag} privilege")
    if identity["memberships"]:
        problems.append("the runtime role has role memberships (as a member or as a granted role)")
    if identity["database_comment"] != DISPOSABLE_DATABASE_COMMENT:
        problems.append("the database does not carry the owner's disposable-target approval comment")
    if identity["owned_objects"]:
        problems.append("the runtime role owns database objects")
    for right in ("can_create_in_database", "can_create_temporary", "can_create_in_public"):
        if identity[right]:
            problems.append(f"the runtime role has {right.removeprefix('can_').replace('_', ' ')} rights")
    return problems


def verify_runtime_identity(state: ProcessState | None = None, connect=None) -> DatabaseTarget:
    """Prove, once per run, that the runtime login is the fixed non-owner role under this workflow's lock.

    Requires the pytest workflow's exclusive session to be held. A login the
    server refuses raises RuntimeIdentityNotReady, remembered so later runtime
    tests fail the same way without retrying; any other mismatch, including an
    unproved lock, raises DatabaseTargetRejected. Only after success are
    runtime connections authorised, and every one still re-proves the lock.
    """
    state = state or _STATE
    runtime = _runtime_target_for_verification(state)
    state.session.assert_held()
    if state.runtime_verified:
        return runtime
    if state.runtime_not_ready:
        raise RuntimeIdentityNotReady(state.runtime_not_ready)
    reject_target_changing_environment(os.environ)
    connection = _open_runtime_check_connection(runtime, state, connect or _ORIGINAL_CONNECT)
    try:
        _check_runtime_connection(connection, runtime, state.session)
    except psycopg2.Error as exc:
        raise DatabaseTargetRejected(
            f"could not verify {runtime.identity}: {redact_all(str(exc).strip(), runtime, state.target)}"
        ) from None
    finally:
        connection.close()
    state.runtime_verified = True
    return runtime


def _runtime_target_for_verification(state: ProcessState) -> DatabaseTarget:
    """Return the configured runtime target, refusing when settings, target or the pytest session are missing."""
    if state.problem:
        raise DatabaseTargetInvalid(state.problem)
    if state.runtime_target is None:
        raise DatabaseTargetNotConfigured(
            f"runtime identity not configured: set {TEST_RUNTIME_DATABASE_URL_VARIABLE} and "
            f"{TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE} (see docs/test_database_runbook.md)."
        )
    if state.session is None or not state.session.active or state.session.purpose != RUNTIME_WORKFLOW:
        raise DatabaseTargetRejected(
            f"the runtime identity may be used only inside the held {RUNTIME_WORKFLOW!r} workflow session."
        )
    return state.runtime_target


def _open_runtime_check_connection(runtime: DatabaseTarget, state: ProcessState, connect):
    """Open the one-off runtime check connection; a refused login is "not ready", recorded and raised."""
    try:
        return connect(runtime.url, application_name="kerno-test-runtime-check")
    except psycopg2.Error as exc:
        state.runtime_not_ready = (
            f"the runtime identity {runtime.identity} is configured but could not log in (role missing, "
            f"password wrong, or CONNECT not granted yet): {redact_all(str(exc).strip(), runtime, state.target)}"
        )
        raise RuntimeIdentityNotReady(state.runtime_not_ready) from None


def _check_runtime_connection(connection, runtime: DatabaseTarget, session: "ExclusiveSession") -> None:
    """Read the runtime identity and look for the guard session's lock, read-only; raise on any mismatch."""
    cursor = connection.cursor()
    try:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(_RUNTIME_IDENTITY_SQL)
        row = cursor.fetchone()
        cursor.execute(
            _GUARD_LOCK_VISIBLE_SQL,
            (EXCLUSIVE_LOCK_CLASS_KEY, EXCLUSIVE_LOCK_OBJECT_KEY, _TWO_INTEGER_ADVISORY_LOCK_SUBID,
             session.backend_pid()),
        )
        lock_visible = cursor.fetchone()[0] == 1
    finally:
        connection.rollback()
    if row is None:
        raise DatabaseTargetRejected("could not read the runtime role and database from the catalog.")
    problems = runtime_identity_problems(dict(zip(_RUNTIME_IDENTITY_FIELDS, row)), runtime)
    if not lock_visible:
        problems.append("the guard session's workflow lock is not visible from the runtime session in this database")
    if problems:
        raise DatabaseTargetRejected(
            f"the live runtime connection is not the approved {runtime.identity}: {'; '.join(problems)}."
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
        self._lost = False

    @property
    def active(self) -> bool:
        """True while this session holds its guard connection and has not seen the lock lost."""
        return self._connection is not None and not self._lost

    def acquire(self) -> None:
        """Connect, verify the live target read-only, then take the lock without waiting.

        Raises DatabaseTargetRejected (unreachable or wrong target) or
        DatabaseTargetBusy (another workflow holds the lock). On any failure
        the connection is closed and nothing has been written.
        """
        connection = self._open()
        try:
            self._verify_and_lock(connection)
        except psycopg2.Error as exc:
            connection.close()
            raise DatabaseTargetRejected(
                f"could not verify or lock {self.target.identity}: {redact(str(exc).strip(), self.target)}"
            ) from None
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        self._lost = False

    def _verify_and_lock(self, connection) -> None:
        """Verify the live target read-only, then try the lock once; raise DatabaseTargetBusy if taken."""
        verify_live_connection(connection, self.target)
        connection.autocommit = True
        cursor = connection.cursor()
        cursor.execute(_TRY_LOCK_SQL, (EXCLUSIVE_LOCK_CLASS_KEY, EXCLUSIVE_LOCK_OBJECT_KEY))
        if not cursor.fetchone()[0]:
            raise DatabaseTargetBusy(
                f"another workflow holds the exclusive lock on {self.target.identity} "
                f"({_describe_holders(cursor)}); run one test workflow at a time."
            )

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
            self._lost = True
            raise DatabaseExclusivityLost(
                f"the exclusive lock on {self.target.identity} is no longer held; stopping before any "
                "further destructive work."
            )

    def backend_pid(self) -> int:
        """Return the server pid of the guard connection, which the runtime same-lock proof looks for."""
        if self._connection is None:
            raise DatabaseExclusivityLost("the exclusive test-database session is not open.")
        return self._connection.get_backend_pid()

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
    try:
        cursor.execute(
            _LOCK_HOLDERS_SQL,
            (EXCLUSIVE_LOCK_CLASS_KEY, EXCLUSIVE_LOCK_OBJECT_KEY, _TWO_INTEGER_ADVISORY_LOCK_SUBID),
        )
        holders = [f"pid {pid} {name or '(unnamed)'}" for pid, name in cursor.fetchall()]
    except psycopg2.Error:
        holders = []
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
    """Refuse any connection that is not to the approved target while the exclusive lock is provably held.

    Covers the fixtures, the tests' own psycopg2 sessions, SQLAlchemy engines
    and the application's connection pool, because all of them end in
    psycopg2.connect. Each request's effective parameters are compared with
    the approved target, and the guard connection re-proves the lock (one
    pg_locks query), so nothing new is opened after exclusivity is lost. The
    catalog identity check is not repeated here; it ran once, on the guard
    connection, when the session was acquired. Raises before libpq sees the
    request, so a refused target is never contacted.
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
    state.session.assert_held()
    reject_target_changing_environment(os.environ)
    try:
        requested_dsn = psycopg2.extensions.make_dsn(dsn or "", **keyword_arguments)
    except psycopg2.ProgrammingError:
        raise DatabaseTargetRejected("a connection was requested with an unparseable connection string.") from None
    _authorize_endpoint(effective_parameters(requested_dsn), state)


def _authorize_endpoint(requested: dict[str, str], state: ProcessState) -> None:
    """Allow the owner endpoint, or the runtime endpoint once verified in the pytest workflow; refuse anything else."""
    if not set(requested) - _ALLOWED_CONNECTION_KEYS:
        endpoint = _endpoint(requested)
        if endpoint == _endpoint_of(state.target):
            return
        runtime = state.runtime_target
        if runtime is not None and endpoint == _endpoint_of(runtime):
            if state.runtime_verified and state.session.purpose == RUNTIME_WORKFLOW:
                return
            raise DatabaseTargetRejected(
                f"a runtime-identity connection to {runtime.identity} is allowed only after its live "
                f"verification, and only in the {RUNTIME_WORKFLOW!r} workflow."
            )
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


def bootstrap_test_process(
    environ=None, state: ProcessState | None = None, loaded_modules=None, read_runtime: bool = False,
) -> ProcessState:
    """Establish the test process's database boundary. Must run before any repository import.

    Disables .env loading, removes any inherited DATABASE_URL (it is never
    test authorisation), installs the connection guard, then reads and
    validates the dedicated settings. A valid target becomes the process's
    DATABASE_URL and KERNO_MIGRATION_DATABASE_URL so existing code paths use
    it; an invalid one is recorded as a problem that fails the run. Only the
    pytest bootstrap passes read_runtime=True, which also validates the
    runtime pair statically. Never opens a connection itself.
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
        pin_migration_database_url(environ, state.target)
        if read_runtime:
            _adopt_runtime_settings(environ, state)
    except KernoTestDatabaseError as exc:
        state.problem = str(exc)
        state.target = None
        state.runtime_target = None
        # A refused bootstrap leaves no pinned target behind for any code path to find.
        environ.pop("DATABASE_URL", None)
        environ.pop(MIGRATION_DATABASE_URL_VARIABLE, None)
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


def _adopt_runtime_settings(environ, state: ProcessState) -> None:
    """Read and statically validate the runtime pair; it never becomes DATABASE_URL or any variable the app reads."""
    settings = read_runtime_settings(environ, runtime_settings_file(environ))
    if settings is None:
        return
    if state.target is None:
        raise DatabaseTargetInvalid(
            f"runtime settings are present but {TEST_DATABASE_URL_VARIABLE} and "
            f"{TEST_DATABASE_APPROVAL_VARIABLE} are not; the runtime identity needs the owner target."
        )
    url, approval, source = settings
    state.runtime_target = resolve_runtime_target(url, approval, environ, state.target)
    state.runtime_source = source


def prepare_migration_process(environ=None, state: ProcessState | None = None, loaded_modules=None) -> DatabaseTarget:
    """Bootstrap a test-migration process and return its target; configuration is mandatory here.

    Raises DatabaseTargetInvalid or DatabaseTargetNotConfigured — the wrapper
    never falls back to .env, DATABASE_URL or libpq defaults, and never reads
    the runtime settings, so it cannot connect as the runtime identity.
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
