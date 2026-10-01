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
       Exit codes: 0 done; 1 a migration failed; 2 bad command, bad
       destination, or configuration missing or refused; 3 target refused,
       busy, or exclusivity lost.
       Offline tests: pytest tests/unit/test_database_safety.py -v
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

_USAGE = (
    "usage: python scripts/migrate_test_database.py current\n"
    "       python scripts/migrate_test_database.py upgrade <revision|head>\n"
    "       python scripts/migrate_test_database.py downgrade <revision|base>"
)


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
    """Return (command, destination) for a well-formed command line, else None."""
    if argv == ["current"]:
        return "current", None
    if len(argv) == _COMMAND_AND_DESTINATION and argv[0] in ("upgrade", "downgrade") and argv[1]:
        return argv[0], argv[1]
    return None


def run_command(command: str, destination: str | None, session, target, safety) -> int:
    """Report the current revision, or apply the planned steps one at a time under the held lock.

    The lock is re-proved before every step and after the last one, so a loss
    during the final revision is not reported as success.
    """
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
