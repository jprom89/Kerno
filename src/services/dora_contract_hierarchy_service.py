"""dora_contract_hierarchy_service.py — recorded contract hierarchy: child -> overarching parent (DORA-V2-002B).

What:  Records, reads and deactivates links saying that one contract's
       overarching contractual arrangement is another contract of the same
       tenant ("Hosting Order 2026 -> Master Services Agreement"). The single
       write path for dora_contract_relationships.
Why:   DORA_MODEL_V2.md §7.3. Hierarchy is its own table so dora_contracts
       never carries a parent column. These are operational records: nothing
       here produces B_02.01.0020 (arrangement type) or B_02.01.0030
       (overarching reference), classifies a contract from its position in the
       graph, or treats an SLA document as a separate arrangement. A contract
       with no recorded parent has no parent RECORDED — that is not proof it
       is standalone.
How:   pytest tests/unit/services/test_dora_contract_hierarchy_service.py -v
       pytest tests/integration/test_dora_v2_002b_hierarchy.py -m integration -v
       pytest tests/integration/test_dora_v2_002b_concurrency.py -m integration -v
       pytest tests/security/test_dora_contract_relationship_isolation.py -m integration -v

Direction and shape — recorded decision
---------------------------------------
A row is child -> parent. A child has at most one ACTIVE parent (Kerno's
modelling constraint for this slice, enforced by a partial unique index); a
parent may have many children, and hierarchies may be several levels deep. An
intermediate node is not classified, and a real case with two plausible
parents is refused for review, never resolved by picking one.

Lifecycle — recorded decision
-----------------------------
Endpoints and type never change. To correct a link the caller deactivates the
old row and adds a new one, both in one transaction it owns; if the add fails
the caller rolls the whole transaction back and the old link is active again.
An inactive row is never reactivated and no row is ever deleted. Deactivating
a link does not touch either contract, and deactivating a contract does not
touch its links: cycle and uniqueness checks follow active LINKS whatever the
state of the contracts at either end. A link to an inactive contract is a
documentary record; it says nothing about live service use.

Serialisation, cycles and isolation — recorded decision
-------------------------------------------------------
A cycle check that only reads is not enough: two transactions can each
validate one edge against the same old graph and together close a cycle. So
both write paths, before they read anything they validate, take the tenant's
ledger advisory lock — the same transaction-scoped lock append_audit_entry
takes — via audit_log.acquire_tenant_ledger_lock, and hold it until the
caller commits or rolls back. The lock is taken in its own statement and the
graph is read in later statements, so under READ COMMITTED every read sees
whatever a previous holder committed. That is why only READ COMMITTED is
accepted: at REPEATABLE READ or SERIALIZABLE the snapshot can predate the
wait. The level is checked before anything else and never changed; a
connection outside a caller-owned transaction (autocommit), where the lock
would end with its own statement, is caught by proving the lock is still held
before the first graph read. Both refusals raise
UnsupportedTransactionIsolationError having written nothing.

The cycle check walks active parent links upwards from the proposed parent.
It visits each contract at most once and refuses rather than stops: reaching
the proposed child is a cycle; reaching a contract it has already visited, or
a contract with more than one active parent, is pre-existing malformed data
(only writable by bypassing this service), and linking beneath it is refused
too. The walk is never truncated and then declared valid.

This protects graph integrity for writers that use this service. It does not
stop the table owner writing rows with raw SQL; the database itself enforces
only the self-link CHECK, the type CHECK, the composite foreign keys and one
active parent per child.

Lock interactions. Within one call, after taking the tenant lock, neither
write path waits on a lock a cooperating writer holds: add's reads take no
row locks and its foreign-key checks take FOR KEY SHARE on the two contract
rows, which nothing in src/ blocks (update_contract's FOR NO KEY UPDATE is
compatible); deactivate's FOR NO KEY UPDATE on the link row can only be
contended by another holder of the tenant lock, because only this service
writes links. Over the whole caller transaction it is different, in two
ways. After a hierarchy write the caller's transaction holds the tenant lock
until it ends, so a later call in the same transaction that waits on a row
lock can deadlock — for example update_contract(A) while another transaction
already holds A's row lock from its own update_contract and is queued for
the tenant lock in append_audit_entry. And locks the caller took earlier
without ledgering (raw SQL, an unledgered write) can be what another tenant
lock holder waits on. Either way PostgreSQL aborts one side with
DeadlockDetected (40P01); the loser commits nothing, and no statement is
retried inside the aborted transaction. That is the KER-107 per-transaction
limitation every ledgered service shares; it is not removed here.

Conflicts and errors
--------------------
A duplicate active link, a second active parent and a cycle each raise
DORAContractConflictError before anything is written. The insert also names
the partial unique index (ON CONFLICT (tenant_id, child_contract_id,
relationship_type) WHERE is_active DO NOTHING) as the final safeguard, so a
foreign-key, CHECK or other integrity failure still surfaces as the driver's
own exception, never relabelled as a duplicate. A direct self-link is invalid
input and raises ValueError before any SQL. Unknown ids and another tenant's
ids are indistinguishable: EntryNotFoundError on add, None or an empty list
elsewhere.

Write authorisation — recorded decision
----------------------------------------
As in dora_contract_service: actor_id and actor_role are validated (a UUID
naming a person, non-blank role) before any SQL and recorded in the ledger,
but they are claims, not authentication. The allow/deny decision belongs to
require_role() at a future router, which must apply
CONTRACT_RELATIONSHIP_CAPABLE_ROLES. No router exists.

Audit
-----
Each successful add or deactivation appends one KER-107 entry in the same
transaction as the write (object_type dora_contract_relationship). Creation
records the stored row; deactivation records the row before and after. A
refused add, an unknown or other tenant's link, and a repeat deactivation of
an inactive link append nothing.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

from config.constants import RbacRole
from src.db.rls import set_tenant_context
from src.exceptions import (
    DORAContractConflictError,
    EntryNotFoundError,
    UnsupportedTransactionIsolationError,
)
from src.models.dora_contract_relationship import RELATIONSHIP_TYPE_OVERARCHING
from src.services.audit_log import (
    acquire_tenant_ledger_lock,
    append_audit_entry,
    tenant_ledger_lock_is_held,
)

# The contract domain's own identifier and actor rules, reused rather than
# copied so the two services cannot disagree about what a valid tenant,
# contract id or actor is.
from src.services.dora_contract_service import (
    _canonical_actor,
    _canonical_record_id,
    _canonical_tenant,
    _require_id,
)

# Roles a future router must require before either write below. Same
# membership as CONTRACT_CAPABLE_ROLES today, kept as its own constant so the
# two authorities can diverge deliberately (CLAUDE.md §17 Ticket A).
CONTRACT_RELATIONSHIP_CAPABLE_ROLES: tuple[RbacRole, ...] = (
    RbacRole.COMPLIANCE_LEAD,
    RbacRole.VCISO,
)

# KER-107 ledger vocabulary for this table.
OBJECT_TYPE_CONTRACT_RELATIONSHIP = "dora_contract_relationship"
ACTION_CONTRACT_RELATIONSHIP_CREATED = "dora_contract_relationship_created"
ACTION_CONTRACT_RELATIONSHIP_DEACTIVATED = "dora_contract_relationship_deactivated"

# The only isolation level the write paths accept, as PostgreSQL spells it.
SUPPORTED_ISOLATION_LEVEL = "read committed"


@dataclasses.dataclass(frozen=True)
class ContractRelationshipOutput:
    """One recorded link as persisted. Mirrors the dora_contract_relationships columns."""

    contract_relationship_id: str
    tenant_id: str
    child_contract_id: str
    parent_contract_id: str
    relationship_type: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass(frozen=True)
class ContractRelationshipDeactivation:
    """What deactivate_contract_relationship did.

    relationship is the link as stored after the call. changed is True when
    this call deactivated it and False when it was already inactive — the
    explicit no-op, which wrote and ledgered nothing.
    """

    relationship: ContractRelationshipOutput
    changed: bool


# ---------------------------------------------------------------------------
# SQL constants — every tenant-scoped statement carries an explicit tenant_id
# predicate in addition to RLS (CLAUDE.md §3.3).
# ---------------------------------------------------------------------------

_RELATIONSHIP_COLUMNS = """contract_relationship_id, tenant_id, child_contract_id, parent_contract_id,
       relationship_type, is_active, created_at, updated_at"""

_SELECT_ISOLATION_LEVEL = "SELECT current_setting('transaction_isolation')"

_SELECT_CONTRACT_ID = """
SELECT contract_id
FROM dora_contracts
WHERE tenant_id = :tenant_id
  AND contract_id = :contract_id
