"""dora_contract_service.py — canonical DORA contracts and their signing parties (DORA-V2-002A).

What:  Creates, reads and amends tenant-owned contracts, records which
       organisations sign them and in what capacity, and looks contracts up
       by signing organisation. The single write path for dora_contracts and
       dora_contract_parties.

Why:   DORA_MODEL_V2.md §7.1, §7.2 and §20. A contract is one reusable record
       identified by the tenant's own reference, signed by one or more
       organisations that already exist in the V2-001 organisation graph.
       Signing is recorded; consuming is not inferred from it. Contract
       hierarchy, costs, ICT services, functions and service usages belong to
       later slices and are deliberately absent.

How to run or test:
    pytest tests/unit/services/test_dora_contract_service.py -v
    pytest tests/security/test_dora_contract_isolation.py -m integration -v
    pytest tests/integration/test_dora_v2_002a_contracts.py -m integration -v
    pytest tests/integration/test_dora_v2_002a_concurrency.py -m integration -v
    pytest tests/integration/test_dora_v2_002a_schema_parity.py -m integration -v

Write authorisation — recorded decision
----------------------------------------
This service does not check the actor's role, following dora_organization_service
and the DORA register services: actor_id and actor_role are recorded in the
ledger, and the allow/deny decision belongs to require_role() at a router,
where the JWT is verified. An actor_id passed to a function here is a claim
the caller makes, not an authentication. CONTRACT_CAPABLE_ROLES is the
allow-list a future router must apply. No router exists; nothing outside
tests reaches these functions.

Identifiers and indistinguishability
------------------------------------
Every tenant, contract and organisation id is canonicalised (lower-case,
hyphenated) before it reaches SQL, a returned object or a ledger state. A
malformed id cannot name a row, so it is treated exactly like an id this
tenant does not have — None, an empty list, or EntryNotFoundError — and never
reaches the driver, where a cast error would abort the caller's transaction.
A row belonging to another tenant is indistinguishable from a missing one.

Duplicates — the database decides
----------------------------------
create_contract and add_contract_party insert with INSERT ... ON CONFLICT ON
CONSTRAINT <that operation's unique constraint> DO NOTHING RETURNING. No row
back means the constraint already holds that value — including when a
concurrent transaction committed it a moment earlier — and the call raises
DORAContractConflictError without writing a ledger entry. The conflict clause
names one constraint, so a foreign-key failure, a CHECK failure or any other
integrity error still surfaces as the driver's own exception, unrelabelled.
After a conflict nothing was written and the caller's transaction remains
usable; after a driver error it is aborted and must be rolled back whole — no
statement is retried inside it.

Concurrency and lock order — recorded decision
-----------------------------------------------
update_contract reads the row with SELECT ... FOR NO KEY UPDATE before
capturing before_state, the pattern DORA-V2-001 established: a concurrent
amendment waits for the earlier one to commit and is then handed the
committed row, so every successful update ledgers the state it actually
replaced. That is the guarantee — accurate audit history. A stale edit is not
rejected: it waits, then wins, with an accurate trail. FOR NO KEY UPDATE
because the updated columns are not keys, so a party insert's foreign-key
check (FOR KEY SHARE) on the contract is not blocked. Read-only functions take
no locks.

Per call, every lock this service waits on comes before the tenant's ledger
advisory lock (pg_advisory_xact_lock inside append_audit_entry): the update
path's row lock; a party insert's implicit FOR KEY SHARE on the contract and
organisation; and an insert's wait on a concurrent, uncommitted duplicate
key. Per transaction that order does not hold. The advisory lock is
transaction-scoped, so a caller-owned transaction that has already ledgered
holds it while a later call waits on one of those locks; if the session it
waits on is itself queued for the advisory lock, PostgreSQL detects the cycle
and aborts one side with DeadlockDetected (SQLSTATE 40P01). The loser commits
nothing — no row, no ledger entry — and the chain stays valid. This is the
KER-107 limitation every ledgered service shares; this service does not claim
to be deadlock-free and does not redesign the ledger lock.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone

from config.constants import RbacRole
from src.db.rls import require_valid_tenant_uuid, set_tenant_context
from src.exceptions import DORAContractConflictError, EntryNotFoundError
from src.models.dora_contract import CONTRACT_TEXT_TRIM_CHARACTERS
from src.models.dora_contract_party import ALLOWED_PARTY_ROLES, PROVIDER_PARTY_ROLES
from src.models.dora_organization_role import ROLE_TYPE_ICT_PROVIDER
from src.services.audit_log import append_audit_entry

# Roles a future router must require before any write function below. Same
# membership as ORGANIZATION_CAPABLE_ROLES today, kept as its own constant so
# the two authorities can diverge deliberately (§17 Ticket A).
CONTRACT_CAPABLE_ROLES: tuple[RbacRole, ...] = (
    RbacRole.COMPLIANCE_LEAD,
    RbacRole.VCISO,
)

# KER-107 ledger vocabulary for this domain (DORA_MODEL_V2.md §31).
OBJECT_TYPE_CONTRACT = "dora_contract"
OBJECT_TYPE_CONTRACT_PARTY = "dora_contract_party"
ACTION_CONTRACT_CREATED = "dora_contract_created"
ACTION_CONTRACT_UPDATED = "dora_contract_updated"
ACTION_CONTRACT_PARTY_ADDED = "dora_contract_party_added"


# ---------------------------------------------------------------------------
# Input / output dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ContractInput:
    """The fields a caller supplies to create a contract. The reference is mandatory."""

    contract_reference: str
    display_name: str | None = None
    contract_start_date: date | None = None
    contract_end_date: date | None = None
    is_active: bool = True


@dataclasses.dataclass(frozen=True)
class ContractUpdate:
    """The complete replacement for a contract's four amendable fields.

    Every field is required, so an amendment cannot clear a date by omission.
    There is no contract_reference field: the reference cannot be changed.
    """

    display_name: str | None
    contract_start_date: date | None
    contract_end_date: date | None
    is_active: bool


@dataclasses.dataclass(frozen=True)
class ContractOutput:
    """One contract as persisted. Mirrors the dora_contracts columns."""

    contract_id: str
    tenant_id: str
    contract_reference: str
    display_name: str | None
    contract_start_date: date | None
    contract_end_date: date | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass(frozen=True)
class ContractPartyOutput:
    """One signing party as persisted. Mirrors the dora_contract_parties columns."""

    contract_party_id: str
    tenant_id: str
    contract_id: str
    organization_id: str
    party_role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# SQL constants — every tenant-scoped statement carries an explicit tenant_id
# predicate in addition to RLS (CLAUDE.md §3.3).
# ---------------------------------------------------------------------------

_CONTRACT_COLUMNS = """contract_id, tenant_id, contract_reference, display_name,
       contract_start_date, contract_end_date, is_active, created_at, updated_at"""

_INSERT_CONTRACT = """
INSERT INTO dora_contracts
    (contract_id, tenant_id, contract_reference, display_name,
     contract_start_date, contract_end_date, is_active, created_at, updated_at)
