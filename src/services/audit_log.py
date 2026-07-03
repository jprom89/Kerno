"""Tamper-evident audit ledger — appends hash-chained entries to audit_log and verifies chain integrity.

Every entry's entry_hash is SHA-256(previous_hash + canonical JSON payload), chained per tenant
from AUDIT_GENESIS_HASH; the append-only trigger from migration 016 blocks UPDATE/DELETE at the
database level, and verify_audit_chain() detects any edit, deletion, or reordering of past entries.
Run tests with: pytest tests/unit/services/test_audit_log.py -v
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from datetime import datetime, timezone

from config.constants import AUDIT_GENESIS_HASH
from src.db.rls import require_valid_tenant_uuid, set_tenant_context
from src.exceptions import TenantContextMissingError  # noqa: F401  re-exported

__all__ = [
    "AuditEntry",
    "ChainVerificationResult",
    "append_audit_entry",
    "build_canonical_payload",
    "compute_entry_hash",
    "get_entries_between",
    "get_entries_by_actor",
    "get_entries_by_control",
    "verify_audit_chain",
    "write_audit_event",
]

# ---------------------------------------------------------------------------
# SQL constants
#
# sequence_number is assigned by the database AFTER the hash is computed, so it
# is deliberately absent from the hashed payload. Ordering integrity does not
# depend on it: each entry's previous_hash must equal the prior entry's
# entry_hash, so any reordering, insertion, or deletion breaks a link.
# ---------------------------------------------------------------------------

_LEDGER_COLUMNS = (
    "id, tenant_id, actor_id, actor_role, action_type, object_type, object_id, "
    "control_id, before_state, after_state, created_at, previous_hash, "
    "entry_hash, sequence_number"
)

_SELECT_LATEST_HASH = """
SELECT entry_hash
FROM audit_log
WHERE tenant_id = :tenant_id
ORDER BY sequence_number DESC
LIMIT 1
"""

_INSERT_ENTRY = f"""
INSERT INTO audit_log
    (id, tenant_id, actor_id, actor_role, action_type, object_type, object_id,
     control_id, before_state, after_state, created_at, previous_hash, entry_hash)
VALUES
    (:id, :tenant_id, :actor_id, :actor_role, :action_type, :object_type, :object_id,
     :control_id, :before_state, :after_state, :created_at, :previous_hash, :entry_hash)