"""

_SELECT_RELATIONSHIP = f"""
SELECT {_RELATIONSHIP_COLUMNS}
FROM dora_contract_relationships
WHERE tenant_id = :tenant_id
  AND contract_relationship_id = :contract_relationship_id
"""

# The locking read behind deactivation: same projection and predicate as
# _SELECT_RELATIONSHIP, so the row captured is the row the UPDATE changes.
# is_active belongs only to a partial index, which PostgreSQL does not count
# as a key, so the UPDATE itself takes FOR NO KEY UPDATE too.
_SELECT_RELATIONSHIP_FOR_UPDATE = _SELECT_RELATIONSHIP + "FOR NO KEY UPDATE\n"

_SELECT_ACTIVE_PARENT_LINKS = f"""
SELECT {_RELATIONSHIP_COLUMNS}
FROM dora_contract_relationships
WHERE tenant_id = :tenant_id
  AND child_contract_id = :child_contract_id
  AND relationship_type = :relationship_type
  AND is_active
ORDER BY created_at ASC, contract_relationship_id ASC
"""

_SELECT_ACTIVE_CHILD_LINKS = f"""
SELECT {_RELATIONSHIP_COLUMNS}
FROM dora_contract_relationships
WHERE tenant_id = :tenant_id
  AND parent_contract_id = :parent_contract_id
  AND relationship_type = :relationship_type
  AND is_active