VALUES
    (:contract_id, :tenant_id, :contract_reference, :display_name,
     :contract_start_date, :contract_end_date, :is_active, :created_at, :updated_at)
ON CONFLICT ON CONSTRAINT uq_dora_contracts_tenant_reference DO NOTHING
RETURNING contract_id
"""

_SELECT_CONTRACT = f"""
SELECT {_CONTRACT_COLUMNS}
FROM dora_contracts
WHERE tenant_id = :tenant_id
  AND contract_id = :contract_id
"""

# The locking read behind update_contract: same projection and predicate as
# _SELECT_CONTRACT, so the row captured is the row the UPDATE overwrites.
_SELECT_CONTRACT_FOR_UPDATE = _SELECT_CONTRACT + "FOR NO KEY UPDATE\n"

_SELECT_CONTRACTS = f"""
SELECT {_CONTRACT_COLUMNS}
FROM dora_contracts
WHERE tenant_id = :tenant_id
ORDER BY contract_reference ASC, contract_id ASC
"""

_SELECT_CONTRACTS_FOR_ORGANIZATION = f"""
SELECT {_CONTRACT_COLUMNS}
FROM dora_contracts
WHERE tenant_id = :tenant_id
  AND EXISTS (
      SELECT 1
      FROM dora_contract_parties
      WHERE dora_contract_parties.tenant_id = :tenant_id
        AND dora_contract_parties.contract_id = dora_contracts.contract_id
        AND dora_contract_parties.organization_id = :organization_id
  )
