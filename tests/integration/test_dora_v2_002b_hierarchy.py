"""DORA-V2-002B — recorded contract hierarchy against the live database: behaviour, lifecycle, audit, transactions.

What:  drives dora_contract_hierarchy_service on the approved test database
       and proves: one parent with several children and a three-level chain;
       a contract with no recorded parent stays unclassified; self-links,
       two-node and longer cycles and a second active parent are refused by
       the service and, where the database can say so, by the constraints too;
       deactivation keeps history, relinking creates a new row, and a failed
       replacement rolled back restores the original link; contract state
       never hides an edge; the ledger records accurate before/after states,
       rolls back with the business write and records nothing for refusals or
       repeats; and the write paths refuse REPEATABLE READ, SERIALIZABLE and
       autocommit before writing anything.
Why:   The claims in the service docstring hold only if PostgreSQL agrees.
How:   pytest tests/integration/test_dora_v2_002b_hierarchy.py -m integration -v
"""

from __future__ import annotations

import os
import uuid
from datetime import timezone

import psycopg2
import psycopg2.errors
import pytest

from src.exceptions import (
    DORAContractConflictError,
    EntryNotFoundError,
    UnsupportedTransactionIsolationError,
)
from src.services.audit_log import (
    acquire_tenant_ledger_lock,
    get_entries_by_actor,
    tenant_ledger_lock_is_held,
    verify_audit_chain,
)
from src.services.dora_contract_hierarchy_service import (
    ACTION_CONTRACT_RELATIONSHIP_CREATED,
    ACTION_CONTRACT_RELATIONSHIP_DEACTIVATED,
    OBJECT_TYPE_CONTRACT_RELATIONSHIP,
    add_contract_relationship,
    deactivate_contract_relationship,
    get_contract_relationship,
    get_recorded_parent,
    list_contract_relationship_history,
    list_recorded_children,
)
from src.services.dora_contract_service import ContractInput, ContractUpdate, create_contract, get_contract, update_contract
from tests.conftest import _DbConnection

_ACTOR = uuid.UUID("d0000000-0000-4000-d000-000000000006")
_ROLE = "compliance_lead"
_CONTRACT_COLUMNS = {
    "contract_id", "tenant_id", "contract_reference", "display_name", "contract_start_date",
    "contract_end_date", "is_active", "created_at", "updated_at",
}


def _contracts(conn, tenant_id, *references: str) -> list[str]:
    """Create and commit one contract per reference; return their ids in order."""
    with conn.transaction():
        return [
            create_contract(conn, tenant_id, ContractInput(contract_reference=reference), actor_id=_ACTOR, actor_role=_ROLE).contract_id
            for reference in references
        ]


def _link(conn, tenant_id, child: str, parent: str):
    """Add one link and commit it."""
    with conn.transaction():
        return add_contract_relationship(conn, tenant_id, child, parent, actor_id=_ACTOR, actor_role=_ROLE)


def _unlink(conn, tenant_id, relationship_id: str):
    """Deactivate one link and commit."""
    with conn.transaction():
        return deactivate_contract_relationship(conn, tenant_id, relationship_id, actor_id=_ACTOR, actor_role=_ROLE)


def _refused(conn, tenant_id, child: str, parent: str, error):
    """Attempt a link that must be refused; roll back and return the error."""
    with pytest.raises(error) as caught:
        with conn.transaction():
            add_contract_relationship(conn, tenant_id, child, parent, actor_id=_ACTOR, actor_role=_ROLE)
    return caught.value


def _parent_of(conn, tenant_id, child: str) -> str | None:
    """Return the child's recorded parent id, or None."""
    with conn.transaction():
        link = get_recorded_parent(conn, tenant_id, child)
    return link.parent_contract_id if link else None


def _relationship_entries(conn, tenant_id) -> list:
    """Return this file's committed ledger entries for relationships, oldest first."""
    with conn.transaction():
        return [e for e in get_entries_by_actor(conn, tenant_id, _ACTOR) if e.object_type == OBJECT_TYPE_CONTRACT_RELATIONSHIP]


def _row_count(conn, tenant_id) -> int:
    """Count the tenant's relationship rows, active and inactive."""
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return conn.execute(
            "SELECT count(*) FROM dora_contract_relationships WHERE tenant_id = %s", [str(tenant_id)]
        ).fetchone()[0]