ORDER BY created_at ASC, contract_relationship_id ASC
"""

_SELECT_CHILD_HISTORY = f"""
SELECT {_RELATIONSHIP_COLUMNS}
FROM dora_contract_relationships
WHERE tenant_id = :tenant_id
  AND child_contract_id = :child_contract_id
ORDER BY created_at ASC, contract_relationship_id ASC
"""

# The conflict target is the partial unique index, named by its columns and
# predicate, so only a second active link for the child is absorbed here.
_INSERT_RELATIONSHIP = f"""
INSERT INTO dora_contract_relationships
    (contract_relationship_id, tenant_id, child_contract_id, parent_contract_id,
     relationship_type, is_active, created_at, updated_at)
VALUES
    (:contract_relationship_id, :tenant_id, :child_contract_id, :parent_contract_id,
     :relationship_type, TRUE, :created_at, :updated_at)
ON CONFLICT (tenant_id, child_contract_id, relationship_type) WHERE is_active DO NOTHING
RETURNING {_RELATIONSHIP_COLUMNS}
"""

_DEACTIVATE_RELATIONSHIP = f"""
UPDATE dora_contract_relationships
SET is_active = FALSE,
    updated_at = :updated_at
WHERE tenant_id = :tenant_id
  AND contract_relationship_id = :contract_relationship_id
  AND is_active
