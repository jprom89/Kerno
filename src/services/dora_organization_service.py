"""dora_organization_service.py — canonical DORA legal organisations (DORA-V2-001).

What:  Creates, reads and amends tenant-owned legal organisations, and attaches
       identifiers and roles to them. The single write path for
       dora_organizations, dora_organization_identifiers and
       dora_organization_roles.

Why:   DORA_MODEL_V2.md §5. A legal organisation — a bank, a subsidiary, an
       intra-group IT company, AWS — must exist once per tenant and be reused
       across every contract and arrangement that involves it, instead of being
       a name string copied into each register row (the v1 shape). Identity is
       separate from the Kerno tenant, separate from any role the organisation
       plays, and separate from the regulatory templates. This service holds
       exactly that foundation and nothing downstream of it: no contracts,
       services, functions, profiles, provenance or template fields.

How to run or test:
    pytest tests/unit/services/test_dora_organization_service.py -v
    pytest tests/security/test_dora_organization_isolation.py -m integration -v
    pytest tests/integration/test_dora_v2_001_organizations.py -m integration -v

Write authorisation — recorded decision
----------------------------------------
This service does not check the actor's role. That follows repository
precedent: the DORA register services (dora_roi_service,
dora_roi_submission_service) take actor_id and actor_role for the ledger and
leave the allow/deny decision to require_role() at the router, where the JWT
is verified. Building a second gate here would be a competing RBAC
implementation, which §17 Ticket A's design rejects. ORGANIZATION_CAPABLE_ROLES
below is the allow-list a future router must apply, and it is the same as the
live register's — filing authority, not the NIS2 evidence-pack list. Until a
router exists, nothing outside tests can reach these functions.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import datetime, timezone

from config.constants import RbacRole
from src.db.rls import set_tenant_context
from src.exceptions import EntryNotFoundError, TenantContextMissingError
from src.models.dora_organization_role import ALLOWED_ROLE_TYPES
from src.services.audit_log import append_audit_entry

# Roles a future router must require before any of the write functions below.
# Same membership as REGISTER_CAPABLE_ROLES on purpose, and a separate constant
# on purpose: editing the organisation graph and editing the v1 register are
# different authorities and may diverge (§17 Ticket A).
ORGANIZATION_CAPABLE_ROLES: tuple[RbacRole, ...] = (
    RbacRole.COMPLIANCE_LEAD,
    RbacRole.VCISO,
)

# KER-107 ledger vocabulary for this domain. Object types are the explicit
# dora_* names DORA_MODEL_V2.md §31 requires; actions are stable strings.
OBJECT_TYPE_ORGANIZATION = "dora_organization"
OBJECT_TYPE_ORGANIZATION_IDENTIFIER = "dora_organization_identifier"
OBJECT_TYPE_ORGANIZATION_ROLE = "dora_organization_role"
ACTION_ORGANIZATION_CREATED = "dora_organization_created"
ACTION_ORGANIZATION_UPDATED = "dora_organization_updated"
ACTION_ORGANIZATION_IDENTIFIER_ADDED = "dora_organization_identifier_added"
ACTION_ORGANIZATION_ROLE_ADDED = "dora_organization_role_added"

# Structural only: two ASCII letters after canonicalising to upper case. Not a
# country reference table — that is later, versioned reference data.
_COUNTRY_CODE_PATTERN = re.compile(r"^[A-Z]{2}$")


# ---------------------------------------------------------------------------
# Input / output dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class OrganizationInput:
    """The fields a caller supplies to create or amend an organisation."""

    legal_name: str
    country_code: str | None = None
    is_active: bool = True


@dataclasses.dataclass(frozen=True)
class OrganizationOutput:
    """One organisation as persisted. Mirrors the dora_organizations columns."""

    organization_id: str
    tenant_id: str
    legal_name: str
    country_code: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass(frozen=True)
class OrganizationIdentifierOutput:
    """One identifier as persisted. Mirrors the dora_organization_identifiers columns."""

    organization_identifier_id: str
    tenant_id: str
    organization_id: str
    identifier_type: str
    identifier_value: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass(frozen=True)
class OrganizationRoleOutput:
    """One role assignment as persisted. Mirrors the dora_organization_roles columns."""

    organization_role_id: str
    tenant_id: str
    organization_id: str
    role_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# SQL constants — every tenant-scoped statement carries an explicit tenant_id
# predicate in addition to RLS (§3.3: the policy is the safety net, not the
# primary enforcement).
# ---------------------------------------------------------------------------

_INSERT_ORGANIZATION = """
INSERT INTO dora_organizations
    (organization_id, tenant_id, legal_name, country_code, is_active,
     created_at, updated_at)