def _chain_is_valid(conn, tenant_id) -> bool:
    """Verify the tenant's whole hash chain separately from any content assertion."""
    with conn.transaction():
        result = verify_audit_chain(conn, tenant_id)
    assert result.is_valid, result.failure_reason
    return True


def _set_active(conn, tenant_id, contract_id: str, is_active: bool) -> None:
    """Change only a contract's own is_active flag, committed."""
    with conn.transaction():
        update_contract(
            conn, tenant_id, contract_id,
            ContractUpdate(display_name=None, contract_start_date=None, contract_end_date=None, is_active=is_active),
            actor_id=_ACTOR, actor_role=_ROLE,
        )


def _raw_insert(conn, tenant_id, child: str, parent: str, relationship_type: str = "overarching") -> None:
    """Insert one link with raw SQL under the tenant's context, bypassing the service."""
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
    conn.execute(
        "INSERT INTO dora_contract_relationships (tenant_id, child_contract_id, parent_contract_id, relationship_type) "
        "VALUES (%s, %s, %s, %s)",
        [str(tenant_id), child, parent, relationship_type],
    )


# ── Shapes the hierarchy supports ───────────────────────────────────────────


@pytest.mark.integration
def test_one_parent_records_several_children(db_connection, tenant_a_id):
    msa, hosting, support = _contracts(db_connection, tenant_a_id, "MSA", "HOSTING-2026", "SUPPORT-2026")
    _link(db_connection, tenant_a_id, hosting, msa)
    _link(db_connection, tenant_a_id, support, msa)
    with db_connection.transaction():
        children = list_recorded_children(db_connection, tenant_a_id, msa)
    assert [c.child_contract_id for c in children] == [hosting, support]
    assert _parent_of(db_connection, tenant_a_id, hosting) == msa == _parent_of(db_connection, tenant_a_id, support)
    assert _parent_of(db_connection, tenant_a_id, msa) is None


@pytest.mark.integration
def test_a_three_level_hierarchy_is_recorded_without_classifying_the_middle(db_connection, tenant_a_id):
    msa, schedule, order = _contracts(db_connection, tenant_a_id, "MSA", "SCHEDULE-1", "ORDER-1")
    _link(db_connection, tenant_a_id, schedule, msa)
    _link(db_connection, tenant_a_id, order, schedule)
    assert _parent_of(db_connection, tenant_a_id, order) == schedule
    assert _parent_of(db_connection, tenant_a_id, schedule) == msa
    with db_connection.transaction():
        columns = {row[0] for row in db_connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'dora_contracts'"
        ).fetchall()}
    assert columns == _CONTRACT_COLUMNS


@pytest.mark.integration
def test_a_contract_with_no_recorded_parent_stays_unclassified(db_connection, tenant_a_id):
    [lonely] = _contracts(db_connection, tenant_a_id, "LONELY-1")
    with db_connection.transaction():
        assert get_recorded_parent(db_connection, tenant_a_id, lonely) is None
        assert list_contract_relationship_history(db_connection, tenant_a_id, lonely) == []
        assert list_recorded_children(db_connection, tenant_a_id, lonely) == []
        contract = get_contract(db_connection, tenant_a_id, lonely)
    assert set(contract.__dataclass_fields__) == _CONTRACT_COLUMNS
    assert _relationship_entries(db_connection, tenant_a_id) == []


# ── Refusals ────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_a_direct_self_link_is_refused_by_the_service_and_by_the_check(db_connection, tenant_a_id):
    [msa] = _contracts(db_connection, tenant_a_id, "MSA")
    _refused(db_connection, tenant_a_id, msa, msa, ValueError)
    with pytest.raises(psycopg2.errors.CheckViolation, match="ck_dora_contract_relationships_not_self"):
        with db_connection.transaction():
            _raw_insert(db_connection, tenant_a_id, msa, msa)
    assert _row_count(db_connection, tenant_a_id) == 0


@pytest.mark.integration
def test_the_type_check_admits_only_overarching(db_connection, tenant_a_id):
    msa, order = _contracts(db_connection, tenant_a_id, "MSA", "ORDER-1")
    with pytest.raises(psycopg2.errors.CheckViolation, match="ck_dora_contract_relationships_type"):
        with db_connection.transaction():
            _raw_insert(db_connection, tenant_a_id, order, msa, relationship_type="subsequent")