RETURNING {_RELATIONSHIP_COLUMNS}
"""


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def add_contract_relationship(
    conn, tenant_id, child_contract_id, parent_contract_id, *, actor_id, actor_role: str
) -> ContractRelationshipOutput:
    """Record that the child contract's overarching arrangement is the parent, ledger it, and return the stored link.

    Validates the tenant, actor and ids, refuses a self-link (ValueError), then
    serialises on the tenant ledger lock before reading anything it checks:
    both contracts must exist in this tenant (EntryNotFoundError otherwise),
    the child must have no active parent (DORAContractConflictError for the
    same parent or a different one), and the parent's recorded ancestry must
    not reach the child (DORAContractConflictError). Raises
    UnsupportedTransactionIsolationError outside a READ COMMITTED,
    caller-owned transaction. Nothing is written or ledgered on any refusal.
    """
    tenant = _canonical_tenant(tenant_id)
    actor, role = _canonical_actor(actor_id, actor_role)
    child = _require_id(child_contract_id, "contract")
    parent = _require_id(parent_contract_id, "contract")
    if child == parent:
        raise ValueError("a contract cannot be recorded as its own overarching arrangement.")
    _serialize_hierarchy_writes(conn, tenant)
    _require_contract(conn, tenant, child)
    _require_contract(conn, tenant, parent)
    _refuse_existing_parent(conn, tenant, child, parent)
    _refuse_cycle(conn, tenant, child, parent)
    created = _insert_relationship(conn, tenant, child, parent)
    _ledger(
        conn, tenant, actor_id=actor, actor_role=role,
        action_type=ACTION_CONTRACT_RELATIONSHIP_CREATED, object_id=created.contract_relationship_id,
        before_state=None, after_state=_to_ledger_state(created),
    )
    return created


def deactivate_contract_relationship(
    conn, tenant_id, contract_relationship_id, *, actor_id, actor_role: str
) -> ContractRelationshipDeactivation | None:
    """Deactivate one link and ledger it; return what happened, or None if this tenant has no such link.

    Serialised on the tenant ledger lock like add_contract_relationship, then
    reads the link under a FOR NO KEY UPDATE row lock so before_state is the
    state actually replaced. An already inactive link is returned with
    changed=False and nothing is written or ledgered. Neither contract is
    touched. Raises UnsupportedTransactionIsolationError outside a READ
    COMMITTED, caller-owned transaction.
    """
    tenant = _canonical_tenant(tenant_id)
    actor, role = _canonical_actor(actor_id, actor_role)
    relationship_id = _canonical_record_id(contract_relationship_id)
    if relationship_id is None:
        return None
    _serialize_hierarchy_writes(conn, tenant)
    previous = _fetch_relationship(conn, _SELECT_RELATIONSHIP_FOR_UPDATE, tenant, relationship_id)
    if previous is None:
        return None
    if not previous.is_active:
        return ContractRelationshipDeactivation(relationship=previous, changed=False)
    row = conn.execute(
        _DEACTIVATE_RELATIONSHIP,
        {
            "tenant_id": tenant,
            "contract_relationship_id": relationship_id,
            "updated_at": datetime.now(timezone.utc),
        },
    ).fetchone()
    deactivated = _row_to_output(row)
    _ledger(
        conn, tenant, actor_id=actor, actor_role=role,
        action_type=ACTION_CONTRACT_RELATIONSHIP_DEACTIVATED, object_id=relationship_id,
        before_state=_to_ledger_state(previous), after_state=_to_ledger_state(deactivated),
    )
    return ContractRelationshipDeactivation(relationship=deactivated, changed=True)


# ---------------------------------------------------------------------------
# Reads — plain, no locks, deterministic order
# ---------------------------------------------------------------------------


def get_contract_relationship(conn, tenant_id, contract_relationship_id) -> ContractRelationshipOutput | None:
    """Return one link, active or not, or None if this tenant has no such link."""
    tenant = _canonical_tenant(tenant_id)
    relationship_id = _canonical_record_id(contract_relationship_id)
    if relationship_id is None:
        return None
    set_tenant_context(conn, tenant)
    return _fetch_relationship(conn, _SELECT_RELATIONSHIP, tenant, relationship_id)


def get_recorded_parent(conn, tenant_id, child_contract_id) -> ContractRelationshipOutput | None:
    """Return the child's active overarching link, or None when no parent is recorded.

    None means only that no parent is recorded — never that the contract is
    standalone — and it is also the answer for an unknown or another
    tenant's contract. More than one active parent cannot be stored while
    the partial unique index exists; if it ever is, this raises
    DORAContractConflictError rather than choose one.
    """
    tenant = _canonical_tenant(tenant_id)
    child = _canonical_record_id(child_contract_id)
    if child is None:
        return None
    set_tenant_context(conn, tenant)
    links = _active_parent_links(conn, tenant, child)
    if len(links) > 1:
        raise DORAContractConflictError(
            f"contract {child} has more than one active overarching arrangement recorded; it needs review."
        )
    return links[0] if links else None


def list_recorded_children(conn, tenant_id, parent_contract_id) -> list[ContractRelationshipOutput]:
    """Return the active links naming this contract as parent, oldest first (empty if none or unknown)."""
    tenant = _canonical_tenant(tenant_id)
    parent = _canonical_record_id(parent_contract_id)
    if parent is None:
        return []
    set_tenant_context(conn, tenant)
    rows = conn.execute(
        _SELECT_ACTIVE_CHILD_LINKS,
        {"tenant_id": tenant, "parent_contract_id": parent, "relationship_type": RELATIONSHIP_TYPE_OVERARCHING},
    ).fetchall()
    return [_row_to_output(row) for row in rows]


def list_contract_relationship_history(conn, tenant_id, child_contract_id) -> list[ContractRelationshipOutput]:
    """Return every link ever recorded for this child, active and inactive, oldest first (empty if none or unknown)."""
    tenant = _canonical_tenant(tenant_id)
    child = _canonical_record_id(child_contract_id)
    if child is None:
        return []
    set_tenant_context(conn, tenant)
    rows = conn.execute(_SELECT_CHILD_HISTORY, {"tenant_id": tenant, "child_contract_id": child}).fetchall()
    return [_row_to_output(row) for row in rows]


# ---------------------------------------------------------------------------
# Serialisation and the checks behind add — tenant context set by the first
# ---------------------------------------------------------------------------


def _serialize_hierarchy_writes(conn, tenant: str) -> None:
    """Refuse an unsupported transaction, set tenant context, take the tenant ledger lock and prove it is held.

    Separate statements in this order, all before the first graph read. The
    isolation level is read, never set. The held check is what catches an
    autocommit connection, where the lock would already be gone.
    """
    level = conn.execute(_SELECT_ISOLATION_LEVEL).fetchone()[0]
    if level != SUPPORTED_ISOLATION_LEVEL:
        raise UnsupportedTransactionIsolationError(
            f"contract hierarchy writes require a {SUPPORTED_ISOLATION_LEVEL.upper()} transaction; "
            f"this one is {str(level).upper()}. Nothing was read or written."
        )
    set_tenant_context(conn, tenant)
    acquire_tenant_ledger_lock(conn, tenant)
    if not tenant_ledger_lock_is_held(conn, tenant):
        raise UnsupportedTransactionIsolationError(
            "contract hierarchy writes need a transaction the caller owns; the tenant lock did not "
            "outlive its own statement (autocommit?). Nothing was read or written."
        )


def _require_contract(conn, tenant: str, contract: str) -> None:
    """Raise EntryNotFoundError unless this tenant has the contract — another tenant's looks the same as none."""
    if conn.execute(_SELECT_CONTRACT_ID, {"tenant_id": tenant, "contract_id": contract}).fetchone() is None:
        raise EntryNotFoundError(f"contract {contract!r} not found")