ORDER BY contract_reference ASC, contract_id ASC
"""

_UPDATE_CONTRACT = """
UPDATE dora_contracts
SET display_name = :display_name,
    contract_start_date = :contract_start_date,
    contract_end_date = :contract_end_date,
    is_active = :is_active,
    updated_at = :updated_at
WHERE tenant_id = :tenant_id
  AND contract_id = :contract_id
"""

_SELECT_ORGANIZATION_ID = """
SELECT organization_id
FROM dora_organizations
WHERE tenant_id = :tenant_id
  AND organization_id = :organization_id
"""

_SELECT_ACTIVE_ORGANIZATION_ROLE = """
SELECT organization_role_id
FROM dora_organization_roles
WHERE tenant_id = :tenant_id
  AND organization_id = :organization_id
  AND role_type = :role_type
  AND is_active = TRUE
"""

_INSERT_PARTY = """
INSERT INTO dora_contract_parties
    (contract_party_id, tenant_id, contract_id, organization_id, party_role,
     is_active, created_at, updated_at)
VALUES
    (:contract_party_id, :tenant_id, :contract_id, :organization_id, :party_role,
     :is_active, :created_at, :updated_at)
ON CONFLICT ON CONSTRAINT uq_dora_contract_parties_tenant_tuple DO NOTHING
RETURNING contract_party_id
"""

_SELECT_PARTIES = """
SELECT contract_party_id, tenant_id, contract_id, organization_id, party_role,
       is_active, created_at, updated_at
FROM dora_contract_parties
WHERE tenant_id = :tenant_id
  AND contract_id = :contract_id
