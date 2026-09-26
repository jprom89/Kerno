"""provision_test_database.py — one-time, owner-run creation of the disposable kerno_test database.

What:  Creates exactly what docs/test_database_runbook.md steps 1–4 describe:
       the restricted login role kerno_test (no SUPERUSER, CREATEDB,
       CREATEROLE, REPLICATION or BYPASSRLS, no memberships), the new
       database kerno_test owned by it, PUBLIC access to it revoked, the
       vector extension installed in it, and the disposable-database
       comment. Then writes the gitignored .env.test and verifies
       everything, including a real login as kerno_test.
Why:   On 26 September 2026 the owner approved
       kerno_test@127.0.0.1:5432/kerno_test as a disposable test database
       and delegated its setup, without typing each SQL statement. The
       administrator password must stay private, so the owner runs this
       script in their own window and types it once at a hidden prompt.
       Claude Code never sees it.
How:   In your own PowerShell window:
           cd J:\\Kerno
           python scripts\\provision_test_database.py
       Optional: --admin-role NAME (default postgres).
       Exit codes: 0 created and verified; 1 stopped after starting (the
       output lists exactly what was created); 2 refused before changing
       anything.

Safety, by construction
-----------------------
- **Refuses rather than reuses.** It stops, changing nothing, if a
  kerno_test role or database already exists, if .env.test already exists,
  or if a libpq variable that could redirect a connection is set. It never
  drops, renames, resets or alters an existing object.
- **Stops on partial failure.** Every statement's result is checked before
  the next one runs. On a failure it reports what was created and stops. It
  never cleans up by deleting and never re-runs over half-created objects.
- **Separate from the test guard.** It is its own process. It reads a few
  constants from tests/_database_safety.py but never installs, changes or
  disables the test connection guard, and it never reads .env.
- **Secrets stay out of view.**
  - The administrator password is typed once, is never echoed, printed or
    stored, and is refused if empty, so saved passwords are never used
    silently.
  - The test-role password is generated here. The server receives only its
    SCRAM-SHA-256 verifier. The plaintext is written only to .env.test.
- **Administrator connections are short-lived.** They are closed before the
  final login check, which runs as the restricted kerno_test role.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os
import pathlib
import secrets
import subprocess
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402

from tests._database_safety import (  # noqa: E402 — constants only; the guard is never installed here
    DEFAULT_TEST_ENV_FILE,
    DISPOSABLE_DATABASE_COMMENT,
    TARGET_CHANGING_LIBPQ_VARIABLES,
    TEST_DATABASE_APPROVAL_VARIABLE,
    TEST_DATABASE_NAME,
    TEST_DATABASE_URL_VARIABLE,
    TEST_ROLE_NAME,
)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5432
ADMIN_DATABASE = "postgres"
DEFAULT_ADMIN_ROLE = "postgres"
DEVELOPMENT_DATABASE = "kerno_dev"
APPLICATION_NAME = "kerno-test-provisioning"
CONNECT_TIMEOUT_SECONDS = 10
TEST_PASSWORD_BYTES = 32
SCRAM_ITERATIONS = 4096
SCRAM_SALT_BYTES = 16
EXPECTED_DATABASE_ACL = f"{{{TEST_ROLE_NAME}=CTc/{TEST_ROLE_NAME}}}"

# "--admin-role NAME" is an option and its value.
_OPTION_WITH_VALUE = 2

EXIT_DONE = 0
EXIT_STOPPED = 1
EXIT_REFUSED = 2


class ProvisioningStopped(Exception):
    """A check failed; the message says why and never contains a secret."""


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def scram_verifier(password: str, salt: bytes, iterations: int = SCRAM_ITERATIONS) -> str:
    """Return the SCRAM-SHA-256 verifier PostgreSQL stores for this password (RFC 5802 / RFC 7677).

    Sending the verifier instead of the password keeps the plaintext out of
    the server's statement log, exactly as psql's \\password does.
    """
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    encode = base64.b64encode
    return (
        f"SCRAM-SHA-256${iterations}:{encode(salt).decode()}"
        f"${encode(stored_key).decode()}:{encode(server_key).decode()}"
    )


def settings_url(password: str) -> str:
    """The URL written to .env.test. token_urlsafe passwords need no URL encoding."""
    return f"postgresql://{TEST_ROLE_NAME}:{password}@{SERVER_HOST}:{SERVER_PORT}/{TEST_DATABASE_NAME}"


def approved_identity() -> str:
    """The exact target identity the owner approved."""
    return f"{TEST_ROLE_NAME}@{SERVER_HOST}:{SERVER_PORT}/{TEST_DATABASE_NAME}"


def redact(text: str, *secrets_to_hide: str) -> str:
    """Remove every given secret from a message before it is printed."""
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(secret, "<hidden>")
    return text


# ---------------------------------------------------------------------------
# Checks that change nothing
# ---------------------------------------------------------------------------


def gitignored(path: pathlib.Path) -> bool:
    """True when git ignores the path; raises ProvisioningStopped if git cannot be asked."""
    try:
        result = subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=REPOSITORY_ROOT, capture_output=True)
    except OSError:
        raise ProvisioningStopped("git is not available to confirm .env.test is ignored.") from None
    return result.returncode == 0


def preflight(environ, env_file: pathlib.Path, is_ignored=gitignored) -> None:
    """Refuse before any connection: redirecting libpq variables, test settings already present, .env.test not ignored."""
    redirecting = [name for name in TARGET_CHANGING_LIBPQ_VARIABLES if environ.get(name)]
    if redirecting:
        raise ProvisioningStopped(f"unset {', '.join(redirecting)} first: libpq would apply them to the connection.")
    shell_settings = [name for name in (TEST_DATABASE_URL_VARIABLE, TEST_DATABASE_APPROVAL_VARIABLE) if name in environ]
    if shell_settings:
        raise ProvisioningStopped(
            f"unset {', '.join(shell_settings)} first: while set they would override the .env.test this writes."
        )
    if env_file.exists():
        raise ProvisioningStopped(f"{env_file.name} already exists; it is not overwritten. Nothing was changed.")
    if not is_ignored(env_file):
        raise ProvisioningStopped(f"{env_file.name} is not ignored by git; refusing to write a password there.")


def check_server(admin) -> None:
    """Confirm the administrator connection is the loopback server on the expected port, as a superuser."""
    row = _fetch_one(
        admin,
        "SELECT host(inet_server_addr()), inet_server_port(), rolsuper, split_part(version(), ' on ', 1) "
        "FROM pg_roles WHERE rolname = current_user",
    )
    address, port, superuser, version = row
    if address not in ("127.0.0.1", "::1") or port != SERVER_PORT:
        raise ProvisioningStopped("the administrator connection is not the loopback server on port 5432.")
    if not superuser:
        raise ProvisioningStopped("the administrator role is not a superuser; pgvector cannot be installed.")
    say(f"  ok  server {address}:{port}, {version}")


def refuse_existing(admin) -> None:
    """Stop, reporting its state, if a kerno_test role or database already exists."""
    role = _fetch_one(
        admin,
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
        "FROM pg_roles WHERE rolname = %s",
        [TEST_ROLE_NAME],
        allow_none=True,
    )
    database = _fetch_one(
        admin,
        "SELECT pg_get_userbyid(datdba), shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = %s",
        [TEST_DATABASE_NAME],
        allow_none=True,
    )
    if role is not None or database is not None:
        state = []
        if role is not None:
            state.append(f"role {TEST_ROLE_NAME} exists (login, super, createdb, createrole, replication, bypassrls = {role})")
        if database is not None:
            state.append(f"database {TEST_DATABASE_NAME} exists (owner {database[0]}, comment {database[1]!r})")
        raise ProvisioningStopped("; ".join(state) + ". Nothing was changed; this needs the owner's review.")
    say(f"  ok  no {TEST_ROLE_NAME} role and no {TEST_DATABASE_NAME} database exist yet")


def development_acl(admin):
    """kerno_dev's access list, read before and after to prove it is untouched."""
    row = _fetch_one(admin, "SELECT datacl FROM pg_database WHERE datname = %s", [DEVELOPMENT_DATABASE], allow_none=True)
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# The changes — each result checked before the next
# ---------------------------------------------------------------------------