def _refuse_existing_parent(conn, tenant: str, child: str, parent: str) -> None:
    """Raise DORAContractConflictError if the child already has an active parent, the same one or another."""
    links = _active_parent_links(conn, tenant, child)
    if not links:
        return
    if len(links) > 1:
        raise DORAContractConflictError(
            f"contract {child} has more than one active overarching arrangement recorded; it needs review."
        )
    existing = links[0].parent_contract_id
    if existing == parent:
        raise DORAContractConflictError(f"contract {child} is already recorded under contract {parent}.")
    raise DORAContractConflictError(
        f"contract {child} already has an active overarching arrangement (contract {existing}); "
        "deactivate that link before recording another."
    )


def _refuse_cycle(conn, tenant: str, child: str, parent: str) -> None:
    """Raise DORAContractConflictError if following active links upward from the parent can reach the child.

    Each contract is visited at most once, so the walk ends on any data. A
    revisited contract or a contract with several active parents is
    malformed history; it is refused, never skipped.
    """
    visited: set[str] = set()
    current = parent
    while True:
        if current == child:
            raise DORAContractConflictError(
                f"recording contract {parent} as the overarching arrangement of contract {child} "
                "would create a cycle."
            )
        if current in visited:
            raise DORAContractConflictError(
                f"the recorded hierarchy above contract {parent} already loops back to contract {current}; "
                "it needs review before anything is linked beneath it."
            )
        visited.add(current)
        links = _active_parent_links(conn, tenant, current)
        if not links:
            return
        if len(links) > 1:
            raise DORAContractConflictError(
                f"contract {current}, above contract {parent}, has more than one active overarching "
                "arrangement recorded; it needs review before anything is linked beneath it."
            )
        current = links[0].parent_contract_id