ORDER BY party_role ASC, organization_id ASC
"""


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def create_contract(
    conn, tenant_id, contract_input: ContractInput, *, actor_id, actor_role: str
) -> ContractOutput:
    """Validate, persist and return a new contract, ledgering who created it.

    Canonicalises the tenant and the input first (reference and display name
    trimmed at both ends by the contract whitespace policy, dates checked),
    sets tenant context, and inserts with the reference's unique constraint
    as the arbiter. Raises TenantContextMissingError on a bad tenant,
    ValueError on invalid input, and DORAContractConflictError when the tenant
    already has that reference — in which case nothing is written or ledgered.
    """
    tenant = _canonical_tenant(tenant_id)
    normalized = _normalize_contract_input(contract_input)
    set_tenant_context(conn, tenant)
    now = datetime.now(timezone.utc)
    created = ContractOutput(
        contract_id=str(uuid.uuid4()),
        tenant_id=tenant,
        contract_reference=normalized.contract_reference,
        display_name=normalized.display_name,
        contract_start_date=normalized.contract_start_date,
        contract_end_date=normalized.contract_end_date,
        is_active=normalized.is_active,
        created_at=now,
        updated_at=now,
    )
    if conn.execute(_INSERT_CONTRACT, dataclasses.asdict(created)).fetchone() is None:
        raise DORAContractConflictError(
            f"contract reference {created.contract_reference!r} already exists in this tenant."
        )
    _ledger(
        conn, tenant, actor_id=actor_id, actor_role=actor_role,
        action_type=ACTION_CONTRACT_CREATED, object_type=OBJECT_TYPE_CONTRACT,
        object_id=created.contract_id, before_state=None, after_state=_to_ledger_state(created),
    )
    return created


def get_contract(conn, tenant_id, contract_id) -> ContractOutput | None:
    """Return one contract for this tenant, or None if this tenant has no such contract."""
    tenant = _canonical_tenant(tenant_id)
    contract = _canonical_record_id(contract_id)
    if contract is None:
        return None
    set_tenant_context(conn, tenant)
    return _fetch_contract(conn, tenant, contract)


def list_contracts(conn, tenant_id) -> list[ContractOutput]:
    """Return every contract for this tenant, ordered by reference."""
    tenant = _canonical_tenant(tenant_id)
    set_tenant_context(conn, tenant)
    rows = conn.execute(_SELECT_CONTRACTS, {"tenant_id": tenant}).fetchall()
    return [_contract_row_to_output(row) for row in rows]


def update_contract(
    conn, tenant_id, contract_id, contract_update: ContractUpdate, *, actor_id, actor_role: str
) -> ContractOutput | None:
    """Replace a contract's amendable fields and return it, or None if this tenant has no such contract.

    Only display_name, the two dates and is_active can change; tenant,
    contract id and reference cannot. Reads the row under a FOR NO KEY UPDATE
    lock before capturing before_state, so the ledger entry records the state
    this write actually replaced even when another amendment committed first.
    Raises TenantContextMissingError on a bad tenant and ValueError on invalid
    input. Nothing is written or ledgered for a contract this tenant lacks.
    """
    tenant = _canonical_tenant(tenant_id)
    normalized = _normalize_contract_update(contract_update)
    contract = _canonical_record_id(contract_id)
    if contract is None:
        return None
    set_tenant_context(conn, tenant)
    previous = _lock_contract_for_update(conn, tenant, contract)
    if previous is None:
        return None
    updated = dataclasses.replace(
        previous,
        display_name=normalized.display_name,
        contract_start_date=normalized.contract_start_date,
        contract_end_date=normalized.contract_end_date,
        is_active=normalized.is_active,
        updated_at=datetime.now(timezone.utc),
    )
    conn.execute(_UPDATE_CONTRACT, dataclasses.asdict(updated))
    _ledger(
        conn, tenant, actor_id=actor_id, actor_role=actor_role,
        action_type=ACTION_CONTRACT_UPDATED, object_type=OBJECT_TYPE_CONTRACT,
        object_id=updated.contract_id,
        before_state=_to_ledger_state(previous), after_state=_to_ledger_state(updated),
    )
    return updated


# ---------------------------------------------------------------------------
# Signing parties
# ---------------------------------------------------------------------------


def add_contract_party(
    conn, tenant_id, contract_id, organization_id, party_role: str, *, actor_id, actor_role: str
) -> ContractPartyOutput:
    """Record that an organisation signs a contract in one capacity, and ledger it.

    Requires the contract and the organisation to exist in this tenant, and
    for provider_signatory and intragroup_provider_signatory requires the
    organisation to already hold the ict_provider role — the role is checked,
    never assigned. Raises TenantContextMissingError, ValueError (bad role or
    missing provider role), EntryNotFoundError (unknown contract or
    organisation), or DORAContractConflictError when the exact tuple is
    already recorded, in which case nothing is written or ledgered.
    """
    tenant = _canonical_tenant(tenant_id)
    _validate_party_role(party_role)
    contract = _require_id(contract_id, "contract")
    organization = _require_id(organization_id, "organisation")
    set_tenant_context(conn, tenant)
    _require_party_references(conn, tenant, contract, organization, party_role)
    now = datetime.now(timezone.utc)
    added = ContractPartyOutput(
        contract_party_id=str(uuid.uuid4()),
        tenant_id=tenant,
        contract_id=contract,
        organization_id=organization,
        party_role=party_role,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    if conn.execute(_INSERT_PARTY, dataclasses.asdict(added)).fetchone() is None:
        raise DORAContractConflictError(
            f"organisation {organization} is already recorded as {party_role} on contract {contract}."
        )
    _ledger(
        conn, tenant, actor_id=actor_id, actor_role=actor_role,
        action_type=ACTION_CONTRACT_PARTY_ADDED, object_type=OBJECT_TYPE_CONTRACT_PARTY,
        object_id=added.contract_party_id, before_state=None, after_state=_to_ledger_state(added),
    )
    return added


def list_contract_parties(conn, tenant_id, contract_id) -> list[ContractPartyOutput]:
    """Return every signing party on one contract for this tenant (empty if the tenant has no such contract)."""
    tenant = _canonical_tenant(tenant_id)
    contract = _canonical_record_id(contract_id)
    if contract is None:
        return []
    set_tenant_context(conn, tenant)
    rows = conn.execute(_SELECT_PARTIES, {"tenant_id": tenant, "contract_id": contract}).fetchall()
    return [_party_row_to_output(row) for row in rows]


def list_contracts_for_organization(conn, tenant_id, organization_id) -> list[ContractOutput]:
    """Return each contract this tenant's organisation signs, once, in any capacity, ordered by reference.

    Empty if the tenant has no such organisation or it signs nothing. A
    signing party is not a consumer; this says nothing about who uses a service.
    """
    tenant = _canonical_tenant(tenant_id)
    organization = _canonical_record_id(organization_id)
    if organization is None:
        return []
    set_tenant_context(conn, tenant)
    rows = conn.execute(
        _SELECT_CONTRACTS_FOR_ORGANIZATION, {"tenant_id": tenant, "organization_id": organization}
    ).fetchall()
    return [_contract_row_to_output(row) for row in rows]


# ---------------------------------------------------------------------------
# Identifiers, validation and normalisation
# ---------------------------------------------------------------------------


def _canonical_tenant(tenant_id) -> str:
    """Return the tenant id as a canonical UUIDv4 string, or raise TenantContextMissingError.

    None, empty, malformed and wrong-version ids all raise before any SQL runs.
    """
    return str(require_valid_tenant_uuid(tenant_id))


def _canonical_record_id(record_id) -> str | None:
    """Return a record id as a canonical lower-case UUID string, or None if it is not a UUID at all."""
    try:
        return str(uuid.UUID(str(record_id)))
    except ValueError:
        return None


def _require_id(record_id, noun: str) -> str:
    """Return a canonical record id, or raise EntryNotFoundError — a malformed id names nothing."""
    canonical = _canonical_record_id(record_id)
    if canonical is None:
        raise EntryNotFoundError(f"{noun} {record_id!r} not found")
    return canonical


def _canonical_text(value, field_name: str) -> str:
    """Return a string with the contract whitespace set trimmed from both ends; '' for None.

    Anything that is not a string is refused rather than coerced.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text; received {type(value).__name__}.")
    return value.strip(CONTRACT_TEXT_TRIM_CHARACTERS)