VALUES
    (:organization_id, :tenant_id, :legal_name, :country_code, :is_active,
     :created_at, :updated_at)
"""

_SELECT_ORGANIZATION = """
SELECT organization_id, tenant_id, legal_name, country_code, is_active,
       created_at, updated_at
FROM dora_organizations
WHERE tenant_id = :tenant_id
  AND organization_id = :organization_id
"""

_SELECT_ORGANIZATIONS = """
SELECT organization_id, tenant_id, legal_name, country_code, is_active,
       created_at, updated_at
FROM dora_organizations
WHERE tenant_id = :tenant_id
ORDER BY legal_name ASC, organization_id ASC
"""

_UPDATE_ORGANIZATION = """
UPDATE dora_organizations
SET legal_name = :legal_name,
    country_code = :country_code,
    is_active = :is_active,
    updated_at = :updated_at
WHERE tenant_id = :tenant_id
  AND organization_id = :organization_id
"""

_INSERT_IDENTIFIER = """
INSERT INTO dora_organization_identifiers
    (organization_identifier_id, tenant_id, organization_id,
     identifier_type, identifier_value, is_active, created_at, updated_at)
VALUES
    (:organization_identifier_id, :tenant_id, :organization_id,
     :identifier_type, :identifier_value, :is_active, :created_at, :updated_at)
"""

_SELECT_IDENTIFIER_BY_TYPE_VALUE = """
SELECT organization_id
FROM dora_organization_identifiers
WHERE tenant_id = :tenant_id
  AND identifier_type = :identifier_type
  AND identifier_value = :identifier_value
"""

_SELECT_IDENTIFIERS = """
SELECT organization_identifier_id, tenant_id, organization_id,
       identifier_type, identifier_value, is_active, created_at, updated_at
FROM dora_organization_identifiers
WHERE tenant_id = :tenant_id
  AND organization_id = :organization_id
ORDER BY identifier_type ASC, identifier_value ASC
"""

_INSERT_ROLE = """
INSERT INTO dora_organization_roles
    (organization_role_id, tenant_id, organization_id, role_type, is_active,
     created_at, updated_at)
VALUES
    (:organization_role_id, :tenant_id, :organization_id, :role_type, :is_active,
     :created_at, :updated_at)
"""

_SELECT_ROLE_BY_TYPE = """
SELECT organization_role_id
FROM dora_organization_roles
WHERE tenant_id = :tenant_id
  AND organization_id = :organization_id
  AND role_type = :role_type
"""

_SELECT_ROLES = """
SELECT organization_role_id, tenant_id, organization_id, role_type, is_active,
       created_at, updated_at
FROM dora_organization_roles
WHERE tenant_id = :tenant_id
  AND organization_id = :organization_id
