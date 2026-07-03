"""KER-107 integration tests — override/ledger atomicity and the auditor query patterns.

Proves against a live PostgreSQL database (migration 016 applied) that capture_override
writes its ledger entry in the same transaction (commit and rollback together), and that
auditors can query the ledger by control, by actor, and by created_at range.
Run: pytest tests/integration/test_ker107_audit_ledger.py -m integration -v
"""

from __future__ import annotations

import uuid

import pytest

from src.services.audit_log import (
    append_audit_entry,
    get_entries_between,
    get_entries_by_actor,
    get_entries_by_control,
)
from src.services.override_service import OverrideInput, capture_override

_ACTOR_ONE = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_ACTOR_TWO = uuid.UUID("e0000000-0000-4000-e000-000000000005")


class _FakeAuthSession:
    def __init__(self, tenant_id: uuid.UUID) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> uuid.UUID:
        return self._tenant_id


class _DeliberateRollback(Exception):
    pass


def _make_override_input(control_id: str) -> OverrideInput:
    return OverrideInput(
        reviewer_id=_ACTOR_ONE,
        reviewer_role="vciso",
        action_type="approve",
        original_control_id=control_id,
        corrected_control_id=None,
        justification_text="Reviewed and confirmed.",
    )


def _append_entry(conn, tenant_id, control_id: str, actor_id):
    return append_audit_entry(
        conn,
        tenant_id,
        actor_id=actor_id,
        actor_role="vciso",
        action_type="approve",
        object_type="override",
        object_id=str(uuid.uuid4()),
        control_id=control_id,
        before_state={"control_id": control_id},
        after_state={"control_id": control_id, "justification_text": None},
    )


@pytest.mark.integration
def test_override_capture_writes_ledger_entry_in_same_transaction(db_connection, tenant_a_id):
    with db_connection.transaction():
        override = capture_override(
            _FakeAuthSession(tenant_a_id), db_connection,
            _make_override_input("ker107-atomic-commit"),
        )
    with db_connection.transaction():
        entries = get_entries_by_control(db_connection, tenant_a_id, "ker107-atomic-commit")
    assert len(entries) == 1
    assert entries[0].object_id == str(override.override_id)
    assert entries[0].actor_id == str(override.reviewer_id)


@pytest.mark.integration
def test_override_capture_rolls_back_ledger_entry_atomically(db_connection, tenant_a_id):
    captured: dict = {}
    with pytest.raises(_DeliberateRollback):
        with db_connection.transaction():
            override = capture_override(
                _FakeAuthSession(tenant_a_id), db_connection,
                _make_override_input("ker107-atomic-rollback"),
            )
            captured["override_id"] = str(override.override_id)
            raise _DeliberateRollback()
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        override_rows = db_connection.execute(
            "SELECT override_id FROM overrides WHERE override_id = %s",
            [captured["override_id"]],
        ).fetchall()
        audit_rows = db_connection.execute(
            "SELECT id FROM audit_log WHERE object_id = %s",
            [captured["override_id"]],
        ).fetchall()
    assert override_rows == [], "Override row must roll back with the failed transaction"
    assert audit_rows == [], "Ledger entry must roll back together with its override"


@pytest.mark.integration
def test_auditor_can_query_by_control(db_connection, tenant_a_id):
    with db_connection.transaction():
        _append_entry(db_connection, tenant_a_id, "ker107-q-ctrl-a", _ACTOR_ONE)
        _append_entry(db_connection, tenant_a_id, "ker107-q-ctrl-a", _ACTOR_ONE)
        _append_entry(db_connection, tenant_a_id, "ker107-q-ctrl-b", _ACTOR_ONE)
    with db_connection.transaction():
        entries = get_entries_by_control(db_connection, tenant_a_id, "ker107-q-ctrl-a")
    assert len(entries) == 2
    assert all(e.control_id == "ker107-q-ctrl-a" for e in entries)


@pytest.mark.integration
def test_auditor_can_query_by_actor(db_connection, tenant_a_id):
    with db_connection.transaction():
        _append_entry(db_connection, tenant_a_id, "ker107-q-actor", _ACTOR_ONE)
        _append_entry(db_connection, tenant_a_id, "ker107-q-actor", _ACTOR_ONE)
        _append_entry(db_connection, tenant_a_id, "ker107-q-actor", _ACTOR_TWO)
    with db_connection.transaction():
        entries = get_entries_by_actor(db_connection, tenant_a_id, _ACTOR_ONE)
    assert len(entries) == 2
    assert all(e.actor_id == str(_ACTOR_ONE) for e in entries)


@pytest.mark.integration
def test_auditor_can_query_by_time_range(db_connection, tenant_a_id):
    with db_connection.transaction():
        _append_entry(db_connection, tenant_a_id, "ker107-q-time-1", _ACTOR_ONE)
        middle = _append_entry(db_connection, tenant_a_id, "ker107-q-time-2", _ACTOR_ONE)
        _append_entry(db_connection, tenant_a_id, "ker107-q-time-3", _ACTOR_ONE)
    with db_connection.transaction():
        entries = get_entries_between(
            db_connection, tenant_a_id, middle.created_at, middle.created_at
        )
    assert len(entries) == 1
    assert entries[0].control_id == "ker107-q-time-2"
