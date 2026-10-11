"""runtime_identity.py — the API's startup check that its database login is a restricted runtime identity.

What:  At startup, reads the identity of the application's own pooled
       connection (session_user, its role attributes, its memberships, and
       whether it owns the database or any object in it) and refuses to start
       outside development if that identity is superuser, BYPASSRLS,
       CREATEROLE or CREATEDB, is a member of another role, or owns the
       database or any object. It also refuses when KERNO_MIGRATION_DATABASE_URL
       (the owner/migrator URL) is present in the API's environment, and when
       the identity cannot be read at all. In development every problem is
       logged as a warning instead.
Why:   C2 (PILOT-READY-002, docs/pilot_readiness_kickoff.md §1.6). Row-level
       security binds a non-owner role; the table owner bypasses non-forced
       policies and can disable the ledger triggers. The runtime boundary
       holds only if the API really runs as the non-owner login, so the API
       checks rather than assumes it. This is a startup check of the identity
       the pool hands out; it does not stop a later change of DATABASE_URL in
       a running process, and it does not compare the database's recorded
       grant profile (the serving-profile ticket adds that).
How:   Called by src/api/app.py's lifespan. Offline tests:
       pytest tests/unit/api/test_runtime_identity_gate.py -v
       The live check against kerno_test_app runs in
       tests/security/test_runtime_identity_boundary.py.
"""

from __future__ import annotations

import dataclasses
import logging
import os

logger = logging.getLogger(__name__)

MIGRATION_DATABASE_URL_VARIABLE = "KERNO_MIGRATION_DATABASE_URL"

# Owned objects are counted in this database and among shared objects
# (dbid 0 — databases, tablespaces), which covers "owns the database".
_IDENTITY_SQL = """
SELECT session_user,
       roles.rolsuper, roles.rolbypassrls, roles.rolcreaterole, roles.rolcreatedb,
       (SELECT count(*) FROM pg_auth_members WHERE member = roles.oid),
       databases.datdba = roles.oid,
       (SELECT count(*) FROM pg_shdepend
        WHERE refclassid = 'pg_authid'::regclass AND refobjid = roles.oid AND deptype = 'o'
          AND dbid IN (0, databases.oid))
FROM pg_roles AS roles, pg_database AS databases
WHERE roles.rolname = session_user AND databases.datname = current_database()
"""


@dataclasses.dataclass(frozen=True)
class ConnectionIdentity:
    """What the API's pooled connection really is, as read from the catalogs."""

    session_user: str
    superuser: bool
    bypass_rls: bool
    create_role: bool
    create_database: bool
    memberships: int
    owns_database: bool
    owned_objects: int


def identity_problems(identity: ConnectionIdentity) -> list[str]:
    """Return every way the identity is an owner or elevated login (empty for a restricted runtime)."""
    problems = []
    for attribute in ("superuser", "bypass_rls", "create_role", "create_database"):
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


def enforce_runtime_identity(development: bool, read_identity=None) -> None:
    """Refuse startup outside development unless the API connects as a restricted runtime identity.

    Collects every problem — the owner URL in the environment, an unreadable
    identity, an elevated or owning login — and raises RuntimeError listing
    them outside development, or logs one warning in development. Messages
    name attributes and the variable, never a URL or credential.
    """
    problems = []
    if (os.environ.get(MIGRATION_DATABASE_URL_VARIABLE) or "").strip():
        problems.append(f"{MIGRATION_DATABASE_URL_VARIABLE} (the owner/migrator URL) is set in the API environment")
    reader = read_identity or read_connection_identity
    try:
        problems.extend(identity_problems(reader()))
    except Exception as exc:  # noqa: BLE001 — any failure to read the identity is itself a refusal
        problems.append(f"the connection identity could not be read ({type(exc).__name__})")
    if not problems:
        return
    message = f"the API database login is not a restricted runtime identity: {'; '.join(problems)}"
    if development:
        logger.warning("%s (allowed in development only)", message)
        return
    raise RuntimeError(message)