@pytest.mark.integration
def test_two_node_and_longer_cycles_are_refused_and_write_nothing(db_connection, tenant_a_id):
    contract_a, contract_b, contract_c = _contracts(db_connection, tenant_a_id, "A", "B", "C")
    _link(db_connection, tenant_a_id, contract_a, contract_b)
    _refused(db_connection, tenant_a_id, contract_b, contract_a, DORAContractConflictError)
    _link(db_connection, tenant_a_id, contract_b, contract_c)
    error = _refused(db_connection, tenant_a_id, contract_c, contract_a, DORAContractConflictError)
    assert "cycle" in str(error)
    assert _row_count(db_connection, tenant_a_id) == 2
    created = [e for e in _relationship_entries(db_connection, tenant_a_id) if e.action_type == ACTION_CONTRACT_RELATIONSHIP_CREATED]
    assert [(e.after_state["child_contract_id"], e.after_state["parent_contract_id"]) for e in created] == [(contract_a, contract_b), (contract_b, contract_c)]


@pytest.mark.integration
def test_a_second_active_parent_is_refused_by_the_service_and_by_the_partial_index(db_connection, tenant_a_id):
    order, msa_one, msa_two = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA-1", "MSA-2")
    _link(db_connection, tenant_a_id, order, msa_one)
    _refused(db_connection, tenant_a_id, order, msa_two, DORAContractConflictError)
    _refused(db_connection, tenant_a_id, order, msa_one, DORAContractConflictError)
    with pytest.raises(psycopg2.errors.UniqueViolation, match="uq_dora_contract_relationships_one_active_parent"):
        with db_connection.transaction():
            _raw_insert(db_connection, tenant_a_id, order, msa_two)
    assert _parent_of(db_connection, tenant_a_id, order) == msa_one


@pytest.mark.integration
def test_an_unknown_endpoint_is_not_found_and_nothing_is_written(db_connection, tenant_a_id):
    [msa] = _contracts(db_connection, tenant_a_id, "MSA")
    _refused(db_connection, tenant_a_id, str(uuid.uuid4()), msa, EntryNotFoundError)
    _refused(db_connection, tenant_a_id, msa, str(uuid.uuid4()), EntryNotFoundError)
    assert _row_count(db_connection, tenant_a_id) == 0
    assert _relationship_entries(db_connection, tenant_a_id) == []


# ── Lifecycle ───────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_deactivation_keeps_history_and_relinking_creates_a_new_row(db_connection, tenant_a_id):
    order, msa_one, msa_two = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA-1", "MSA-2")
    first = _link(db_connection, tenant_a_id, order, msa_one)
    outcome = _unlink(db_connection, tenant_a_id, first.contract_relationship_id)
    assert outcome.changed is True and outcome.relationship.is_active is False
    assert _parent_of(db_connection, tenant_a_id, order) is None
    second = _link(db_connection, tenant_a_id, order, msa_one)
    assert second.contract_relationship_id != first.contract_relationship_id
    _unlink(db_connection, tenant_a_id, second.contract_relationship_id)
    third = _link(db_connection, tenant_a_id, order, msa_two)
    with db_connection.transaction():
        history = list_contract_relationship_history(db_connection, tenant_a_id, order)
        old = get_contract_relationship(db_connection, tenant_a_id, first.contract_relationship_id)
    assert [(h.contract_relationship_id, h.parent_contract_id, h.is_active) for h in history] == [
        (first.contract_relationship_id, msa_one, False),
        (second.contract_relationship_id, msa_one, False),
        (third.contract_relationship_id, msa_two, True),
    ]
    assert (old.child_contract_id, old.parent_contract_id, old.relationship_type, old.created_at) == (
        first.child_contract_id, first.parent_contract_id, first.relationship_type, first.created_at,
    )


@pytest.mark.integration
def test_a_repeat_deactivation_is_a_no_op_with_no_second_event(db_connection, tenant_a_id):
    order, msa = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA")
    link = _link(db_connection, tenant_a_id, order, msa)
    first = _unlink(db_connection, tenant_a_id, link.contract_relationship_id)
    repeat = _unlink(db_connection, tenant_a_id, link.contract_relationship_id)
    assert first.changed is True and repeat.changed is False
    assert repeat.relationship == first.relationship
    deactivations = [e for e in _relationship_entries(db_connection, tenant_a_id) if e.action_type == ACTION_CONTRACT_RELATIONSHIP_DEACTIVATED]
    assert len(deactivations) == 1
    with db_connection.transaction():
        assert deactivate_contract_relationship(
            db_connection, tenant_a_id, str(uuid.uuid4()), actor_id=_ACTOR, actor_role=_ROLE
        ) is None


