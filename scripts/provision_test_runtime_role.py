"""provision_test_runtime_role.py — one-time, administrator-run creation of the non-owner runtime login kerno_test_app.

What:  Does exactly what docs/pilot_readiness_kickoff.md §2.7 step 3
       describes: writes the gitignored .env.test.runtime holding the runtime
       pair, then creates the login role kerno_test_app (NOSUPERUSER
       NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT, no
       memberships, owning nothing) and verifies it from the catalog as the
       administrator. It grants nothing: CONNECT and the table privileges come
       later, from the owner's guarded
       `python scripts/migrate_test_database.py grant-runtime` (step 5).
Why:   C2 (PILOT-READY-002) runs the API as a non-owner login so that
       row-level security binds it. Creating a role needs SUPERUSER or
       CREATEROLE, which the owner kerno_test must never hold, so an
       administrator creates it. The administrator password must stay
       private, so the owner runs this script in their own window and types
       it once at a hidden prompt. Claude Code never sees it.
How:   In an ordinary interactive PowerShell window:
           cd J:\\Kerno
           python scripts\\provision_test_runtime_role.py
       Optional: --admin-role NAME (default postgres).
       Exit codes: 0 created and verified; 1 stopped after starting (the
       output says exactly what exists and what the owner decides); 2 refused
       before changing anything.
       Offline tests: pytest tests/unit/test_provision_test_runtime_role.py -v

Safety, by construction
-----------------------
- **Refuses rather than reuses.** It stops, changing nothing, if a libpq
  variable that could redirect the connection or any runtime setting is in
  the environment, if .env.test.runtime already exists or would not be
  gitignored, if the owner role kerno_test, its database, the database's
  owner or its disposable marker are not as provision_test_database.py left
  them, if PUBLIC holds CONNECT on kerno_test, or if kerno_test_app already
  exists. It never reuses, alters or drops a role or a database.
- **The administrator must be a superuser.** On PostgreSQL 16 and later, a
  non-superuser who creates a role through CREATEROLE is automatically
  granted membership in it WITH ADMIN OPTION. That pg_auth_members row would
  break "zero memberships in either direction", which the test guard
  requires of the runtime identity, so such an administrator is refused
  before anything changes. A superuser creator gets no such grant.
- **The settings file first, then the role.** The file is created with
  exclusive create, so an existing file is never overwritten, and before
  CREATE ROLE, so a role this script created always has its password on
  disk. Every result is checked before the next step. On a failure it
  reports exactly what exists and the owner's decision (§2.7 step 4), and
  stops. It never deletes, retries or recovers anything.
- **One connection, and it grants nothing.** Its only connection is the
  administrator's, to the postgres maintenance database. It never connects
  as kerno_test_app (CONNECT is not granted yet, because PUBLIC CONNECT on
  kerno_test is revoked), never connects to kerno_test or kerno_dev, and
  never edits pg_hba.conf or restarts anything.
- **Separate from the test guard.** It is its own process. It reads
  constants from tests/_database_safety.py and reuses the reviewed helpers of
  scripts/provision_test_database.py, but never installs, changes or
  disables the test connection guard, and never reads .env or .env.test.
- **Secrets stay out of view.**
  - The administrator password is typed once, is never echoed, printed or
    stored, and is refused if empty, so saved passwords are never used
    silently.
  - The runtime password comes from secrets.token_urlsafe(32) in this
    process. The server receives only its SCRAM-SHA-256 verifier. The
    plaintext is written only to .env.test.runtime and never printed; it and
    the verifier are removed from every error message.
"""

from __future__ import annotations

import dataclasses
import getpass
import importlib.util
import os
import pathlib
import secrets
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402

