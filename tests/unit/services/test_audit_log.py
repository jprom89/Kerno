"""Unit tests for src/services/audit_log.py — the tamper-evident hash-chained ledger.

Twenty-two tests cover genesis linking, hash computation (including an independent
SHA-256 recomputation), identifier and timezone normalization of the hashed payload,
per-tenant chain locking, append-only behaviour at the SQL level, canonical serialization
determinism, chain verification against tampering / deletion / reordering, the
system-event compatibility wrapper, and the three auditor query patterns.
All tests use spy connections; no database is required.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from config.constants import AUDIT_GENESIS_HASH
from src.exceptions import TenantContextMissingError
from src.services.audit_log import (
    append_audit_entry,
    build_canonical_payload,
    compute_entry_hash,
    get_entries_between,
    get_entries_by_actor,
    get_entries_by_control,
    verify_audit_chain,
    write_audit_event,
)

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_ACTOR_ID = "d0000000-0000-4000-d000-000000000004"
_CREATED_AT = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


# ── Test infrastructure ───────────────────────────────────────────────────────


class _NullResult:
    def fetchone(self):
        return None

    def fetchall(self) -> list:
        return []


class _RowsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _LedgerSpyConn:
    """Records execute() calls; serves the latest-hash row and canned chain rows."""

    def __init__(self, latest_hash: str | None = None, chain_rows: list | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._latest_hash = latest_hash
        self._chain_rows = chain_rows or []

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "SELECT entry_hash" in sql:
            return _RowsResult([(self._latest_hash,)]) if self._latest_hash else _NullResult()
        if "ORDER BY sequence_number ASC" in sql:
            return _RowsResult(self._chain_rows)
        return _NullResult()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _append_valid_entry(spy) -> object:
    return append_audit_entry(
        spy,
        _TENANT_ID,
        actor_id=_ACTOR_ID,
        actor_role="vciso",
        action_type="approve",
        object_type="override",
        object_id="ov-001",
        control_id="ctrl-001",
        before_state={"control_id": "ctrl-001"},
        after_state={"control_id": "ctrl-001", "justification_text": None},
    )


def _audit_insert_params(spy) -> dict:
    return next(p for s, p in spy.calls if "INSERT INTO audit_log" in s)


def _make_chain_rows(count: int, tenant_id=_TENANT_ID) -> list[tuple]:
    # Rows mirror _LEDGER_COLUMNS order: id, tenant_id, actor_id, actor_role,
    # action_type, object_type, object_id, control_id, before_state, after_state,
    # created_at, previous_hash, entry_hash, sequence_number.
    rows: list[tuple] = []
    previous_hash = AUDIT_GENESIS_HASH
    for index in range(count):
        entry_id = str(uuid.UUID(int=index + 1))
        created_at = _CREATED_AT + timedelta(seconds=index)
        before_state = {"control_id": f"ctrl-{index}"}
        after_state = {"control_id": f"ctrl-{index}-new", "justification_text": None}
        payload = build_canonical_payload(
            entry_id, str(tenant_id), _ACTOR_ID, "vciso", "edit", "override",
            f"ov-{index}", f"ctrl-{index}", before_state, after_state, created_at,
        )
        entry_hash = compute_entry_hash(previous_hash, payload)
        rows.append(
            (entry_id, str(tenant_id), _ACTOR_ID, "vciso", "edit", "override",
             f"ov-{index}", f"ctrl-{index}", before_state, after_state, created_at,
             previous_hash, entry_hash, index + 1)
        )
        previous_hash = entry_hash
    return rows


# ── Append: genesis, chaining, hashing ────────────────────────────────────────


def test_first_entry_previous_hash_is_genesis():
    spy = _LedgerSpyConn(latest_hash=None)
    entry = _append_valid_entry(spy)
    params = _audit_insert_params(spy)
    assert params["previous_hash"] == AUDIT_GENESIS_HASH
    assert entry.previous_hash == AUDIT_GENESIS_HASH


def test_second_entry_chains_to_latest_stored_hash():
    latest = "a" * 64
    spy = _LedgerSpyConn(latest_hash=latest)
    entry = _append_valid_entry(spy)
    assert entry.previous_hash == latest
    assert _audit_insert_params(spy)["previous_hash"] == latest


def test_entry_hash_recomputable_from_returned_entry():
    spy = _LedgerSpyConn()
    entry = _append_valid_entry(spy)
    recomputed = compute_entry_hash(
        entry.previous_hash,
        build_canonical_payload(
            entry.id, entry.tenant_id, entry.actor_id, entry.actor_role,
            entry.action_type, entry.object_type, entry.object_id, entry.control_id,
            entry.before_state, entry.after_state, entry.created_at,
        ),
    )
    assert recomputed == entry.entry_hash
    assert _audit_insert_params(spy)["entry_hash"] == entry.entry_hash


def test_entry_hash_matches_independent_sha256():
    spy = _LedgerSpyConn()
    entry = _append_valid_entry(spy)
    payload = json.dumps(
        {
            "id": entry.id,
            "tenant_id": str(_TENANT_ID),
            "actor_id": _ACTOR_ID,
            "actor_role": "vciso",
            "action_type": "approve",
            "object_type": "override",
            "object_id": "ov-001",
            "control_id": "ctrl-001",
            "before_state": {"control_id": "ctrl-001"},
            "after_state": {"control_id": "ctrl-001", "justification_text": None},
            "created_at": entry.created_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = hashlib.sha256((AUDIT_GENESIS_HASH + payload).encode("utf-8")).hexdigest()
    assert entry.entry_hash == expected


def test_uppercase_identifiers_normalized_before_hashing_and_locking():
    spy = _LedgerSpyConn()
    append_audit_entry(
        spy,
        str(_TENANT_ID).upper(),
        actor_id=_ACTOR_ID.upper(),
        actor_role="vciso",
        action_type="approve",
        object_type="override",
    )
    params = _audit_insert_params(spy)
    assert params["tenant_id"] == str(_TENANT_ID)
    assert params["actor_id"] == _ACTOR_ID
    lock_params = next(p for s, p in spy.calls if "pg_advisory_xact_lock" in s)
    assert lock_params["lock_key"] == str(_TENANT_ID)


def test_canonical_payload_renders_created_at_in_utc():
    same_instant_plus_two = datetime(2026, 7, 3, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    same_instant_utc = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)
    payload_plus_two = build_canonical_payload(
        "id-1", "t-1", None, "system", "event", "system_event",
        None, None, None, None, same_instant_plus_two,
    )
    payload_utc = build_canonical_payload(
        "id-1", "t-1", None, "system", "event", "system_event",
        None, None, None, None, same_instant_utc,
    )
    assert payload_plus_two == payload_utc


def test_canonical_payload_ignores_state_dict_insertion_order():
    state_one = {"control_id": "ctrl-1", "justification_text": "x"}
    state_two = {"justification_text": "x", "control_id": "ctrl-1"}
    payload_one = build_canonical_payload(
        "id-1", "t-1", None, "system", "event", "system_event",
        None, None, None, state_one, _CREATED_AT,
    )
    payload_two = build_canonical_payload(
        "id-1", "t-1", None, "system", "event", "system_event",
        None, None, None, state_two, _CREATED_AT,
    )
    assert payload_one == payload_two


# ── Append: isolation, locking, append-only SQL ───────────────────────────────


def test_append_sets_tenant_context_before_any_other_sql():
    spy = _LedgerSpyConn()
    _append_valid_entry(spy)
    assert "SET LOCAL" in spy.calls[0][0]


def test_append_with_none_tenant_raises_before_sql():
    spy = _LedgerSpyConn()
    with pytest.raises(TenantContextMissingError):
        append_audit_entry(
            spy, None, actor_id=None, actor_role="system",
            action_type="event", object_type="system_event",
        )
    assert len(spy.calls) == 0


def test_append_acquires_chain_lock_before_reading_latest_hash():
    spy = _LedgerSpyConn()
    _append_valid_entry(spy)
    lock_index = next(i for i, (s, _) in enumerate(spy.calls) if "pg_advisory_xact_lock" in s)
    latest_index = next(i for i, (s, _) in enumerate(spy.calls) if "SELECT entry_hash" in s)
    assert lock_index < latest_index


def test_append_and_verify_issue_no_update_or_delete():
    spy = _LedgerSpyConn(latest_hash="b" * 64)
    _append_valid_entry(spy)
    verify_audit_chain(spy, _TENANT_ID)
    for sql, _ in spy.calls:
        normalized = " ".join(sql.upper().split())
        assert "UPDATE AUDIT_LOG" not in normalized
        assert "DELETE FROM AUDIT_LOG" not in normalized


def test_append_rejects_blank_action_type():
    spy = _LedgerSpyConn()
    with pytest.raises(ValueError, match="action_type"):
        append_audit_entry(
            spy, _TENANT_ID, actor_id=None, actor_role="system",
            action_type="  ", object_type="system_event",
        )
    assert len(spy.calls) == 0


# ── System-event wrapper (KER-105 compatibility) ──────────────────────────────


def test_write_audit_event_appends_system_entry():
    spy = _LedgerSpyConn()
    write_audit_event(
        spy, _TENANT_ID, "recommendation_generated",
        {"recommendation_id": "rec-1", "control_id": "ctrl-9", "status": "met"},
    )
    params = _audit_insert_params(spy)
    assert params["actor_id"] is None
    assert params["actor_role"] == "system"
    assert params["action_type"] == "recommendation_generated"
    assert params["control_id"] == "ctrl-9"
    assert json.loads(params["after_state"])["recommendation_id"] == "rec-1"


# ── Chain verification ────────────────────────────────────────────────────────


def test_verify_empty_chain_is_valid():
    spy = _LedgerSpyConn(chain_rows=[])
    result = verify_audit_chain(spy, _TENANT_ID)
    assert result.is_valid is True
    assert result.entry_count == 0


def test_verify_valid_chain_passes():
    spy = _LedgerSpyConn(chain_rows=_make_chain_rows(3))
    result = verify_audit_chain(spy, _TENANT_ID)
    assert result.is_valid is True
    assert result.entry_count == 3
    assert result.failure_reason is None


def test_verify_detects_content_tampering():
    rows = _make_chain_rows(3)
    tampered = list(rows[1])
    tampered[4] = "reject"  # action_type edited after the fact
    rows[1] = tuple(tampered)
    result = verify_audit_chain(_LedgerSpyConn(chain_rows=rows), _TENANT_ID)
    assert result.is_valid is False
    assert "does not match its stored hash" in result.failure_reason
    assert result.failed_entry_id == rows[1][0]


def test_verify_detects_deleted_middle_entry():
    rows = _make_chain_rows(3)
    del rows[1]
    result = verify_audit_chain(_LedgerSpyConn(chain_rows=rows), _TENANT_ID)
    assert result.is_valid is False
    assert "chain link broken" in result.failure_reason


def test_verify_detects_reordered_entries():
    rows = _make_chain_rows(3)
    rows[0], rows[1] = rows[1], rows[0]
    result = verify_audit_chain(_LedgerSpyConn(chain_rows=rows), _TENANT_ID)
    assert result.is_valid is False
    assert "chain link broken" in result.failure_reason


def test_verify_detects_forged_genesis():
    rows = _make_chain_rows(2)
    forged_first = list(rows[0])
    forged_first[11] = "f" * 64  # previous_hash no longer the genesis constant
    rows[0] = tuple(forged_first)
    result = verify_audit_chain(_LedgerSpyConn(chain_rows=rows), _TENANT_ID)
    assert result.is_valid is False
    assert "chain link broken" in result.failure_reason


# ── Auditor query patterns ────────────────────────────────────────────────────


def test_get_entries_by_control_filters_and_maps_rows():
    spy = _LedgerSpyConn(chain_rows=_make_chain_rows(2))
    entries = get_entries_by_control(spy, _TENANT_ID, "ctrl-0")
    sql, params = next((s, p) for s, p in spy.calls if "control_id = :control_id" in s)
    assert params["control_id"] == "ctrl-0"
    assert params["tenant_id"] == str(_TENANT_ID)
    assert len(entries) == 2
    assert entries[0].sequence_number == 1
    assert entries[0].previous_hash == AUDIT_GENESIS_HASH


def test_get_entries_by_actor_filters_by_actor():
    spy = _LedgerSpyConn()
    get_entries_by_actor(spy, _TENANT_ID, _ACTOR_ID)
    sql, params = next((s, p) for s, p in spy.calls if "actor_id = :actor_id" in s)
    assert params["actor_id"] == _ACTOR_ID


def test_get_entries_between_uses_inclusive_bounds():
    spy = _LedgerSpyConn()
    start_at = _CREATED_AT
    end_at = _CREATED_AT + timedelta(hours=1)
    get_entries_between(spy, _TENANT_ID, start_at, end_at)
    sql, params = next((s, p) for s, p in spy.calls if "created_at >= :start_at" in s)
    assert "created_at <= :end_at" in sql
    assert params["start_at"] == start_at
    assert params["end_at"] == end_at
