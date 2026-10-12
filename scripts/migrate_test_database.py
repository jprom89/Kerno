"""migrate_test_database.py — run Alembic against the approved disposable test database, and nothing else.

What:  Applies or reverts migrations on kerno_test one revision at a time.
       Before any migration code runs it proves, read-only, that the live
       connection is the owner-approved disposable target, then takes the same
       exclusive advisory lock the live test suite takes, re-proves the lock
       before every revision and once more after the last, and releases it on
       every exit path. It reports the target by identity only
       (role@host:port/database) and never prints a credential.
Why:   TEST-SAFETY-001. Plain `alembic` reads DATABASE_URL, and
       migrations/env.py loads the ordinary .env when it is unset — so a
       mistyped command reaches the development database. This wrapper reads
       only KERNO_TEST_DATABASE_URL / KERNO_TEST_DATABASE_APPROVAL (process
       environment or .env.test), disables .env loading, and refuses every
       other target. Normal `alembic` use is unchanged; nothing here
       redirects it. It never creates, drops or recreates a database.
How:   python scripts/migrate_test_database.py current
       python scripts/migrate_test_database.py upgrade head
       python scripts/migrate_test_database.py downgrade <revision|base>
       python scripts/migrate_test_database.py grant-runtime [--profile full|pilot]
       python scripts/migrate_test_database.py revoke-runtime
       Exit codes: 0 done; 1 a migration or grant change failed; 2 bad
       command, bad destination, configuration missing or refused, or a
       grant precondition unmet; 3 target refused, busy, or exclusivity lost.
       Offline tests: pytest tests/unit/test_database_safety_processes.py
       tests/unit/test_runtime_grant_commands.py -v

Runtime grants (C2, PILOT-READY-002; profiles PILOT-READY-003). grant-runtime
and revoke-runtime run as the owner kerno_test under the same lock (purpose
"migrate"), in one transaction, for the fixed role kerno_test_app.
grant-runtime applies the "full" profile unless --profile names "pilot"; no
other value, role, target or SQL is accepted. It refuses unless migration 030
(the profile lookup) is applied and the role exists with no elevated
attribute, no membership in either direction and no owned object; it then
grants CONNECT and calls kerno_bootstrap.apply_runtime_grants, which revokes
the role's previous set, applies the chosen one and records the assignment.
revoke-runtime (029 or later) calls kerno_bootstrap.revoke_runtime_grants and
revokes CONNECT. Neither reads the runtime settings or connects as the
runtime, and neither creates, alters or drops a role. A profile switch changes
grants for every session of that login: stop the app first and restart it
with the matching KERNO_SERVING_PROFILE after.
"""

from __future__ import annotations

import pathlib
import sys

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# The boundary comes before any repository or Alembic import.
from tests import _database_safety as test_database_safety  # noqa: E402

EXIT_DONE = 0
EXIT_MIGRATION_FAILED = 1
EXIT_CONFIGURATION_REFUSED = 2
EXIT_TARGET_REFUSED = 3

# "upgrade <revision>" and "downgrade <revision>" are a command and a destination.
_COMMAND_AND_DESTINATION = 2
# "grant-runtime --profile <profile>" is a command, an option and its value.
_COMMAND_OPTION_AND_VALUE = 3

_USAGE = (
    "usage: python scripts/migrate_test_database.py current\n"
    "       python scripts/migrate_test_database.py upgrade <revision|head>\n"
    "       python scripts/migrate_test_database.py downgrade <revision|base>\n"
    "       python scripts/migrate_test_database.py grant-runtime [--profile full|pilot]\n"
    "       python scripts/migrate_test_database.py revoke-runtime"
)

GRANT_RUNTIME = "grant-runtime"
REVOKE_RUNTIME = "revoke-runtime"
RUNTIME_GRANT_COMMANDS = (GRANT_RUNTIME, REVOKE_RUNTIME)
PROFILE_OPTION = "--profile"
# The two grant profiles of docs/pilot_readiness_kickoff.md §1.4. Without
# --profile, grant-runtime applies "full", exactly as it did before profiles.
RUNTIME_PROFILES = ("full", "pilot")
DEFAULT_RUNTIME_PROFILE = "full"
# Migration 029 creates the procedures; 030 adds the profile lookup that the
# grant set now includes, so grant-runtime needs 030 and revoke-runtime 029.
RUNTIME_MECHANISM_REVISION = "d5e6f7g8"
PROFILE_LOOKUP_REVISION = "e6f7g8h9"

_RUNTIME_ROLE = test_database_safety.TEST_RUNTIME_ROLE_NAME
_DATABASE = test_database_safety.TEST_DATABASE_NAME

