"""KER-107 — Append-only and tamper-evidence security tests for the audit_log ledger.

Unit tests prove the service layer exposes no mutation path; integration tests
(@pytest.mark.integration, live DB with migration 016 applied) prove the database
triggers reject UPDATE, DELETE, and TRUNCATE, that the UNIQUE constraint rejects
chain forks, and that verification detects tampering even when a privileged
attacker disables the trigger first.
Run: pytest tests/security/test_audit_append_only.py -m "not integration" -v
"""

from __future__ import annotations

import inspect
import uuid

import pytest

import src.services.audit_log as audit_log_module
from src.services.audit_log import append_audit_entry, verify_audit_chain

_ACTOR_ID = uuid.UUID("d0000000-0000-4000-d000-000000000004")


def _append_entry(conn, tenant_id, control_id: str = "ker107-ctrl", actor_id=_ACTOR_ID):
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


# ── Unit: the service layer has no mutation path ──────────────────────────────


def test_audit_service_source_contains_no_mutation_sql() -> None:
    source = inspect.getsource(audit_log_module)
    normalized = " ".join(source.upper().split())
    assert "UPDATE AUDIT_LOG" not in normalized
    assert "DELETE FROM AUDIT_LOG" not in normalized


def test_audit_service_exposes_no_update_or_delete_functions() -> None:
    public_names = [name for name in dir(audit_log_module) if not name.startswith("_")]
    for name in public_names:
        assert "update" not in name.lower()
        assert "delete" not in name.lower()


# ── Integration: database-level enforcement and tamper detection ──────────────


@pytest.mark.integration
def test_update_rejected_by_append_only_trigger(db_connection, tenant_a_id):
    with db_connection.transaction():
        entry = _append_entry(db_connection, tenant_a_id)
    # FORCE RLS (migration 018) hides rows without a tenant context; the row
    # must be visible for the row-level trigger to fire at all.
    with pytest.raises(Exception, match="append-only"):
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(
                "UPDATE audit_log SET action_type = 'edit' WHERE id = %s", [entry.id]
            )


@pytest.mark.integration
def test_delete_rejected_by_append_only_trigger(db_connection, tenant_a_id):
    with db_connection.transaction():
        entry = _append_entry(db_connection, tenant_a_id)
    with pytest.raises(Exception, match="append-only"):
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute("DELETE FROM audit_log WHERE id = %s", [entry.id])


@pytest.mark.integration
def test_truncate_rejected_by_append_only_trigger(db_connection, tenant_a_id):
    with db_connection.transaction():
        _append_entry(db_connection, tenant_a_id)
    with pytest.raises(Exception, match="append-only"):
        with db_connection.transaction():
            db_connection.execute("TRUNCATE audit_log")


@pytest.mark.integration
def test_chain_fork_rejected_by_unique_constraint(db_connection, tenant_a_id):
    # Two entries claiming the same parent hash = a forked chain. The
    # UNIQUE (tenant_id, previous_hash) constraint must reject the second
    # regardless of application-level locking.
    with db_connection.transaction():
        entry = _append_entry(db_connection, tenant_a_id, control_id="ker107-fork")
    with pytest.raises(Exception, match="duplicate key"):
        with db_connection.transaction():
            db_connection.execute(
                "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
            )
            db_connection.execute(
                "INSERT INTO audit_log (id, tenant_id, actor_role, action_type, "
                "object_type, created_at, previous_hash, entry_hash) "
                "VALUES (%s, %s, 'system', 'forged_fork', 'system_event', now(), %s, %s)",
                [str(uuid.uuid4()), str(tenant_a_id), entry.previous_hash, "f" * 64],
            )


@pytest.mark.integration
def test_chain_valid_across_multiple_appends(db_connection, tenant_a_id):
    with db_connection.transaction():
        first = _append_entry(db_connection, tenant_a_id, control_id="ker107-chain-1")
        second = _append_entry(db_connection, tenant_a_id, control_id="ker107-chain-2")
        third = _append_entry(db_connection, tenant_a_id, control_id="ker107-chain-3")
    assert second.previous_hash == first.entry_hash
    assert third.previous_hash == second.entry_hash
    with db_connection.transaction():
        result = verify_audit_chain(db_connection, tenant_a_id)
    assert result.is_valid is True
    assert result.entry_count >= 3


@pytest.mark.integration
def test_tampering_after_trigger_bypass_breaks_verification(db_connection, tenant_a_id):
    # A table owner can disable the trigger — the hash chain is the layer that
    # still catches the edit afterwards. This test plays that attacker, who
    # also sets the tenant context so FORCE RLS shows them the target row.
    with db_connection.transaction():
        entry = _append_entry(db_connection, tenant_a_id, control_id="ker107-tamper")
        _append_entry(db_connection, tenant_a_id, control_id="ker107-tamper-2")
    with db_connection.transaction():
        db_connection.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only")
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        db_connection.execute(
            "UPDATE audit_log SET action_type = 'reject' WHERE id = %s", [entry.id]
        )
        db_connection.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_log_append_only")
    with db_connection.transaction():
        result = verify_audit_chain(db_connection, tenant_a_id)
    assert result.is_valid is False
    assert result.failed_entry_id == entry.id
    assert "does not match its stored hash" in result.failure_reason