ORDER BY role_type ASC
"""


# ---------------------------------------------------------------------------
# Organisations
# ---------------------------------------------------------------------------


def create_organization(
    conn, tenant_id, org_input: OrganizationInput, *, actor_id, actor_role: str
) -> OrganizationOutput:
    """Validate, persist and return a new organisation, ledgering who created it.

    Guards the tenant first, normalises the input (trimmed legal name, upper
    case two-letter country code or None), sets tenant context, inserts, and
    appends the KER-107 entry on the same connection so the row and the record
    of who added it commit or roll back together. Raises
    TenantContextMissingError on a bad tenant and ValueError on invalid input.
    """
    _guard_tenant(tenant_id)
    normalized = _normalize_organization_input(org_input)
    set_tenant_context(conn, tenant_id)
    now = datetime.now(timezone.utc)
    created = OrganizationOutput(
        organization_id=str(uuid.uuid4()),
        tenant_id=str(tenant_id),
        legal_name=normalized.legal_name,
        country_code=normalized.country_code,
        is_active=normalized.is_active,
        created_at=now,
        updated_at=now,
    )
    conn.execute(_INSERT_ORGANIZATION, dataclasses.asdict(created))
    _ledger(
        conn, tenant_id, actor_id=actor_id, actor_role=actor_role,
        action_type=ACTION_ORGANIZATION_CREATED,
        object_type=OBJECT_TYPE_ORGANIZATION,
        object_id=created.organization_id,
        before_state=None, after_state=_to_ledger_state(created),
    )
    return created


def get_organization(conn, tenant_id, organization_id: str) -> OrganizationOutput | None:
    """Return one organisation for this tenant, or None if it does not exist here."""
    _guard_tenant(tenant_id)
    set_tenant_context(conn, tenant_id)
    return _fetch_organization(conn, tenant_id, organization_id)


def list_organizations(conn, tenant_id) -> list[OrganizationOutput]:
    """Return every organisation for this tenant, ordered by legal name."""
    _guard_tenant(tenant_id)
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(_SELECT_ORGANIZATIONS, {"tenant_id": str(tenant_id)}).fetchall()
    return [_organization_row_to_output(row) for row in rows]


def update_organization(
    conn, tenant_id, organization_id: str, org_input: OrganizationInput,
    *, actor_id, actor_role: str,
) -> OrganizationOutput | None:
    """Amend an organisation and return it, or None if it does not exist here.

    Reads the row as it stands first so the ledger entry carries before_state
    as well as after_state — an amendment to a legal identity is only
    reconstructable if what it replaced was captured. Raises
    TenantContextMissingError on a bad tenant and ValueError on invalid input.
    """
    _guard_tenant(tenant_id)
    normalized = _normalize_organization_input(org_input)
    set_tenant_context(conn, tenant_id)
    previous = _fetch_organization(conn, tenant_id, organization_id)
    if previous is None:
        return None
    updated = dataclasses.replace(
        previous,
        legal_name=normalized.legal_name,
        country_code=normalized.country_code,
        is_active=normalized.is_active,
        updated_at=datetime.now(timezone.utc),
    )
    conn.execute(_UPDATE_ORGANIZATION, dataclasses.asdict(updated))
    _ledger(
        conn, tenant_id, actor_id=actor_id, actor_role=actor_role,
        action_type=ACTION_ORGANIZATION_UPDATED,
        object_type=OBJECT_TYPE_ORGANIZATION,
        object_id=updated.organization_id,
        before_state=_to_ledger_state(previous), after_state=_to_ledger_state(updated),
    )
    return updated


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def add_organization_identifier(
    conn, tenant_id, organization_id: str, identifier_type: str, identifier_value: str,
    *, actor_id, actor_role: str,
) -> OrganizationIdentifierOutput:
    """Attach one identifier to an organisation and ledger it.

    Normalises the type (trimmed, upper case) and value (trimmed), refuses a
    pair already used by any organisation in this tenant, and refuses an
    organisation this tenant does not have. The database UNIQUE and composite
    foreign key are the guarantees; the checks here give a clear message before
    the driver raises. Raises TenantContextMissingError, ValueError, or
    EntryNotFoundError accordingly.
    """
    _guard_tenant(tenant_id)
    canonical_type, canonical_value = _normalize_identifier(identifier_type, identifier_value)
    set_tenant_context(conn, tenant_id)
    _require_organization(conn, tenant_id, organization_id)
    _reject_duplicate_identifier(conn, tenant_id, canonical_type, canonical_value)
    now = datetime.now(timezone.utc)
    added = OrganizationIdentifierOutput(
        organization_identifier_id=str(uuid.uuid4()),
        tenant_id=str(tenant_id),
        organization_id=str(organization_id),
        identifier_type=canonical_type,
        identifier_value=canonical_value,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    conn.execute(_INSERT_IDENTIFIER, dataclasses.asdict(added))
    _ledger(
        conn, tenant_id, actor_id=actor_id, actor_role=actor_role,
        action_type=ACTION_ORGANIZATION_IDENTIFIER_ADDED,
        object_type=OBJECT_TYPE_ORGANIZATION_IDENTIFIER,
        object_id=added.organization_identifier_id,
        before_state=None, after_state=_to_ledger_state(added),
    )
    return added


def list_organization_identifiers(
    conn, tenant_id, organization_id: str
) -> list[OrganizationIdentifierOutput]:
    """Return every identifier on one organisation for this tenant."""
    _guard_tenant(tenant_id)
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(
        _SELECT_IDENTIFIERS,
        {"tenant_id": str(tenant_id), "organization_id": str(organization_id)},
    ).fetchall()
    return [_identifier_row_to_output(row) for row in rows]


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def add_organization_role(
    conn, tenant_id, organization_id: str, role_type: str, *, actor_id, actor_role: str
) -> OrganizationRoleOutput:
    """Assign one role to an organisation and ledger it.

    Accepts exactly the initial vocabulary (financial_entity, ict_provider),
    refuses a role the organisation already holds, and refuses an organisation
    this tenant does not have. An organisation may hold both roles. Raises
    TenantContextMissingError, ValueError, or EntryNotFoundError accordingly.
    """
    _guard_tenant(tenant_id)
    _validate_role_type(role_type)
    set_tenant_context(conn, tenant_id)
    _require_organization(conn, tenant_id, organization_id)
    _reject_duplicate_role(conn, tenant_id, organization_id, role_type)
    now = datetime.now(timezone.utc)
    added = OrganizationRoleOutput(
        organization_role_id=str(uuid.uuid4()),
        tenant_id=str(tenant_id),
        organization_id=str(organization_id),
        role_type=role_type,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    conn.execute(_INSERT_ROLE, dataclasses.asdict(added))
    _ledger(
        conn, tenant_id, actor_id=actor_id, actor_role=actor_role,
        action_type=ACTION_ORGANIZATION_ROLE_ADDED,
        object_type=OBJECT_TYPE_ORGANIZATION_ROLE,
        object_id=added.organization_role_id,
        before_state=None, after_state=_to_ledger_state(added),
    )
    return added


def list_organization_roles(conn, tenant_id, organization_id: str) -> list[OrganizationRoleOutput]:
    """Return every role on one organisation for this tenant."""
    _guard_tenant(tenant_id)
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(
        _SELECT_ROLES,
        {"tenant_id": str(tenant_id), "organization_id": str(organization_id)},
    ).fetchall()
    return [_role_row_to_output(row) for row in rows]


# ---------------------------------------------------------------------------
# Validation and normalisation
# ---------------------------------------------------------------------------


def _guard_tenant(tenant_id) -> None:
    """Raise TenantContextMissingError before anything else if tenant_id is falsey."""
    if not tenant_id:
        raise TenantContextMissingError("tenant_id is required for DORA organisation access")


def _normalize_organization_input(org_input: OrganizationInput) -> OrganizationInput:
    """Return the input with a trimmed legal name and a canonical country code.

    A blank legal name is rejected. A country code is trimmed and upper-cased,
    becomes None if blank, and must then be exactly two ASCII letters. These
    are the same rules the database CHECK constraints enforce, applied here so
    the caller gets a sentence rather than a driver error.
    """
    legal_name = (org_input.legal_name or "").strip()
    if not legal_name:
        raise ValueError("legal_name must not be empty.")
    country_code = (org_input.country_code or "").strip().upper() or None
    if country_code is not None and not _COUNTRY_CODE_PATTERN.match(country_code):
        raise ValueError(
            f"country_code must be exactly two letters; received {org_input.country_code!r}."
        )
    return OrganizationInput(
        legal_name=legal_name, country_code=country_code, is_active=bool(org_input.is_active)
    )


def _normalize_identifier(identifier_type: str, identifier_value: str) -> tuple[str, str]:
    """Return (type, value) as they will be persisted: type upper-cased, both trimmed, neither blank."""
    canonical_type = (identifier_type or "").strip().upper()
    if not canonical_type:
        raise ValueError("identifier_type must not be empty.")
    canonical_value = (identifier_value or "").strip()
    if not canonical_value:
        raise ValueError("identifier_value must not be empty.")
    return canonical_type, canonical_value


def _validate_role_type(role_type: str) -> None:
    """Reject anything outside the initial role vocabulary."""
    if role_type not in ALLOWED_ROLE_TYPES:
        raise ValueError(
            f"role_type must be one of {sorted(ALLOWED_ROLE_TYPES)}; received {role_type!r}."
        )


def _fetch_organization(conn, tenant_id, organization_id: str) -> OrganizationOutput | None:
    """Read one organisation for this tenant, or None. Requires tenant context to already be set."""
    row = conn.execute(
        _SELECT_ORGANIZATION,
        {"tenant_id": str(tenant_id), "organization_id": str(organization_id)},
    ).fetchone()
    return _organization_row_to_output(row) if row is not None else None


def _require_organization(conn, tenant_id, organization_id: str) -> None:
    """Raise EntryNotFoundError unless this tenant has this organisation.

    Checked under tenant context and with an explicit tenant predicate, so an
    organisation belonging to another tenant is indistinguishable from one that
    does not exist. Requires tenant context to already be set.
    """
    if _fetch_organization(conn, tenant_id, organization_id) is None:
        raise EntryNotFoundError(f"organisation {organization_id!r} not found")


def _reject_duplicate_identifier(conn, tenant_id, identifier_type: str, identifier_value: str) -> None:
    """Raise ValueError if this tenant already uses this (type, value) on any organisation.

    Requires tenant context to already be set.
    """
    row = conn.execute(
        _SELECT_IDENTIFIER_BY_TYPE_VALUE,
        {
            "tenant_id": str(tenant_id),
            "identifier_type": identifier_type,
            "identifier_value": identifier_value,
        },
    ).fetchone()
    if row is not None:
        raise ValueError(
            f"identifier {identifier_type} {identifier_value!r} already identifies an "
            "organisation in this tenant."
        )


def _reject_duplicate_role(conn, tenant_id, organization_id: str, role_type: str) -> None:
    """Raise ValueError if the organisation already holds this role.

    Requires tenant context to already be set.
    """
    row = conn.execute(
        _SELECT_ROLE_BY_TYPE,
        {
            "tenant_id": str(tenant_id),
            "organization_id": str(organization_id),
            "role_type": role_type,
        },
    ).fetchone()
    if row is not None:
        raise ValueError(f"organisation {organization_id!r} already holds role {role_type!r}.")


# ---------------------------------------------------------------------------
# Row mapping, parameters, ledger
# ---------------------------------------------------------------------------


def _organization_row_to_output(row) -> OrganizationOutput:
    """Map a dora_organizations SELECT row (by position) to OrganizationOutput."""
    return OrganizationOutput(
        organization_id=str(row[0]),
        tenant_id=str(row[1]),
        legal_name=row[2],
        country_code=row[3],
        is_active=row[4],
        created_at=row[5],
        updated_at=row[6],
    )


def _identifier_row_to_output(row) -> OrganizationIdentifierOutput:
    """Map a dora_organization_identifiers SELECT row (by position) to its output."""
    return OrganizationIdentifierOutput(
        organization_identifier_id=str(row[0]),
        tenant_id=str(row[1]),
        organization_id=str(row[2]),
        identifier_type=row[3],
        identifier_value=row[4],
        is_active=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


def _role_row_to_output(row) -> OrganizationRoleOutput:
    """Map a dora_organization_roles SELECT row (by position) to its output."""
    return OrganizationRoleOutput(
        organization_role_id=str(row[0]),
        tenant_id=str(row[1]),
        organization_id=str(row[2]),
        role_type=row[3],
        is_active=row[4],
        created_at=row[5],
        updated_at=row[6],
    )


def _to_ledger_state(record) -> dict:
    """Return a dataclass as a dict the audit ledger can serialise (datetimes → ISO 8601)."""
    state = dataclasses.asdict(record)
    for field_name, value in state.items():
        if isinstance(value, datetime):
            state[field_name] = value.isoformat()
    return state


def _ledger(
    conn, tenant_id, *, actor_id, actor_role: str, action_type: str, object_type: str,
    object_id: str, before_state: dict | None, after_state: dict,
) -> None:
    """Append one KER-107 entry on the caller's connection and transaction.

    control_id is None throughout this domain: an organisation is a legal
    counterparty, not a control assessment. Runs after the business write so
    the two share one transaction and one fate.
    """
    append_audit_entry(
        conn,
        tenant_id,
        actor_id=actor_id,
        actor_role=actor_role,
        action_type=action_type,
        object_type=object_type,
        object_id=object_id,
        control_id=None,
        before_state=before_state,
        after_state=after_state,
    )