# Fixed statements: the role, database and profiles are module constants and
# plain lowercase identifiers, never input; --profile only chooses a key.
GRANT_RUNTIME_STATEMENTS = {
    "full": (
        f"GRANT CONNECT ON DATABASE {_DATABASE} TO {_RUNTIME_ROLE}",
        f"CALL kerno_bootstrap.apply_runtime_grants('{_RUNTIME_ROLE}', 'full')",
    ),
    "pilot": (
        f"GRANT CONNECT ON DATABASE {_DATABASE} TO {_RUNTIME_ROLE}",
        f"CALL kerno_bootstrap.apply_runtime_grants('{_RUNTIME_ROLE}', 'pilot')",
    ),
}
REVOKE_RUNTIME_STATEMENTS = (
    f"CALL kerno_bootstrap.revoke_runtime_grants('{_RUNTIME_ROLE}')",
    f"REVOKE CONNECT ON DATABASE {_DATABASE} FROM {_RUNTIME_ROLE}",
)

_MECHANISM_PRESENT_SQL = """
SELECT to_regprocedure('kerno_bootstrap.apply_runtime_grants(name,text)') IS NOT NULL
   AND to_regprocedure('kerno_bootstrap.revoke_runtime_grants(name)') IS NOT NULL
   AND to_regclass('kerno_bootstrap.runtime_assignment') IS NOT NULL
"""
_PROFILE_LOOKUP_PRESENT_SQL = "SELECT to_regprocedure('kerno_bootstrap.session_runtime_profile()') IS NOT NULL"
_RUNTIME_ROLE_SQL = """
SELECT r.rolcanlogin, r.rolsuper, r.rolbypassrls, r.rolcreatedb, r.rolcreaterole, r.rolreplication,
       (SELECT count(*) FROM pg_auth_members AS m WHERE m.member = r.oid OR m.roleid = r.oid),
       (SELECT count(*) FROM pg_shdepend AS s
        WHERE s.refclassid = 'pg_authid'::regclass AND s.refobjid = r.oid AND s.deptype = 'o')
FROM pg_roles AS r WHERE r.rolname = %s
"""
_RUNTIME_ROLE_FIELDS = (
    "can_login", "superuser", "bypass_rls", "create_database", "create_role", "replication",
    "memberships", "owned_objects",
)
_ASSIGNMENT_SQL = "SELECT role_name, profile FROM kerno_bootstrap.runtime_assignment"


class RuntimeGrantRefused(Exception):
    """A grant-runtime or revoke-runtime precondition is unmet; nothing was changed."""


def main(argv: list[str], safety=test_database_safety) -> int:
    """Run one migration command against the approved test database and return the exit code.

    The exclusive session is released on every path, success or failure.
    """
    parsed = parse_arguments(argv)
    if parsed is None:
        say(_USAGE)
        return EXIT_CONFIGURATION_REFUSED
    command, destination = parsed
    try:
        target = safety.prepare_migration_process()
    except safety.KernoTestDatabaseError as exc:
        say(f"refused: {exc}")
        return EXIT_CONFIGURATION_REFUSED
    say(f"test database: {target.identity}")
    try:
        session = safety.ensure_exclusive_session("migrate")
        return run_command(command, destination, session, target, safety)
    except safety.KernoTestDatabaseError as exc:
        say(f"refused: {exc}")
        return EXIT_TARGET_REFUSED
    finally:
        safety.release_exclusive_session()


def parse_arguments(argv: list[str]) -> tuple[str, str | None] | None:
    """Return (command, argument) for a well-formed command line, else None.

    The argument is the destination revision for upgrade and downgrade, the
    allow-listed grant profile for grant-runtime ("full" when --profile is
    absent), and None otherwise.
    """
    if argv == ["current"]:
        return "current", None
    if argv == [REVOKE_RUNTIME]:
        return REVOKE_RUNTIME, None
    if argv == [GRANT_RUNTIME]:
        return GRANT_RUNTIME, DEFAULT_RUNTIME_PROFILE
    if len(argv) == _COMMAND_OPTION_AND_VALUE and argv[:2] == [GRANT_RUNTIME, PROFILE_OPTION]:
        return (GRANT_RUNTIME, argv[2]) if argv[2] in RUNTIME_PROFILES else None
    if len(argv) == _COMMAND_AND_DESTINATION and argv[0] in ("upgrade", "downgrade") and argv[1]:
        return argv[0], argv[1]
    return None