@pytest.mark.integration
def test_a_failed_replacement_rolled_back_restores_the_original_link(db_connection, tenant_a_id):
    order, msa_one, msa_two = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA-1", "MSA-2")
    original = _link(db_connection, tenant_a_id, order, msa_one)
    _link(db_connection, tenant_a_id, msa_two, order)
    entries_before = len(_relationship_entries(db_connection, tenant_a_id))
    with pytest.raises(DORAContractConflictError, match="cycle"):
        with db_connection.transaction():
            deactivate_contract_relationship(
                db_connection, tenant_a_id, original.contract_relationship_id, actor_id=_ACTOR, actor_role=_ROLE
            )
            add_contract_relationship(db_connection, tenant_a_id, order, msa_two, actor_id=_ACTOR, actor_role=_ROLE)
    with db_connection.transaction():
        restored = get_recorded_parent(db_connection, tenant_a_id, order)
    assert restored == original
    assert len(_relationship_entries(db_connection, tenant_a_id)) == entries_before


@pytest.mark.integration
def test_a_successful_replacement_in_one_transaction_swaps_the_parent(db_connection, tenant_a_id):
    order, msa_one, msa_two = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA-1", "MSA-2")
    original = _link(db_connection, tenant_a_id, order, msa_one)
    with db_connection.transaction():
        deactivate_contract_relationship(
            db_connection, tenant_a_id, original.contract_relationship_id, actor_id=_ACTOR, actor_role=_ROLE
        )
        add_contract_relationship(db_connection, tenant_a_id, order, msa_two, actor_id=_ACTOR, actor_role=_ROLE)
    assert _parent_of(db_connection, tenant_a_id, order) == msa_two


@pytest.mark.integration
def test_contract_state_never_hides_an_edge_and_link_state_never_touches_a_contract(db_connection, tenant_a_id):
    contract_a, contract_b = _contracts(db_connection, tenant_a_id, "A", "B")
    link = _link(db_connection, tenant_a_id, contract_a, contract_b)
    _set_active(db_connection, tenant_a_id, contract_a, False)
    _set_active(db_connection, tenant_a_id, contract_b, False)
    _refused(db_connection, tenant_a_id, contract_b, contract_a, DORAContractConflictError)
    with db_connection.transaction():
        assert [c.child_contract_id for c in list_recorded_children(db_connection, tenant_a_id, contract_b)] == [contract_a]
    _set_active(db_connection, tenant_a_id, contract_a, True)
    _unlink(db_connection, tenant_a_id, link.contract_relationship_id)
    with db_connection.transaction():
        assert get_contract(db_connection, tenant_a_id, contract_a).is_active is True
        assert get_contract(db_connection, tenant_a_id, contract_b).is_active is False


# ── Audit ───────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_creation_and_deactivation_ledger_the_stored_states_and_the_chain_verifies(db_connection, tenant_a_id):
    order, msa = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA")
    link = _link(db_connection, tenant_a_id, order, msa)
    _unlink(db_connection, tenant_a_id, link.contract_relationship_id)
    assert _chain_is_valid(db_connection, tenant_a_id)
    created, deactivated = _relationship_entries(db_connection, tenant_a_id)
    with db_connection.transaction():
        stored = get_contract_relationship(db_connection, tenant_a_id, link.contract_relationship_id)
    assert created.action_type == ACTION_CONTRACT_RELATIONSHIP_CREATED and created.before_state is None
    assert created.after_state["contract_relationship_id"] == link.contract_relationship_id
    assert created.after_state["is_active"] is True
    assert deactivated.action_type == ACTION_CONTRACT_RELATIONSHIP_DEACTIVATED
    assert deactivated.before_state == created.after_state
    assert deactivated.after_state["is_active"] is False
    assert deactivated.after_state["updated_at"] == stored.updated_at.astimezone(timezone.utc).isoformat()
    assert created.after_state["created_at"] == stored.created_at.astimezone(timezone.utc).isoformat()
    assert {k: v for k, v in deactivated.after_state.items() if k not in ("is_active", "updated_at")} == {
        k: v for k, v in created.after_state.items() if k not in ("is_active", "updated_at")
    }