def execute_checked(connection, statement, expected_status: str, description: str, created: list[str]) -> None:
    """Run one statement, require the expected command status, and record what now exists."""
    cursor = connection.cursor()
    cursor.execute(statement)
    if cursor.statusmessage != expected_status:
        raise ProvisioningStopped(f"{description}: expected {expected_status!r}, got {cursor.statusmessage!r}.")
    created.append(description)
    say(f"  ok  {description}")


def create_role_and_database(admin, test_password: str, created: list[str]) -> None:
    """CREATE ROLE (restricted, SCRAM verifier), CREATE DATABASE owned by it, REVOKE PUBLIC."""
    verifier = scram_verifier(test_password, secrets.token_bytes(SCRAM_SALT_BYTES))
    role = sql.Identifier(TEST_ROLE_NAME)
    database = sql.Identifier(TEST_DATABASE_NAME)
    execute_checked(
        admin,
        sql.SQL(
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
        ).format(role, sql.Literal(verifier)),
        "CREATE ROLE", f"role {TEST_ROLE_NAME} (restricted login)", created,
    )
    execute_checked(
        admin, sql.SQL("CREATE DATABASE {} OWNER {}").format(database, role),
        "CREATE DATABASE", f"database {TEST_DATABASE_NAME} owned by {TEST_ROLE_NAME}", created,
    )
    execute_checked(
        admin, sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database),
        "REVOKE", f"PUBLIC access to {TEST_DATABASE_NAME} revoked", created,
    )