def run_command(command: str, destination: str | None, session, target, safety) -> int:
    """Report the current revision, or apply the planned steps one at a time under the held lock.

    The lock is re-proved before every step and after the last one, so a loss
    during the final revision is not reported as success. grant-runtime and
    revoke-runtime are handed to run_runtime_grant_command, with the parsed
    profile for grant-runtime.
    """
    if command in RUNTIME_GRANT_COMMANDS:
        profile = destination if command == GRANT_RUNTIME else DEFAULT_RUNTIME_PROFILE
        return run_runtime_grant_command(command, session, target, safety, profile=profile)
    try:
        config = alembic_config()
        current = current_revision(target)
        if command == "current":
            say(f"current revision: {current or 'base'}")
            return EXIT_DONE
        steps = _planned_steps(config, command, current, destination, target, safety)
        if steps is None:
            return EXIT_CONFIGURATION_REFUSED
        for step in steps:
            session.assert_held()
            apply_step(config, command, step)
            say(f"{command} -> {step}")
        session.assert_held()
        say(f"done: {command} {destination}")
        return EXIT_DONE
    except safety.DatabaseExclusivityLost as exc:
        say(f"stopped: {exc}")
        return EXIT_TARGET_REFUSED
    except safety.KernoTestDatabaseError as exc:
        say(f"refused: {exc}")
        return EXIT_TARGET_REFUSED
    except Exception as exc:  # noqa: BLE001 — reported without credentials, then a non-zero exit
        say(f"migration failed: {type(exc).__name__}: {safety.redact(str(exc), target)}")
        return EXIT_MIGRATION_FAILED


def _planned_steps(config, command: str, current: str | None, destination: str, target, safety) -> list[str] | None:
    """Return the plan, or None after reporting a destination the history cannot reach from here."""
    try:
        return plan_steps(config, command, current, destination)
    except Exception as exc:  # noqa: BLE001 — Alembic raises several types for an unusable destination
        say(f"cannot {command} to {destination!r} from {current or 'base'}: {safety.redact(str(exc), target)}")
        return None


def alembic_config():
    """Return the repository's Alembic configuration with an absolute script location."""
    from alembic.config import Config

    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
    return config