def _validate_dates(start, end) -> None:
    """Refuse a non-date (a datetime included) and an end date before the start date."""
    for field_name, value in (("contract_start_date", start), ("contract_end_date", end)):
        if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
            raise ValueError(f"{field_name} must be a date or None; received {type(value).__name__}.")
    if start is not None and end is not None and end < start:
        raise ValueError(f"contract_end_date {end} is before contract_start_date {start}.")


def _validate_is_active(value) -> None:
    """Refuse anything but a real boolean for is_active."""
    if not isinstance(value, bool):
        raise ValueError(f"is_active must be True or False; received {value!r}.")


def _normalize_contract_input(contract_input: ContractInput) -> ContractInput:
    """Return the creation input in canonical form, refusing a missing reference.

    The reference is required and never invented: a missing or blank one is
    a ValueError. The display name becomes None when blank. These are the
    same rules the database CHECKs enforce, applied first so the caller gets
    a sentence rather than a driver error.
    """
    reference = _canonical_text(contract_input.contract_reference, "contract_reference")
    if not reference:
        raise ValueError("contract_reference is required and must not be blank.")
    _validate_dates(contract_input.contract_start_date, contract_input.contract_end_date)
    _validate_is_active(contract_input.is_active)
    return ContractInput(
        contract_reference=reference,
        display_name=_canonical_text(contract_input.display_name, "display_name") or None,
        contract_start_date=contract_input.contract_start_date,
        contract_end_date=contract_input.contract_end_date,
        is_active=contract_input.is_active,
    )


def _normalize_contract_update(contract_update: ContractUpdate) -> ContractUpdate:
    """Return the amendment in canonical form: display name trimmed or None, dates and flag checked."""
    _validate_dates(contract_update.contract_start_date, contract_update.contract_end_date)
    _validate_is_active(contract_update.is_active)
    return dataclasses.replace(
        contract_update,
        display_name=_canonical_text(contract_update.display_name, "display_name") or None,
    )


def _validate_party_role(party_role: str) -> None:
    """Reject anything outside the initial party-role vocabulary."""
    if party_role not in ALLOWED_PARTY_ROLES:
        raise ValueError(
            f"party_role must be one of {sorted(ALLOWED_PARTY_ROLES)}; received {party_role!r}."
        )


# ---------------------------------------------------------------------------
# Reads behind the write paths — tenant context must already be set
# ---------------------------------------------------------------------------