"""

_SELECT_CHAIN = f"""
SELECT {_LEDGER_COLUMNS}
FROM audit_log
WHERE tenant_id = :tenant_id
ORDER BY sequence_number ASC
"""

_SELECT_BY_CONTROL = f"""
SELECT {_LEDGER_COLUMNS}
FROM audit_log
WHERE tenant_id = :tenant_id
AND control_id = :control_id
ORDER BY sequence_number ASC
"""

_SELECT_BY_ACTOR = f"""
SELECT {_LEDGER_COLUMNS}
FROM audit_log
WHERE tenant_id = :tenant_id
AND actor_id = :actor_id
ORDER BY sequence_number ASC
"""

_SELECT_BETWEEN = f"""
SELECT {_LEDGER_COLUMNS}
FROM audit_log
WHERE tenant_id = :tenant_id
AND created_at >= :start_at
AND created_at <= :end_at
ORDER BY sequence_number ASC
"""

# Serializes appends per tenant: without this lock, two concurrent requests
# could both read the same latest entry_hash and write two entries with the
# same previous_hash, forking the chain and failing verification later.
# hashtextextended maps the tenant UUID string to a stable bigint lock key;
# the lock releases automatically when the transaction commits or rolls back.
_ACQUIRE_CHAIN_LOCK = """
SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))
"""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AuditEntry:
    """One immutable ledger entry. sequence_number is None on freshly appended
    entries because the database assigns it after the hash is computed."""

    id: str
    tenant_id: str
    actor_id: str | None
    actor_role: str
    action_type: str
    object_type: str
    object_id: str | None
    control_id: str | None
    before_state: dict | None
    after_state: dict | None
    created_at: datetime
    previous_hash: str
    entry_hash: str
    sequence_number: int | None = None


@dataclasses.dataclass(frozen=True)
class ChainVerificationResult:
    """Outcome of a full-chain integrity walk for one tenant."""

    is_valid: bool
    entry_count: int
    failure_reason: str | None
    failed_entry_id: str | None


# ---------------------------------------------------------------------------
# Hashing primitives — public so the migration backfill, external auditors,
# and tests can recompute hashes from stored column values.
# ---------------------------------------------------------------------------


def build_canonical_payload(
    entry_id,
    tenant_id,
    actor_id,
    actor_role: str,
    action_type: str,
    object_type: str,
    object_id: str | None,
    control_id: str | None,
    before_state: dict | None,
    after_state: dict | None,
    created_at: datetime,
) -> str:
    """Return the deterministic JSON string that gets hashed for one entry.

    Sorted keys and compact separators make the serialization independent of dict
    insertion order; UUIDs are stringified and created_at is rendered in UTC
    ISO-8601 — psycopg2 returns timestamptz in the session timezone, so without
    the UTC normalization a non-UTC connection would recompute a different string
    for the same instant and falsely report tampering. State dict values must be
    JSON-native strings, booleans, ints, or nulls: non-integer floats do not
    round-trip byte-identically through JSONB's numeric normalization.
    """
    return json.dumps(
        {
            "id": str(entry_id),
            "tenant_id": str(tenant_id),
            "actor_id": str(actor_id) if actor_id is not None else None,
            "actor_role": actor_role,
            "action_type": action_type,
            "object_type": object_type,
            "object_id": object_id,
            "control_id": control_id,
            "before_state": before_state,
            "after_state": after_state,
            "created_at": created_at.astimezone(timezone.utc).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_entry_hash(previous_hash: str, canonical_payload: str) -> str:
    """Return SHA-256 hex of previous_hash concatenated with the canonical payload."""
    return hashlib.sha256((previous_hash + canonical_payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_audit_entry(
    conn,
    tenant_id,
    *,
    actor_id,
    actor_role: str,
    action_type: str,
    object_type: str,
    object_id: str | None = None,
    control_id: str | None = None,
    before_state: dict | None = None,
    after_state: dict | None = None,
) -> AuditEntry:
    """Append one hash-chained entry to the tenant's audit ledger and return it.

    Sets tenant context, serializes appends per tenant via an advisory lock, links
    the new entry to the latest prior entry_hash (or AUDIT_GENESIS_HASH for the
    first entry), and inserts within the caller's open transaction so the entry
    commits or rolls back atomically with the business write it records.
    actor_id None means the event was system-generated, not a human decision.
    Raises TenantContextMissingError on invalid tenant, ValueError on blank fields.
    """
    _validate_required_fields(actor_role, action_type, object_type)
    # Identifiers are normalized to canonical lowercase UUID strings BEFORE
    # hashing and locking: PostgreSQL normalizes UUID columns on storage, so
    # hashing a caller-supplied variant (e.g. uppercase) would permanently break
    # verification against the stored row — and an un-normalized advisory-lock
    # key would let two spellings of one tenant bypass each other's lock.
    canonical_tenant_id = str(require_valid_tenant_uuid(tenant_id))
    canonical_actor_id = _normalize_actor_id(actor_id)
    set_tenant_context(conn, canonical_tenant_id)
    conn.execute(_ACQUIRE_CHAIN_LOCK, {"lock_key": canonical_tenant_id})
    previous_hash = _fetch_latest_entry_hash(conn, canonical_tenant_id)
    entry = _build_entry(
        canonical_tenant_id, canonical_actor_id, actor_role, action_type,
        object_type, object_id, control_id, before_state, after_state, previous_hash,
    )
    conn.execute(_INSERT_ENTRY, _entry_to_insert_params(entry))
    return entry


def write_audit_event(conn, tenant_id, event_type: str, event_data: dict) -> AuditEntry:
    """Append a system-generated event to the ledger (KER-105 compatibility wrapper).

    Kept so existing callers (mapping_service) keep working unchanged: the event
    payload is preserved in after_state, control_id is lifted out when present,
    and actor_id None with actor_role 'system' marks the entry as machine-generated.
    """
    event_data = event_data or {}
    return append_audit_entry(
        conn,
        tenant_id,
        actor_id=None,
        actor_role="system",
        action_type=event_type,
        object_type="system_event",
        object_id=None,
        control_id=event_data.get("control_id"),
        before_state=None,
        after_state=event_data,
    )


def verify_audit_chain(conn, tenant_id) -> ChainVerificationResult:
    """Walk the tenant's full ledger in sequence order and verify every hash link.

    Fails on a broken link (previous_hash not matching the prior entry_hash —
    which catches deletion, insertion, and reordering) and on content tampering
    (recomputed entry_hash not matching the stored one). Deleting the newest
    entry is the one edit the chain alone cannot see; the database append-only
    trigger exists to block exactly that. An empty ledger is valid.
    """
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(_SELECT_CHAIN, {"tenant_id": str(tenant_id)}).fetchall()
    expected_previous = AUDIT_GENESIS_HASH
    for row in rows:
        entry = _row_to_entry(row)
        failure = _verify_single_entry(entry, expected_previous)
        if failure is not None:
            return ChainVerificationResult(
                is_valid=False,
                entry_count=len(rows),
                failure_reason=failure,
                failed_entry_id=entry.id,
            )
        expected_previous = entry.entry_hash
    return ChainVerificationResult(
        is_valid=True, entry_count=len(rows), failure_reason=None, failed_entry_id=None
    )


def get_entries_by_control(conn, tenant_id, control_id: str) -> list[AuditEntry]:
    """Return the tenant's ledger entries for one control, oldest first."""
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(
        _SELECT_BY_CONTROL, {"tenant_id": str(tenant_id), "control_id": control_id}
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def get_entries_by_actor(conn, tenant_id, actor_id) -> list[AuditEntry]:
    """Return the tenant's ledger entries recorded by one actor, oldest first."""
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(
        _SELECT_BY_ACTOR, {"tenant_id": str(tenant_id), "actor_id": str(actor_id)}
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def get_entries_between(conn, tenant_id, start_at: datetime, end_at: datetime) -> list[AuditEntry]:
    """Return the tenant's ledger entries with created_at in [start_at, end_at], oldest first."""
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(
        _SELECT_BETWEEN,
        {"tenant_id": str(tenant_id), "start_at": start_at, "end_at": end_at},
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _normalize_actor_id(actor_id) -> str | None:
    if actor_id is None:
        return None
    return str(uuid.UUID(str(actor_id)))


def _build_entry(
    tenant_id: str,
    actor_id: str | None,
    actor_role: str,
    action_type: str,
    object_type: str,
    object_id: str | None,
    control_id: str | None,
    before_state: dict | None,
    after_state: dict | None,
    previous_hash: str,
) -> AuditEntry:
    entry_id = uuid.uuid4()
    created_at = datetime.now(timezone.utc)
    canonical_payload = build_canonical_payload(
        entry_id, tenant_id, actor_id, actor_role, action_type,
        object_type, object_id, control_id, before_state, after_state, created_at,
    )
    return AuditEntry(
        id=str(entry_id),
        tenant_id=tenant_id,
        actor_id=actor_id,
        actor_role=actor_role,
        action_type=action_type,
        object_type=object_type,
        object_id=object_id,
        control_id=control_id,
        before_state=before_state,
        after_state=after_state,
        created_at=created_at,
        previous_hash=previous_hash,
        entry_hash=compute_entry_hash(previous_hash, canonical_payload),
    )


def _validate_required_fields(actor_role: str, action_type: str, object_type: str) -> None:
    if not actor_role or not actor_role.strip():
        raise ValueError("actor_role must not be empty.")
    if not action_type or not action_type.strip():
        raise ValueError("action_type must not be empty.")
    if not object_type or not object_type.strip():
        raise ValueError("object_type must not be empty.")


def _entry_to_insert_params(entry: AuditEntry) -> dict:
    return {
        "id": entry.id,
        "tenant_id": entry.tenant_id,
        "actor_id": entry.actor_id,
        "actor_role": entry.actor_role,
        "action_type": entry.action_type,
        "object_type": entry.object_type,
        "object_id": entry.object_id,
        "control_id": entry.control_id,
        "before_state": json.dumps(entry.before_state) if entry.before_state is not None else None,
        "after_state": json.dumps(entry.after_state) if entry.after_state is not None else None,
        "created_at": entry.created_at,
        "previous_hash": entry.previous_hash,
        "entry_hash": entry.entry_hash,
    }


def _fetch_latest_entry_hash(conn, tenant_id) -> str:
    row = conn.execute(_SELECT_LATEST_HASH, {"tenant_id": str(tenant_id)}).fetchone()
    if row is None:
        return AUDIT_GENESIS_HASH
    return row[0]


def _verify_single_entry(entry: AuditEntry, expected_previous: str) -> str | None:
    if entry.previous_hash != expected_previous:
        return (
            f"chain link broken: previous_hash {entry.previous_hash!r} does not match "
            f"the prior entry's hash {expected_previous!r} (entry deleted, inserted, or reordered)"
        )
    recomputed = compute_entry_hash(
        entry.previous_hash,
        build_canonical_payload(
            entry.id, entry.tenant_id, entry.actor_id, entry.actor_role,
            entry.action_type, entry.object_type, entry.object_id, entry.control_id,
            entry.before_state, entry.after_state, entry.created_at,
        ),
    )
    if recomputed != entry.entry_hash:
        return "entry content does not match its stored hash (row was modified after append)"
    return None


def _row_to_entry(row) -> AuditEntry:
    return AuditEntry(
        id=str(row[0]),
        tenant_id=str(row[1]),
        actor_id=str(row[2]) if row[2] is not None else None,
        actor_role=row[3],
        action_type=row[4],
        object_type=row[5],
        object_id=row[6],
        control_id=row[7],
        before_state=row[8],
        after_state=row[9],
        created_at=row[10],
        previous_hash=row[11],
        entry_hash=row[12],
        sequence_number=row[13],
    )