def current_revision(target) -> str | None:
    """Read the target's current Alembic revision (None on an empty database)."""
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    engine = create_engine(target.url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def plan_steps(config, command: str, current: str | None, destination: str) -> list[str]:
    """Return the single-revision targets to apply in order, so the lock can be re-proved between them.

    Upgrade yields each revision from just above current up to destination;
    downgrade yields each down_revision from current down to destination. A
    branched history is refused rather than guessed at.
    """
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(config)
    if len(script.get_heads()) != 1:
        raise RuntimeError(f"the migration history has {len(script.get_heads())} heads; the wrapper needs one.")
    if command == "upgrade":
        resolved = script.get_revision(destination).revision
        return [revision.revision for revision in reversed(list(script.iterate_revisions(resolved, current)))]
    if current is None:
        return []
    resolved = None if destination == "base" else script.get_revision(destination).revision
    return [revision.down_revision or "base" for revision in script.iterate_revisions(current, resolved)]


def run_runtime_grant_command(
    command: str, session, target, safety, connect=None, profile: str = DEFAULT_RUNTIME_PROFILE
) -> int:
    """Apply (grant-runtime, the given profile) or withdraw the fixed runtime role's grants as the owner.

    One transaction under the held lock. Refuses (exit 2, nothing changed) for
    a profile outside the allow-list, unless the required migration is in the
    current revision's history and its objects exist (030 for grant-runtime,
    029 for revoke-runtime), and, for grant-runtime, unless the fixed role
    exists with no elevated attribute, membership or owned object. The lock is
    re-proved before the commit and after it.
    """
    try:
        session.assert_held()
        if profile not in RUNTIME_PROFILES:
            raise RuntimeGrantRefused("the grant profile must be 'full' or 'pilot'.")
        required = PROFILE_LOOKUP_REVISION if command == GRANT_RUNTIME else RUNTIME_MECHANISM_REVISION
        if not includes_revision(alembic_config(), current_revision(target), required):
            raise RuntimeGrantRefused(f"migration {required} is not applied; run 'upgrade head' first.")
        connection = open_owner_connection(target, command, connect)
        try:
            change_runtime_grants(connection, command, session, profile)
        finally:
            connection.close()
        session.assert_held()
        say(_done_message(command, target, profile))
        return EXIT_DONE
    except RuntimeGrantRefused as exc:
        say(f"refused: {exc}")
        return EXIT_CONFIGURATION_REFUSED
    except safety.DatabaseExclusivityLost as exc:
        say(f"stopped: {exc}")
        return EXIT_TARGET_REFUSED
    except safety.KernoTestDatabaseError as exc:
        say(f"refused: {exc}")
        return EXIT_TARGET_REFUSED
    except Exception as exc:  # noqa: BLE001 — reported without credentials, then a non-zero exit
        say(f"{command} failed: {type(exc).__name__}: {safety.redact(str(exc), target)}")
        return EXIT_MIGRATION_FAILED


def _done_message(command: str, target, profile: str) -> str:
    """The success line: which command, role and target, and for grant-runtime which profile."""
    if command == GRANT_RUNTIME:
        return f"done: {command} {_RUNTIME_ROLE} on {target.identity} (profile {profile})"
    return f"done: {command} {_RUNTIME_ROLE} on {target.identity}"


def includes_revision(config, current: str | None, required: str) -> bool:
    """True when required is current or one of its ancestors in the migration history."""
    from alembic.script import ScriptDirectory

    if current is None:
        return False
    script = ScriptDirectory.from_config(config)
    return any(revision.revision == required for revision in script.iterate_revisions(current, "base"))


def open_owner_connection(target, command: str, connect=None):
    """Open one owner connection through the guarded psycopg2.connect, which re-proves the lock."""
    import psycopg2

    connect = connect or psycopg2.connect
    return connect(target.url, application_name=f"kerno-test-guard:{command}")


def change_runtime_grants(connection, command: str, session, profile: str = DEFAULT_RUNTIME_PROFILE) -> None:
    """Check preconditions, run the fixed statements, verify the assignment, re-prove the lock, then commit.

    Any failure rolls the whole transaction back, so a refusal or an error
    leaves the grants and the assignment exactly as they were.
    """
    connection.autocommit = False
    cursor = connection.cursor()
    try:
        require_runtime_mechanism(cursor, command)
        require_runtime_role(cursor, command)
        statements = GRANT_RUNTIME_STATEMENTS[profile] if command == GRANT_RUNTIME else REVOKE_RUNTIME_STATEMENTS
        for statement in statements:
            cursor.execute(statement)
        require_assignment(cursor, command, profile)
        session.assert_held()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def require_runtime_mechanism(cursor, command: str = GRANT_RUNTIME) -> None:
    """Refuse unless 029's procedures and assignment record exist, and for grant-runtime 030's profile lookup too."""
    cursor.execute(_MECHANISM_PRESENT_SQL)
    if not cursor.fetchone()[0]:
        raise RuntimeGrantRefused("the kerno_bootstrap grant procedures are missing; apply migration 029 first.")
    if command != GRANT_RUNTIME:
        return
    cursor.execute(_PROFILE_LOOKUP_PRESENT_SQL)
    if not cursor.fetchone()[0]:
        raise RuntimeGrantRefused("the kerno_bootstrap profile lookup is missing; apply migration 030 first.")


def require_runtime_role(cursor, command: str) -> None:
    """Refuse unless the fixed role exists; for grant-runtime, also unless it is unelevated and isolated."""
    cursor.execute(_RUNTIME_ROLE_SQL, (_RUNTIME_ROLE,))
    row = cursor.fetchone()
    if row is None:
        raise RuntimeGrantRefused(
            f"role {_RUNTIME_ROLE} does not exist; the administrator creates it with "
            "scripts/provision_test_runtime_role.py."
        )
    if command != GRANT_RUNTIME:
        return
    role = dict(zip(_RUNTIME_ROLE_FIELDS, row))
    problems = [name for name in _RUNTIME_ROLE_FIELDS[1:] if role[name]]
    if not role["can_login"]:
        problems.insert(0, "cannot log in")
    if problems:
        raise RuntimeGrantRefused(f"role {_RUNTIME_ROLE} is not a valid runtime role: {', '.join(problems)}.")


def require_assignment(cursor, command: str, profile: str = DEFAULT_RUNTIME_PROFILE) -> None:
    """Refuse to commit unless the assignment record now says exactly what this command did."""
    cursor.execute(_ASSIGNMENT_SQL)
    rows = cursor.fetchall()
    expected = [(_RUNTIME_ROLE, profile)] if command == GRANT_RUNTIME else []
    if [tuple(row) for row in rows] != expected:
        raise RuntimeGrantRefused(f"the runtime assignment after {command} is not what was applied; rolled back.")


def apply_step(config, command: str, step: str) -> None:
    """Apply exactly one revision step through Alembic."""
    from alembic import command as alembic_command

    if command == "upgrade":
        alembic_command.upgrade(config, step)
    else:
        alembic_command.downgrade(config, step)


def say(message: str) -> None:
    """Print one line for the operator."""
    print(message, flush=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
