"""Row-Level Security helpers — the application-layer half of tenant isolation.

Plain-English summary
---------------------
Kerno keeps every customer's ("tenant's") data in one shared PostgreSQL
database. The thing that stops Tenant A from ever seeing Tenant B's rows is a
two-part lock:

  1. A database Row-Level Security (RLS) policy that automatically filters every
     query by the "current tenant" recorded in a PostgreSQL session variable.
  2. This file, which sets that session variable to the correct tenant before
     any query runs.

The database policy is the safety net. The function in this file,
``set_tenant_context``, is the primary lock: it must be called inside the
caller's transaction, before any tenant data is read or written. If the tenant
identifier is missing or malformed, this code refuses to continue rather than
letting a query run with no tenant filter in place. (CLAUDE.md Section 3,
LEARNING_PIPELINE_SPEC.md Section 3.2.)

How to run or test
------------------
Unit tests (no database required):

    pytest tests/security/test_tenant_isolation.py -m "not integration" -v

These tests verify that ``set_tenant_context`` raises ``TenantContextMissingError``
for every invalid tenant form (None, empty, non-UUID, wrong UUID version) and
issues no SQL when it does so.
"""

from __future__ import annotations

from uuid import UUID

from config.constants import TENANT_ID_UUID_VERSION
from src.exceptions import TenantContextMissingError

# The PostgreSQL session variable that the RLS policy reads. Setting it with
# SET LOCAL scopes it to the current transaction only, so a tenant context can
# never leak into another request that happens to reuse the same pooled
# connection. (LEARNING_PIPELINE_SPEC.md Section 3.2.)
_TENANT_CONTEXT_VARIABLE = "app.current_tenant_id"


def set_tenant_context(conn, tenant_id: UUID | str) -> None:
    """Tell the database which tenant the next queries belong to.

    Sets the PostgreSQL session variable that switches on the tenant-isolation
    policy, so the database will only return rows owned by this tenant. Must be
    called inside an already-open transaction, before any tenant data is read
    or written. It does not open or close the transaction — the caller owns
    that. Refuses to run (raises ``TenantContextMissingError``) if the tenant
    identifier is missing, empty, or not a valid UUIDv4.
    """
    valid_tenant_id = require_valid_tenant_uuid(tenant_id)
    conn.execute(
        f"SET LOCAL {_TENANT_CONTEXT_VARIABLE} = %s",
        [str(valid_tenant_id)],
    )


def require_valid_tenant_uuid(tenant_id: UUID | str | None) -> UUID:
    """Return the tenant identifier as a checked UUIDv4, or fail loudly.

    The single place tenant identifiers are validated. Accepts either a UUID
    object or its string form, validates it explicitly (never relying on silent
    type coercion), and raises ``TenantContextMissingError`` for anything that is
    missing, empty, malformed, the wrong type, or not version 4. Returns the
    canonical UUID so callers that need the validated value can reuse it.
    (CLAUDE.md Sections 3 and 4.4.)
    """
    if tenant_id is None:
        raise TenantContextMissingError(
            "Tenant ID is required before any database query."
        )
    if isinstance(tenant_id, UUID):
        candidate = tenant_id
    elif isinstance(tenant_id, str):
        candidate = _parse_uuid_string(tenant_id)
    else:
        raise TenantContextMissingError(
            "Tenant ID must be a UUID or a UUID string; received unsupported "
            f"type '{type(tenant_id).__name__}'."
        )
    if candidate.version != TENANT_ID_UUID_VERSION:
        raise TenantContextMissingError(
            f"Tenant ID must be a UUIDv{TENANT_ID_UUID_VERSION} "
            f"(received a version {candidate.version} UUID)."
        )
    return candidate


def _parse_uuid_string(raw_tenant_id: str) -> UUID:
    """Turn a tenant-ID string into a UUID, rejecting blank or malformed input.

    Raises ``TenantContextMissingError`` if the string is empty or only
    whitespace, or if it is not a parseable UUID, so a bad identifier can never
    reach the database.
    """
    stripped = raw_tenant_id.strip()
    if not stripped:
        raise TenantContextMissingError("Tenant ID is empty.")
    try:
        return UUID(stripped)
    except ValueError:
        raise TenantContextMissingError("Tenant ID is not a valid UUID.") from None