def _fetch_contract(conn, tenant: str, contract: str) -> ContractOutput | None:
    """Read one contract for this tenant without locking it, or None."""
    row = conn.execute(_SELECT_CONTRACT, {"tenant_id": tenant, "contract_id": contract}).fetchone()
    return _contract_row_to_output(row) if row is not None else None


def _lock_contract_for_update(conn, tenant: str, contract: str) -> ContractOutput | None:
    """Read one contract for this tenant under a FOR NO KEY UPDATE row lock, or None.

    Only update_contract uses this. Under READ COMMITTED a concurrent
    amendment waits here for an uncommitted one and then sees its committed
    values, which is what keeps before_state honest.
    """
    row = conn.execute(
        _SELECT_CONTRACT_FOR_UPDATE, {"tenant_id": tenant, "contract_id": contract}
    ).fetchone()
    return _contract_row_to_output(row) if row is not None else None


def _require_party_references(conn, tenant: str, contract: str, organization: str, party_role: str) -> None:
    """Refuse a party whose contract or organisation this tenant lacks, or a provider without the role.

    Plain reads: nothing in this slice deletes a contract, an organisation or
    an organisation role, so what is found here still holds at the INSERT,
    and the composite foreign keys back it regardless.
    """
    if _fetch_contract(conn, tenant, contract) is None:
        raise EntryNotFoundError(f"contract {contract!r} not found")
    params = {"tenant_id": tenant, "organization_id": organization}
    if conn.execute(_SELECT_ORGANIZATION_ID, params).fetchone() is None:
        raise EntryNotFoundError(f"organisation {organization!r} not found")
    if party_role not in PROVIDER_PARTY_ROLES:
        return
    role_params = {**params, "role_type": ROLE_TYPE_ICT_PROVIDER}
    if conn.execute(_SELECT_ACTIVE_ORGANIZATION_ROLE, role_params).fetchone() is None:
        raise ValueError(
            f"{party_role} requires organisation {organization} to hold the "
            f"{ROLE_TYPE_ICT_PROVIDER} role; it does not, and it is not assigned here."
        )


# ---------------------------------------------------------------------------
# Row mapping and ledger
# ---------------------------------------------------------------------------


def _contract_row_to_output(row) -> ContractOutput:
    """Map a _CONTRACT_COLUMNS row (by position) to ContractOutput, ids canonical."""
    return ContractOutput(
        contract_id=str(row[0]),
        tenant_id=str(row[1]),
        contract_reference=row[2],
        display_name=row[3],
        contract_start_date=row[4],
        contract_end_date=row[5],
        is_active=row[6],
        created_at=row[7],
        updated_at=row[8],
    )


def _party_row_to_output(row) -> ContractPartyOutput:
    """Map a dora_contract_parties SELECT row (by position) to ContractPartyOutput."""
    return ContractPartyOutput(
        contract_party_id=str(row[0]),
        tenant_id=str(row[1]),
        contract_id=str(row[2]),
        organization_id=str(row[3]),
        party_role=row[4],
        is_active=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


def _to_ledger_state(record) -> dict:
    """Return a dataclass as a dict the ledger can serialise: timestamps as UTC ISO 8601, dates as ISO dates.

    Timestamps read back from PostgreSQL arrive in the session's zone; writing
    them as UTC makes an update's before_state equal the previous entry's
    after_state field for field.
    """
    state = dataclasses.asdict(record)
    for field_name, value in state.items():
        if isinstance(value, datetime):
            aware = value.astimezone(timezone.utc) if value.tzinfo is not None else value
            state[field_name] = aware.isoformat()
        elif isinstance(value, date):
            state[field_name] = value.isoformat()
    return state


def _ledger(
    conn, tenant: str, *, actor_id, actor_role: str, action_type: str, object_type: str,
    object_id: str, before_state: dict | None, after_state: dict,
) -> None:
    """Append one KER-107 entry on the caller's connection, after the business write it records.

    control_id is None throughout this domain: a contract is not a control
    assessment. Same transaction, same fate as the write.
    """
    append_audit_entry(
        conn,
        tenant,
        actor_id=actor_id,
        actor_role=actor_role,
        action_type=action_type,
        object_type=object_type,
        object_id=object_id,
        control_id=None,
        before_state=before_state,
        after_state=after_state,
    )
