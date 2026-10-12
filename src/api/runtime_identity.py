"""runtime_identity.py — the API's startup check that its database login is a restricted runtime identity.

What:  At startup, reads the identity of the application's own pooled
       connection (session_user, its role attributes, its memberships, and
       whether it owns the database or any object in it) and refuses to start
       outside development if that identity is superuser, BYPASSRLS,
       CREATEROLE, CREATEDB or REPLICATION, is a member of another role, or
       owns the database or any object. It also refuses when KERNO_MIGRATION_DATABASE_URL
       (the owner/migrator URL) is present in the API's environment, and when
       the identity cannot be read at all. It then asks the database, through
       the owner-defined helper kerno_bootstrap.session_runtime_profile()
       (migration 030), which grant profile is assigned to this login, and
       refuses unless it equals the serving profile the app was built with.
       In development a full-profile app only logs these problems as a
       warning; a pilot app refuses in every environment.
Why:   C2 (PILOT-READY-002, docs/pilot_readiness_kickoff.md §1.6). Row-level
       security binds a non-owner role; the table owner bypasses non-forced
       policies and can disable the ledger triggers. The runtime boundary
       holds only if the API really runs as the non-owner login, so the API
       checks rather than assumes it. This is a startup check of the identity
       the pool hands out; it does not stop a later change of DATABASE_URL in
       a running process. The profile check compares the owner's recorded
       label at startup; it does not attest that nobody changed the grants
       afterwards, so a profile switch needs the app stopped first and
       restarted after (docs/test_database_runbook.md §9).
How:   Called by src/api/app.py's lifespan. Offline tests:
       pytest tests/unit/api/test_runtime_identity_gate.py -v
       The live check against kerno_test_app runs in
       tests/security/test_runtime_identity_boundary.py.
"""

from __future__ import annotations

import dataclasses
import functools
import logging
import os

from src.api.serving_profile import PILOT_PROFILE, SERVING_PROFILES

logger = logging.getLogger(__name__)

MIGRATION_DATABASE_URL_VARIABLE = "KERNO_MIGRATION_DATABASE_URL"

# Owned objects are counted in this database and among shared objects
# (dbid 0 — databases, tablespaces), which covers "owns the database".
_IDENTITY_SQL = """
SELECT session_user,
       roles.rolsuper, roles.rolbypassrls, roles.rolcreaterole, roles.rolcreatedb, roles.rolreplication,
       (SELECT count(*) FROM pg_auth_members WHERE member = roles.oid),
       databases.datdba = roles.oid,
       (SELECT count(*) FROM pg_shdepend
        WHERE refclassid = 'pg_authid'::regclass AND refobjid = roles.oid AND deptype = 'o'
          AND dbid IN (0, databases.oid))
FROM pg_roles AS roles, pg_database AS databases
WHERE roles.rolname = session_user AND databases.datname = current_database()
"""

# The helper takes no argument: it answers only for session_user, the login
# that authenticated this connection, and returns the profile text or NULL.
_ASSIGNED_PROFILE_SQL = "SELECT kerno_bootstrap.session_runtime_profile()"


@dataclasses.dataclass(frozen=True)
class ConnectionIdentity:
    """What the API's pooled connection really is, as read from the catalogs."""

    session_user: str
    superuser: bool
    bypass_rls: bool
    create_role: bool
    create_database: bool
    replication: bool
    memberships: int
    owns_database: bool
    owned_objects: int


def identity_problems(identity: ConnectionIdentity) -> list[str]:
    """Return every way the identity is an owner or elevated login (empty for a restricted runtime)."""
    problems = []
    for attribute in ("superuser", "bypass_rls", "create_role", "create_database", "replication"):
        if getattr(identity, attribute):
            problems.append(f"the login has the {attribute} attribute")
    if identity.memberships:
        problems.append("the login is a member of another role")
    if identity.owns_database:
        problems.append("the login owns the database")
    if identity.owned_objects:
        problems.append("the login owns database objects")
    return problems


def read_connection_identity() -> ConnectionIdentity:
    """Lease one connection from the application pool, read its identity read-only, and return it."""
    from src.api import dependencies

    pool = dependencies._get_pool()
    raw_conn = pool.getconn()
    try:
        cursor = raw_conn.cursor()
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(_IDENTITY_SQL)
        row = cursor.fetchone()
    finally:
        try:
            raw_conn.rollback()
        finally:
            pool.putconn(raw_conn)
    if row is None:
        raise RuntimeError("the connection's role and database could not be read from the catalogs")
    return ConnectionIdentity(*row)


def read_assigned_profile() -> str | None:
    """Lease one pooled connection and return the grant profile the database assigns to this login, or None.

    Calls the owner-defined helper read-only; the runtime holds EXECUTE on it
    and nothing on the assignment table itself.
    """
    from src.api import dependencies

    pool = dependencies._get_pool()
    raw_conn = pool.getconn()
    try:
        cursor = raw_conn.cursor()
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(_ASSIGNED_PROFILE_SQL)
        row = cursor.fetchone()
    finally:
        try:
            raw_conn.rollback()
        finally:
            pool.putconn(raw_conn)
    return None if row is None else row[0]


def profile_problems(assigned: str | None, serving_profile: str) -> list[str]:
    """Return why the database's assignment for this login does not match the serving profile (empty when it does)."""
    if assigned is None:
        return [
            "no grant profile is assigned to this login (none is recorded, or it is recorded for another role); "
            f"this process serves {serving_profile!r}"
        ]
    if assigned not in SERVING_PROFILES:
        return ["the grant profile assigned to this login is not a recognised profile"]
    if assigned != serving_profile:
        return [
            f"the database assigns this login the {assigned!r} grant profile but this process "
            f"serves {serving_profile!r}"
        ]
    return []


def enforce_runtime_identity(
    development: bool, serving_profile: str, read_identity=None, read_profile=None
) -> None:
    """Refuse startup unless the API connects as a restricted runtime login assigned this serving profile.

    Collects every problem — the owner URL in the environment, an unreadable
    or elevated or owning login, a missing, unknown or different assigned
    profile — and raises RuntimeError listing them. Only a full-profile app in
    development logs them as one warning instead; a pilot app refuses in every
    environment. Messages name attributes, variables and profiles, never a URL
    or credential.
    """
    problems = _environment_problems()
    problems.extend(_read_problems(read_identity or read_connection_identity, identity_problems, "connection identity"))
    problems.extend(
        _read_problems(
            read_profile or read_assigned_profile,
            functools.partial(profile_problems, serving_profile=serving_profile),
            "assigned grant profile",
        )
    )
    if not problems:
        return
    message = f"the API database login is not a restricted runtime identity for this profile: {'; '.join(problems)}"
    if development and serving_profile != PILOT_PROFILE:
        logger.warning("%s (allowed for the full profile in development only)", message)
        return
    raise RuntimeError(message)


def _environment_problems() -> list[str]:
    """The owner/migrator URL must never be in the API's environment."""
    if (os.environ.get(MIGRATION_DATABASE_URL_VARIABLE) or "").strip():
        return [f"{MIGRATION_DATABASE_URL_VARIABLE} (the owner/migrator URL) is set in the API environment"]
    return []


def _read_problems(read, judge, what: str) -> list[str]:
    """Read one fact from the database and judge it; a failed read is itself a problem, named by type only."""
    try:
        return judge(read())
    except Exception as exc:  # noqa: BLE001 — any failure to read is itself a refusal
        return [f"the {what} could not be read ({type(exc).__name__})"]