def install_extension_and_marker(admin, connect_admin, created: list[str]) -> None:
    """CREATE EXTENSION vector inside kerno_test (its own short admin connection), then the comment."""
    in_test_database = connect_admin(TEST_DATABASE_NAME)
    try:
        in_test_database.autocommit = True
        if _fetch_one(in_test_database, "SELECT current_database()")[0] != TEST_DATABASE_NAME:
            raise ProvisioningStopped("the extension connection did not reach kerno_test.")
        execute_checked(in_test_database, "CREATE EXTENSION vector", "CREATE EXTENSION",
                        f"extension vector in {TEST_DATABASE_NAME}", created)
    finally:
        in_test_database.close()
    execute_checked(
        admin,
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(TEST_DATABASE_NAME), sql.Literal(DISPOSABLE_DATABASE_COMMENT)
        ),
        "COMMENT", "disposable-database marker (COMMENT ON DATABASE)", created,
    )


def write_settings(env_file: pathlib.Path, test_password: str, created: list[str]) -> None:
    """Create .env.test with exactly the two settings; never overwrite an existing file."""
    content = (
        f"{TEST_DATABASE_URL_VARIABLE}={settings_url(test_password)}\n"
        f"{TEST_DATABASE_APPROVAL_VARIABLE}={approved_identity()}\n"
    )
    with open(env_file, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    created.append(f"{env_file.name} (the two test settings; password only here)")
    say(f"  ok  wrote {env_file.name} (gitignored)")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_as_admin(admin, acl_before) -> None:
    """Runbook step 4 checks that need the catalog as administrator; kerno_dev must be unchanged."""
    attributes = _fetch_one(
        admin,
        "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin, "
        "(SELECT count(*) FROM pg_auth_members WHERE member = r.oid) FROM pg_roles r WHERE rolname = %s",
        [TEST_ROLE_NAME],
    )
    _require(tuple(attributes) == (False, False, False, False, False, True, 0),
             f"role {TEST_ROLE_NAME}: only LOGIN, no privileges, no memberships")
    owner, acl, comment = _fetch_one(
        admin,
        "SELECT pg_get_userbyid(datdba), datacl::text, shobj_description(oid, 'pg_database') "
        "FROM pg_database WHERE datname = %s",
        [TEST_DATABASE_NAME],
    )
    _require(owner == TEST_ROLE_NAME, f"database owner is {TEST_ROLE_NAME}")
    _require(acl == EXPECTED_DATABASE_ACL, f"database access list is {EXPECTED_DATABASE_ACL}")
    _require(comment == DISPOSABLE_DATABASE_COMMENT, "disposable-database marker is set")
    _require(development_acl(admin) == acl_before, f"{DEVELOPMENT_DATABASE} access list unchanged")


def verify_as_test_role(connect, test_password: str) -> None:
    """Log in as the restricted role, the way tests will, and check identity, extension and schema rights."""
    connection = connect(
        host=SERVER_HOST, port=SERVER_PORT, dbname=TEST_DATABASE_NAME, user=TEST_ROLE_NAME,
        password=test_password, application_name=f"{APPLICATION_NAME}-check",
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
    try:
        row = _fetch_one(
            connection,
            "SELECT current_database(), session_user, current_user, inet_server_port(), "
            "EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector'), "
            "has_schema_privilege('public', 'CREATE')",
        )
        connection.rollback()
    finally:
        connection.close()
    database, session_user, current_user, port, has_vector, can_create = row
    _require((database, session_user, current_user, port) == (TEST_DATABASE_NAME, TEST_ROLE_NAME, TEST_ROLE_NAME, SERVER_PORT),
             f"login as {TEST_ROLE_NAME} reaches {approved_identity()}")
    _require(has_vector is True, "extension vector installed")
    _require(can_create is True, f"{TEST_ROLE_NAME} can create tables in schema public")


def _require(condition: bool, description: str) -> None:
    """Print one verification result, or stop."""
    if not condition:
        raise ProvisioningStopped(f"verification failed: {description}.")
    say(f"  ok  verified: {description}")


def _fetch_one(connection, query: str, params=None, allow_none: bool = False):
    """Run a read and return its single row (None only when allowed)."""
    cursor = connection.cursor()
    cursor.execute(query, params)
    row = cursor.fetchone()
    if row is None and not allow_none:
        raise ProvisioningStopped("a catalog read returned no row.")
    return row


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def say(message: str) -> None:
    """Print one line for the owner."""
    print(message, flush=True)


def admin_connector(connect, admin_role: str, admin_password: str):
    """Return a function opening a short-lived administrator connection to a named database on the loopback server."""
    def connect_admin(database: str):
        """Open one administrator connection to the named database."""
        return connect(
            host=SERVER_HOST, port=SERVER_PORT, dbname=database, user=admin_role,
            password=admin_password, application_name=APPLICATION_NAME,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )
    return connect_admin


def provision(connect_admin, confirm, env_file: pathlib.Path, created: list[str]) -> str | None:
    """Check, ask, write settings, create, verify as administrator. Returns the test password, or None if declined."""
    admin = connect_admin(ADMIN_DATABASE)
    try:
        admin.autocommit = True
        check_server(admin)
        refuse_existing(admin)
        acl_before = development_acl(admin)
        if not confirm():
            return None
        test_password = secrets.token_urlsafe(TEST_PASSWORD_BYTES)
        write_settings(env_file, test_password, created)
        create_role_and_database(admin, test_password, created)
        install_extension_and_marker(admin, connect_admin, created)
        verify_as_admin(admin, acl_before)
        return test_password
    finally:
        admin.close()
        say("  ok  administrator connection closed")


def confirm_with_owner(ask) -> bool:
    """Show exactly what will be created and require the word yes."""
    say(f"\nAbout to create on {SERVER_HOST}:{SERVER_PORT}:")
    say(f"  - login role {TEST_ROLE_NAME}: NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS, generated password")
    say(f"  - database {TEST_DATABASE_NAME} owned by {TEST_ROLE_NAME}, PUBLIC access revoked")
    say(f"  - extension vector in {TEST_DATABASE_NAME}; comment '{DISPOSABLE_DATABASE_COMMENT}'")
    say(f"  - {DEFAULT_TEST_ENV_FILE.name} with the two test settings")
    say(f"Nothing else is changed; {DEVELOPMENT_DATABASE} is not touched.")
    return ask("Type yes to proceed: ").strip().lower() == "yes"


def run(argv, environ=None, connect=None, read_secret=None, ask=None, env_file=None, is_ignored=gitignored) -> int:
    """Drive the whole provisioning and return the exit code; every dependency is injectable for tests."""
    environ = os.environ if environ is None else environ
    env_file = env_file or DEFAULT_TEST_ENV_FILE
    admin_role = DEFAULT_ADMIN_ROLE
    if len(argv) == _OPTION_WITH_VALUE and argv[0] == "--admin-role" and argv[1]:
        admin_role = argv[1]
    elif argv:
        say("usage: python scripts/provision_test_database.py [--admin-role NAME]")
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
    """Provision, verify the restricted login, and report — hiding both passwords from every message."""
    created: list[str] = []
    test_password = None
    try:
        test_password = provision(admin_connector(connect, admin_role, admin_password),
                                  lambda: confirm_with_owner(ask), env_file, created)
        if test_password is None:
            say("declined: nothing was changed.")
            return EXIT_REFUSED
        verify_as_test_role(connect, test_password)
    except (ProvisioningStopped, psycopg2.Error, OSError) as exc:
        message = redact(str(exc).strip(), admin_password, test_password or "")
        say(f"\nSTOPPED: {type(exc).__name__}: {message}")
        say("Created before stopping: " + ("; ".join(created) if created else "nothing"))
        say("Nothing was deleted or retried. Report this output; do not re-run over it.")
        return EXIT_STOPPED if created else EXIT_REFUSED
    say(f"\nDONE: {approved_identity()} provisioned and verified.")
    say("Created: " + "; ".join(created))
    say("Next (Claude Code): python scripts/migrate_test_database.py upgrade head")
    return EXIT_DONE


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