def _insert_relationship(conn, tenant: str, child: str, parent: str) -> ContractRelationshipOutput:
    """Insert the active link and return it as stored; a second active link for the child is a conflict."""
    now = datetime.now(timezone.utc)
    row = conn.execute(
        _INSERT_RELATIONSHIP,
        {
            "contract_relationship_id": str(uuid.uuid4()),
            "tenant_id": tenant,
            "child_contract_id": child,
            "parent_contract_id": parent,
            "relationship_type": RELATIONSHIP_TYPE_OVERARCHING,
            "created_at": now,
            "updated_at": now,
        },
    ).fetchone()
    if row is None:
        raise DORAContractConflictError(
            f"contract {child} already has an active overarching arrangement; nothing was recorded."
        )
    return _row_to_output(row)


# ---------------------------------------------------------------------------
# Row mapping and ledger
# ---------------------------------------------------------------------------


def _active_parent_links(conn, tenant: str, child: str) -> list[ContractRelationshipOutput]:
    """Return the child's active overarching links (normally zero or one), whatever the contracts' own state."""
    rows = conn.execute(
        _SELECT_ACTIVE_PARENT_LINKS,
        {"tenant_id": tenant, "child_contract_id": child, "relationship_type": RELATIONSHIP_TYPE_OVERARCHING},
    ).fetchall()
    return [_row_to_output(row) for row in rows]


def _fetch_relationship(conn, sql: str, tenant: str, relationship_id: str) -> ContractRelationshipOutput | None:
    """Run one of the single-link SELECTs and return the link, or None."""
    row = conn.execute(sql, {"tenant_id": tenant, "contract_relationship_id": relationship_id}).fetchone()
    return _row_to_output(row) if row is not None else None


def _row_to_output(row) -> ContractRelationshipOutput:
    """Map a _RELATIONSHIP_COLUMNS row (by position) to ContractRelationshipOutput, ids canonical."""
    return ContractRelationshipOutput(
        contract_relationship_id=str(row[0]),
        tenant_id=str(row[1]),
        child_contract_id=str(row[2]),
        parent_contract_id=str(row[3]),
        relationship_type=row[4],
        is_active=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


def _to_ledger_state(relationship: ContractRelationshipOutput) -> dict:
    """Return a link as a dict the ledger can serialise, timestamps as UTC ISO 8601.

    Timestamps read back from PostgreSQL arrive in the session's zone; writing
    them as UTC makes a deactivation's before_state equal the creation
    entry's after_state field for field.
    """
    state = dataclasses.asdict(relationship)
    for field_name in ("created_at", "updated_at"):
        state[field_name] = state[field_name].astimezone(timezone.utc).isoformat()
    return state


def _ledger(
    conn, tenant: str, *, actor_id, actor_role: str, action_type: str,
    object_id: str, before_state: dict | None, after_state: dict,
) -> None:
    """Append one KER-107 entry on the caller's connection, after the write it records.

    control_id is None: a contract link is not a control assessment. Same
    transaction, same fate as the write.
    """
    append_audit_entry(
        conn,
        tenant,
        actor_id=actor_id,
        actor_role=actor_role,
        action_type=action_type,
        object_type=OBJECT_TYPE_CONTRACT_RELATIONSHIP,
        object_id=object_id,
        control_id=None,
        before_state=before_state,
        after_state=after_state,
    )
