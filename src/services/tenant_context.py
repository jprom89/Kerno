"""Tenant context for a request — resolve the tenant from the session, then lock.

Plain-English summary
---------------------
Before Kerno reads or writes any tenant data, it has to tell the database which
company the request belongs to. This module is the one approved way to do that
from the application's business logic.

The single most important security rule here (CLAUDE.md Section 3) is *where the
tenant identity comes from*: it is read from the already-authenticated session —
the trusted record of who logged in — and **never** from anything the user typed
into the request. A user cannot ask to act as another company by putting a
different id in a form field, because that field is never consulted.

The flow is always: resolve the tenant from the session, validate it is a real
UUIDv4, set the database tenant context, and hand back the validated id.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/security/test_tenant_isolation.py -m "not integration" -v

The tests in that file verify that ``resolve_and_set_tenant_context`` raises
``TenantContextMissingError`` for missing, invalid, and non-UUIDv4 tenant IDs,
and that no SQL is issued when it does so.
"""

from __future__ import annotations

from uuid import UUID

from src.db.rls import require_valid_tenant_uuid, set_tenant_context
from src.exceptions import TenantContextMissingError

# Re-exported so callers can import the error from the service layer they already
# use, without reaching into the lower-level db package. (PROMPT File 5.)
__all__ = ["resolve_and_set_tenant_context", "TenantContextMissingError"]


def resolve_and_set_tenant_context(session, conn) -> UUID:
    """Lock the database to the logged-in user's tenant and return that tenant id.

    Reads the tenant identity from the authenticated session (never from raw
    request input), checks it is a valid UUIDv4, activates the database tenant
    isolation for the current transaction, and returns the validated tenant id
    for the caller to reuse. Raises ``TenantContextMissingError`` if the session
    cannot supply a valid tenant — in which case no query should run.
    """
    resolved_tenant_id = _resolve_tenant_id_from_session(session)
    validated_tenant_id = require_valid_tenant_uuid(resolved_tenant_id)
    set_tenant_context(conn, validated_tenant_id)
    return validated_tenant_id


def _resolve_tenant_id_from_session(session) -> object:
    """Ask the authenticated session for its tenant id — the only trusted source.

    Deliberately accepts nothing from request bodies, query strings, or headers:
    it only calls the session's own ``resolve_tenant_id()``. Raises
    ``TenantContextMissingError`` if there is no session or the session cannot
    provide a tenant, so the system fails closed rather than guessing.
    """
    if session is None:
        raise TenantContextMissingError(
            "No authenticated session is present; cannot resolve a tenant."
        )
    resolver = getattr(session, "resolve_tenant_id", None)
    if not callable(resolver):
        raise TenantContextMissingError(
            "Authenticated session does not expose resolve_tenant_id(); "
            "refusing to infer tenant identity from any other source."
        )
    return resolver()