@pytest.mark.integration
def test_a_rolled_back_link_leaves_neither_row_nor_ledger_entry(db_connection, tenant_a_id):
    order, msa = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA")
    add_contract_relationship(db_connection, tenant_a_id, order, msa, actor_id=_ACTOR, actor_role=_ROLE)
    db_connection.rollback()
    assert _row_count(db_connection, tenant_a_id) == 0
    assert _relationship_entries(db_connection, tenant_a_id) == []
    assert _chain_is_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_refused_and_repeated_operations_append_no_success_entries(db_connection, tenant_a_id):
    contract_a, contract_b, contract_c = _contracts(db_connection, tenant_a_id, "A", "B", "C")
    link = _link(db_connection, tenant_a_id, contract_a, contract_b)
    _refused(db_connection, tenant_a_id, contract_a, contract_b, DORAContractConflictError)
    _refused(db_connection, tenant_a_id, contract_a, contract_c, DORAContractConflictError)
    _refused(db_connection, tenant_a_id, contract_b, contract_a, DORAContractConflictError)
    _refused(db_connection, tenant_a_id, contract_a, contract_a, ValueError)
    _unlink(db_connection, tenant_a_id, link.contract_relationship_id)
    _unlink(db_connection, tenant_a_id, link.contract_relationship_id)
    actions = [e.action_type for e in _relationship_entries(db_connection, tenant_a_id)]
    assert actions == [ACTION_CONTRACT_RELATIONSHIP_CREATED, ACTION_CONTRACT_RELATIONSHIP_DEACTIVATED]
    assert _chain_is_valid(db_connection, tenant_a_id)


# ── Transactions the write paths refuse ─────────────────────────────────────


def _open(isolation_level: str | None = None, autocommit: bool = False) -> _DbConnection:
    """Open an independent session in the given mode, inside this test workflow's guard."""
    raw = psycopg2.connect(os.environ["DATABASE_URL"])
    if autocommit:
        raw.autocommit = True
    else:
        raw.set_session(isolation_level=isolation_level, autocommit=False)
    return _DbConnection(raw)


@pytest.mark.integration
@pytest.mark.parametrize("level", ["REPEATABLE READ", "SERIALIZABLE"])
def test_a_stricter_isolation_level_is_refused_before_anything_is_written(db_connection, tenant_a_id, level):
    order, msa = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA")
    link = _link(db_connection, tenant_a_id, order, msa)
    session = _open(level)
    try:
        with pytest.raises(UnsupportedTransactionIsolationError, match="READ COMMITTED"):
            add_contract_relationship(session, tenant_a_id, msa, order, actor_id=_ACTOR, actor_role=_ROLE)
        assert session.execute("SHOW transaction_isolation").fetchone()[0] == level.lower()
        session.rollback()
        with pytest.raises(UnsupportedTransactionIsolationError, match="READ COMMITTED"):
            deactivate_contract_relationship(
                session, tenant_a_id, link.contract_relationship_id, actor_id=_ACTOR, actor_role=_ROLE
            )
        assert session.execute("SHOW transaction_isolation").fetchone()[0] == level.lower()
    finally:
        session.rollback()
        session._conn.close()
    assert _row_count(db_connection, tenant_a_id) == 1
    assert _parent_of(db_connection, tenant_a_id, order) == msa


@pytest.mark.integration
def test_an_autocommit_connection_is_refused_before_anything_is_written(db_connection, tenant_a_id):
    order, msa = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA")
    session = _open(autocommit=True)
    try:
        with pytest.raises(UnsupportedTransactionIsolationError, match="autocommit"):
            add_contract_relationship(session, tenant_a_id, order, msa, actor_id=_ACTOR, actor_role=_ROLE)
    finally:
        session._conn.close()
    assert _row_count(db_connection, tenant_a_id) == 0
    assert _relationship_entries(db_connection, tenant_a_id) == []


@pytest.mark.integration
def test_the_held_check_finds_the_ledger_lock_for_keys_of_both_signs_and_only_while_held(db_connection):
    signs = set()
    for index in range(16):
        tenant = str(uuid.UUID(int=(index + 1) * 0x0123456789ABCDEF, version=4))
        with db_connection.transaction():
            assert tenant_ledger_lock_is_held(db_connection, tenant) is False
            acquire_tenant_ledger_lock(db_connection, tenant)
            assert tenant_ledger_lock_is_held(db_connection, tenant) is True
            signs.add(db_connection.execute("SELECT hashtextextended(%s, 0) < 0", [tenant]).fetchone()[0])
        with db_connection.transaction():
            assert tenant_ledger_lock_is_held(db_connection, tenant) is False
    assert signs == {True, False}, "the sample did not cover both halves of the key space"