from tests._database_safety import (  # noqa: E402 — constants only; the guard is never installed here
    DEFAULT_TEST_RUNTIME_ENV_FILE,
    DISPOSABLE_DATABASE_COMMENT,
    LOOPBACK_ADDRESSES,
    TARGET_CHANGING_LIBPQ_VARIABLES,
    TEST_DATABASE_NAME,
    TEST_ROLE_NAME,
    TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE,
    TEST_RUNTIME_DATABASE_URL_VARIABLE,
    TEST_RUNTIME_ENV_FILE_VARIABLE,
    TEST_RUNTIME_ROLE_NAME,
)


def _load_owner_provisioning():
    """Import scripts/provision_test_database.py by path: scripts/ is not a package, so import by name is ambiguous."""
    path = REPOSITORY_ROOT / "scripts" / "provision_test_database.py"
    spec = importlib.util.spec_from_file_location("provision_test_database", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The reviewed owner-database script. Its SCRAM verifier, redaction, git
# check, checked statement, catalog read, connector, endpoint and exit codes
# are reused rather than copied, so both scripts reach the same server and
# fail, report and redact the same way.
owner_provisioning = _load_owner_provisioning()
ProvisioningStopped = owner_provisioning.ProvisioningStopped
say = owner_provisioning.say

SERVER_HOST = owner_provisioning.SERVER_HOST
SERVER_PORT = owner_provisioning.SERVER_PORT
ADMIN_DATABASE = owner_provisioning.ADMIN_DATABASE
DEFAULT_ADMIN_ROLE = owner_provisioning.DEFAULT_ADMIN_ROLE
DEVELOPMENT_DATABASE = owner_provisioning.DEVELOPMENT_DATABASE
SCRAM_SALT_BYTES = owner_provisioning.SCRAM_SALT_BYTES
EXIT_DONE = owner_provisioning.EXIT_DONE
EXIT_STOPPED = owner_provisioning.EXIT_STOPPED
EXIT_REFUSED = owner_provisioning.EXIT_REFUSED

# secrets.token_urlsafe(32), as kickoff §2.7 step 3 specifies.
RUNTIME_PASSWORD_BYTES = 32
CREATE_ROLE_STATUS = "CREATE ROLE"

# "--admin-role NAME" is an option and its value.
_OPTION_WITH_VALUE = 2

# Any of these in the shell would make the test process read runtime settings
# from somewhere other than the file this writes (the guard allows one source).
_RUNTIME_ENVIRONMENT_VARIABLES = (
    TEST_RUNTIME_DATABASE_URL_VARIABLE, TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE, TEST_RUNTIME_ENV_FILE_VARIABLE,
)

CREATE_ROLE_TEMPLATE = sql.SQL(
    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD {}"
)

SERVER_SQL = (
    "SELECT host(inet_server_addr()), inet_server_port(), rolsuper, split_part(version(), ' on ', 1) "
    "FROM pg_roles WHERE rolname = current_user"
)
OWNER_ROLE_SQL = "SELECT rolcanlogin FROM pg_roles WHERE rolname = %s"
# pg_database, its comment and its ACL are shared catalogs, so kerno_test is
# checked from the postgres database without connecting to it. 'public' names
# the PUBLIC pseudo-role.
OWNER_DATABASE_SQL = (
    "SELECT pg_get_userbyid(datdba), shobj_description(oid, 'pg_database'), "
    "has_database_privilege('public', oid, 'CONNECT') FROM pg_database WHERE datname = %s"
)
EXISTING_RUNTIME_ROLE_SQL = (
    "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit "
    "FROM pg_roles WHERE rolname = %s"
)
# Memberships are counted in both directions and pg_shdepend (a shared catalog)
# holds owner rows for objects in every database, as the test guard counts
# them. pg_authid, readable only by a superuser, shows the server stored the
# verifier as sent; a malformed one would have been hashed as a plaintext
# password, and nothing else proves it before the first runtime login.
VERIFICATION_SQL = """
SELECT r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole, r.rolreplication, r.rolbypassrls,
       r.rolinherit,
       (SELECT count(*) FROM pg_auth_members AS m WHERE m.member = r.oid OR m.roleid = r.oid),
       (SELECT count(*) FROM pg_shdepend AS s
        WHERE s.refclassid = 'pg_authid'::regclass AND s.refobjid = r.oid AND s.deptype = 'o'),
       has_database_privilege(r.oid, %(database)s, 'CONNECT'),
       r.rolpassword = %(verifier)s
FROM pg_authid AS r WHERE r.rolname = %(role)s
"""
# (field, expected value, what it means), in VERIFICATION_SQL column order:
# the role exactly as CREATE ROLE made it, and nothing granted yet.
EXPECTED_ROLE_STATE = (
    ("can_login", True, "LOGIN"),
    ("superuser", False, "NOSUPERUSER"),
    ("create_database", False, "NOCREATEDB"),
    ("create_role", False, "NOCREATEROLE"),
    ("replication", False, "NOREPLICATION"),
    ("bypass_rls", False, "NOBYPASSRLS"),
    ("inherit", False, "NOINHERIT"),
    ("memberships", 0, "no role memberships, as a member or as a granted role"),
    ("owned_objects", 0, "owns no object (no pg_shdepend owner rows)"),
    ("can_connect", False, f"no CONNECT on {TEST_DATABASE_NAME} yet (grant-runtime grants it)"),
    ("stored_verifier_matches", True, "the server stored exactly the SCRAM verifier it was sent"),
)
VERIFICATION_FIELDS = tuple(field for field, _, _ in EXPECTED_ROLE_STATE)


@dataclasses.dataclass(frozen=True)
class RuntimeCredential:
    """The generated runtime password and the SCRAM-SHA-256 verifier the server receives; neither is ever shown."""

    password: str = dataclasses.field(repr=False)
    verifier: str = dataclasses.field(repr=False)

    @classmethod
    def generate(cls) -> RuntimeCredential:
        """Generate a token_urlsafe(32) password and its verifier with a fresh random salt."""
        password = secrets.token_urlsafe(RUNTIME_PASSWORD_BYTES)
        salt = secrets.token_bytes(SCRAM_SALT_BYTES)
        return cls(password=password, verifier=owner_provisioning.scram_verifier(password, salt))


@dataclasses.dataclass
class Progress:
    """What this run has created so far, read on a stop to tell the owner exactly what exists."""

    created: list[str] = dataclasses.field(default_factory=list)
    file_written: bool = False
    role_created: bool = False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def settings_url(password: str) -> str:
    """The runtime URL written to .env.test.runtime. token_urlsafe passwords need no URL encoding."""
    return f"postgresql://{TEST_RUNTIME_ROLE_NAME}:{password}@{SERVER_HOST}:{SERVER_PORT}/{TEST_DATABASE_NAME}"


def approved_identity() -> str:
    """The exact runtime identity the test guard requires as the runtime approval."""
    return f"{TEST_RUNTIME_ROLE_NAME}@{SERVER_HOST}:{SERVER_PORT}/{TEST_DATABASE_NAME}"


# ---------------------------------------------------------------------------
# Checks that change nothing
# ---------------------------------------------------------------------------


def runtime_file_gitignored(path: pathlib.Path) -> bool:
    """True when git ignores the path; raises ProvisioningStopped naming this file if git cannot be asked."""
    try:
        return owner_provisioning.gitignored(path)
    except ProvisioningStopped:
        raise ProvisioningStopped(f"git is not available to confirm {path.name} is ignored.") from None


def preflight(environ, env_file: pathlib.Path, is_ignored=runtime_file_gitignored) -> None:
    """Refuse before any password or connection: redirecting libpq variables, runtime settings, an unsafe file path."""
    redirecting = [name for name in TARGET_CHANGING_LIBPQ_VARIABLES if environ.get(name)]
    if redirecting:
        raise ProvisioningStopped(f"unset {', '.join(redirecting)} first: libpq would apply them to the connection.")
    shell_settings = [name for name in _RUNTIME_ENVIRONMENT_VARIABLES if name in environ]
    if shell_settings:
        raise ProvisioningStopped(
            f"unset {', '.join(shell_settings)} first: while set, the test process would not use the "
            f"{env_file.name} this writes."
        )
    if os.path.lexists(env_file):
        raise ProvisioningStopped(f"{env_file.name} already exists; it is not overwritten. Nothing was changed.")
    if not is_ignored(env_file):
        raise ProvisioningStopped(f"{env_file.name} is not ignored by git; refusing to write a password there.")


def check_server(admin) -> None:
    """Confirm the administrator connection is the loopback server on the expected port, as a superuser."""
    address, port, superuser, version = owner_provisioning._fetch_one(admin, SERVER_SQL)
    if address not in LOOPBACK_ADDRESSES or port != SERVER_PORT:
        raise ProvisioningStopped(f"the administrator connection is not the loopback server on port {SERVER_PORT}.")
    if not superuser:
        raise ProvisioningStopped(
            "the administrator role is not a superuser. On PostgreSQL 16 and later a CREATEROLE creator is granted "
            f"membership in the role it creates, so {TEST_RUNTIME_ROLE_NAME} would not have zero memberships."
        )
    say(f"  ok  server {address}:{port}, {version}; administrator is a superuser")


def owner_target_problems(owner_role, database) -> list[str]:
    """Every way the owner role and the disposable database differ from what provision_test_database.py created."""
    problems = []
    if owner_role is None:
        problems.append(f"role {TEST_ROLE_NAME} does not exist")
    if database is None:
        problems.append(f"database {TEST_DATABASE_NAME} does not exist")
        return problems
    owner, comment, public_can_connect = database
    if owner != TEST_ROLE_NAME:
        problems.append(f"database {TEST_DATABASE_NAME} is owned by {owner!r}, not {TEST_ROLE_NAME!r}")
    if comment != DISPOSABLE_DATABASE_COMMENT:
        problems.append(f"database {TEST_DATABASE_NAME} does not carry the disposable-database comment")
    if public_can_connect:
        problems.append(f"PUBLIC holds CONNECT on {TEST_DATABASE_NAME}, so a new role would gain it at once")
    return problems


def check_owner_target(admin) -> None:
    """Refuse unless the owner role, its database, owner and marker are in place and PUBLIC cannot connect."""
    problems = owner_target_problems(
        owner_provisioning._fetch_one(admin, OWNER_ROLE_SQL, [TEST_ROLE_NAME], allow_none=True),
        owner_provisioning._fetch_one(admin, OWNER_DATABASE_SQL, [TEST_DATABASE_NAME], allow_none=True),
    )
    if problems:
        raise ProvisioningStopped("; ".join(problems) + ". Nothing was changed; this needs the owner's review.")
    say(f"  ok  role {TEST_ROLE_NAME} and database {TEST_DATABASE_NAME} exist (owner, marker, no PUBLIC CONNECT)")


def refuse_existing_runtime_role(admin) -> None:
    """Stop, reporting its attributes, if kerno_test_app already exists; it is never reused, altered or dropped here."""
    role = owner_provisioning._fetch_one(admin, EXISTING_RUNTIME_ROLE_SQL, [TEST_RUNTIME_ROLE_NAME], allow_none=True)
    if role is not None:
        raise ProvisioningStopped(
            f"role {TEST_RUNTIME_ROLE_NAME} already exists (login, super, createdb, createrole, replication, "
            f"bypassrls, inherit = {tuple(role)}). Nothing was changed; it is never reused, altered or dropped "
            "here. This needs the owner's review."
        )
    say(f"  ok  no {TEST_RUNTIME_ROLE_NAME} role exists yet")


def confirm_with_owner(ask, env_file: pathlib.Path) -> bool:
    """Show exactly what will be created and require the word yes."""
    say(f"\nAbout to create on {SERVER_HOST}:{SERVER_PORT}:")
    say(f"  - {env_file.name} (gitignored), written first: the runtime pair; the generated password only there")
    say(f"  - login role {TEST_RUNTIME_ROLE_NAME}: "
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT")
    say("Nothing is granted: CONNECT and table privileges come later, from the owner's grant-runtime.")
    say(f"No login as {TEST_RUNTIME_ROLE_NAME} is attempted. {TEST_DATABASE_NAME}, {DEVELOPMENT_DATABASE}, "
        ".env and .env.test are not touched.")
    return ask("Type yes to proceed: ").strip().lower() == "yes"


# ---------------------------------------------------------------------------
# The changes — each result checked before the next
# ---------------------------------------------------------------------------


def write_runtime_settings(env_file: pathlib.Path, password: str, progress: Progress) -> None:
    """Create .env.test.runtime holding exactly the runtime pair; exclusive create never overwrites a file."""
    content = (
        f"{TEST_RUNTIME_DATABASE_URL_VARIABLE}={settings_url(password)}\n"
        f"{TEST_RUNTIME_DATABASE_APPROVAL_VARIABLE}={approved_identity()}\n"
    )
    with open(env_file, "x", encoding="utf-8", newline="\n") as handle:
        # Recorded on creation, before writing: a failed write still leaves a file behind.
        progress.file_written = True
        progress.created.append(f"{env_file.name} (the runtime pair; the password only there)")
        handle.write(content)
    say(f"  ok  wrote {env_file.name} (gitignored)")


def create_runtime_role(admin, verifier: str, progress: Progress) -> None:
    """Execute the one approved CREATE ROLE, sending only the SCRAM verifier, and require its command status."""
    statement = CREATE_ROLE_TEMPLATE.format(sql.Identifier(TEST_RUNTIME_ROLE_NAME), sql.Literal(verifier))
    owner_provisioning.execute_checked(
        admin, statement, CREATE_ROLE_STATUS, f"role {TEST_RUNTIME_ROLE_NAME} (restricted login, NOINHERIT)",
        progress.created,
    )
    progress.role_created = True


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verification_problems(role_state: dict) -> list[str]:
    """Every way the new role differs from exactly what CREATE ROLE made (empty when it matches)."""
    problems = []
    for field, expected, meaning in EXPECTED_ROLE_STATE:
        if role_state[field] != expected:
            problems.append(f"{meaning} (found {role_state[field]!r})")
    return problems


def verify_runtime_role(admin, verifier: str) -> None:
    """Read the new role from the catalog as the administrator and stop unless it is exactly as created."""
    row = owner_provisioning._fetch_one(
        admin, VERIFICATION_SQL,
        {"database": TEST_DATABASE_NAME, "verifier": verifier, "role": TEST_RUNTIME_ROLE_NAME},
    )
    problems = verification_problems(dict(zip(VERIFICATION_FIELDS, row)))
    if problems:
        raise ProvisioningStopped(f"verification of {TEST_RUNTIME_ROLE_NAME} failed: " + "; ".join(problems) + ".")
    for _, _, meaning in EXPECTED_ROLE_STATE:
        say(f"  ok  verified: {meaning}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def provision(admin, ask, env_file: pathlib.Path, credential: RuntimeCredential, progress: Progress) -> bool:
    """Check, ask, write the settings file, create the role, verify it. Returns False if the owner declined."""
    # Autocommit: CREATE ROLE is committed when it returns, so verification
    # reads committed state and closing the connection cannot roll it back.
    admin.autocommit = True
    check_server(admin)
    check_owner_target(admin)
    refuse_existing_runtime_role(admin)
    if not confirm_with_owner(ask, env_file):
        return False
    write_runtime_settings(env_file, credential.password, progress)
    create_runtime_role(admin, credential.verifier, progress)
    verify_runtime_role(admin, credential.verifier)
    return True


def owner_decision(progress: Progress, env_file: pathlib.Path) -> list[str]:
    """The kickoff §2.7 step 4 state and owner decision that match what exists."""
    role = TEST_RUNTIME_ROLE_NAME
    if progress.role_created:
        return [
            f"State: {env_file.name} written; role {role} created; its verification failed or did not finish.",
            f"Owner decides: ALTER ROLE {role} NOLOGIN, or an administrator DROP ROLE {role}. "
            f"{env_file.name} holds its password.",
        ]
    if progress.file_written:
        return [
            f"State: {env_file.name} written; role {role} not created by this run.",
            f"Owner decides: confirm the role is absent (SELECT 1 FROM pg_roles WHERE rolname = '{role}'), then "
            f"delete {env_file.name}. If the role does exist, treat it as role created, verification failed.",
        ]
    return [f"State: nothing was created; {env_file.name} was not written and no role was created."]


def report_stop(exc: Exception, progress: Progress, env_file: pathlib.Path, secrets_to_hide: tuple[str, ...]) -> None:
    """Say why the run stopped, exactly what exists and what the owner decides; nothing is deleted or retried."""
    message = owner_provisioning.redact(str(exc).strip(), *secrets_to_hide)
    say(f"\nSTOPPED: {type(exc).__name__}: {message}")
    say("Created before stopping: " + ("; ".join(progress.created) if progress.created else "nothing"))
    for line in owner_decision(progress, env_file):
        say(line)
    say("Nothing was deleted, altered or retried. Report this output; do not re-run over it.")


def report_done(env_file: pathlib.Path) -> None:
    """Say what now exists and the owner's next guarded step."""
    say(f"\nDONE: role {TEST_RUNTIME_ROLE_NAME} created and verified; {env_file.name} written for "
        f"{approved_identity()}.")
    say(f"No login was attempted: {TEST_RUNTIME_ROLE_NAME} has no CONNECT until the owner grants it.")
    say("Next (owner, guarded): python scripts/migrate_test_database.py grant-runtime")


def run(argv, environ=None, connect=None, read_secret=None, ask=None, env_file=None,
        is_ignored=runtime_file_gitignored) -> int:
    """Drive the whole provisioning and return the exit code; every dependency is injectable for tests."""
    environ = os.environ if environ is None else environ
    env_file = env_file or DEFAULT_TEST_RUNTIME_ENV_FILE
    admin_role = DEFAULT_ADMIN_ROLE
    if len(argv) == _OPTION_WITH_VALUE and argv[0] == "--admin-role" and argv[1]:
        admin_role = argv[1]
    elif argv:
        say("usage: python scripts/provision_test_runtime_role.py [--admin-role NAME]")
        return EXIT_REFUSED
    try:
        preflight(environ, env_file, is_ignored)
    except ProvisioningStopped as exc:
        say(f"refused: {exc}")
        return EXIT_REFUSED
    admin_password = (read_secret or getpass.getpass)(f"Password for PostgreSQL role {admin_role} (not shown): ")
    if not admin_password:
        say("refused: no administrator password given; nothing was done.")
        return EXIT_REFUSED
    return _run_with_password(admin_role, admin_password, connect or psycopg2.connect, ask or input, env_file)


def _run_with_password(admin_role, admin_password, connect, ask, env_file) -> int:
    """Provision and report, hiding both passwords and the verifier from every message."""
    progress = Progress()
    credential = RuntimeCredential.generate()
    connect_admin = owner_provisioning.admin_connector(connect, admin_role, admin_password)
    try:
        admin = connect_admin(ADMIN_DATABASE)
        try:
            proceeded = provision(admin, ask, env_file, credential, progress)
        finally:
            admin.close()
            say("  ok  administrator connection closed")
    except (ProvisioningStopped, psycopg2.Error, OSError) as exc:
        report_stop(exc, progress, env_file, (admin_password, credential.password, credential.verifier))
        return EXIT_STOPPED if progress.file_written or progress.role_created else EXIT_REFUSED
    if not proceeded:
        say("declined: nothing was changed.")
        return EXIT_REFUSED
    report_done(env_file)
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
