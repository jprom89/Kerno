"""Unit tests for dora_contract_hierarchy_service — guards, serialisation order, cycles, conflicts, ledger.

No database. CONTRACT_A spy connection answers from a small in-memory graph and records
every execute(), so the tests can assert what SQL ran, in what order, and that
nothing ran when the input or the transaction was refused. Real locking,
real concurrency, RLS and the constraints are proven against PostgreSQL in
tests/integration/test_dora_v2_002b_*.py and
tests/security/test_dora_contract_relationship_isolation.py.

Run: pytest tests/unit/services/test_dora_contract_hierarchy_service.py -v
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from config.constants import RbacRole
from src.exceptions import (
    DORAContractConflictError,
    EntryNotFoundError,
    TenantContextMissingError,
    UnsupportedTransactionIsolationError,
)
from src.services.dora_contract_hierarchy_service import (
    ACTION_CONTRACT_RELATIONSHIP_CREATED,
    ACTION_CONTRACT_RELATIONSHIP_DEACTIVATED,
    CONTRACT_RELATIONSHIP_CAPABLE_ROLES,
    OBJECT_TYPE_CONTRACT_RELATIONSHIP,
    add_contract_relationship,
    deactivate_contract_relationship,
    get_contract_relationship,
    get_recorded_parent,
    list_contract_relationship_history,
    list_recorded_children,
)

_TENANT = "a0000000-0000-4000-a000-000000000001"
_ACTOR = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_ROLE = "compliance_lead"
_PLUS_TWO = timezone(timedelta(hours=2))
_STORED_AT = datetime(2026, 10, 1, 14, 0, 0, tzinfo=_PLUS_TWO)


def _contract(letter: str) -> str:
    return f"c0000000-0000-4000-c000-00000000000{letter}"


CONTRACT_A, CONTRACT_B, CONTRACT_C, CONTRACT_D, CONTRACT_E = (_contract(letter) for letter in "abcde")

_ISOLATION = "current_setting('transaction_isolation')"
_HELD = "FROM pg_locks"
_LEDGER_LOCK = "pg_advisory_xact_lock"
_CONTRACT_LOOKUP = "FROM dora_contracts"
_PARENT_LINKS = "child_contract_id = :child_contract_id\n  AND relationship_type"
_CHILD_LINKS = "parent_contract_id = :parent_contract_id"
_HISTORY = "child_contract_id = :child_contract_id\nORDER BY"
_ONE_LINK = "AND contract_relationship_id = :contract_relationship_id"
_INSERT = "INSERT INTO dora_contract_relationships"
_DEACTIVATE = "UPDATE dora_contract_relationships"
_LEDGER_INSERT = "INSERT INTO audit_log"
_RELATIONSHIP_TABLE = "dora_contract_relationships"


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return list(self._rows)


class _GraphSpy:
    """A tenant's contracts and links in memory; records every statement and answers it."""

    def __init__(
        self,
        contracts=(CONTRACT_A, CONTRACT_B, CONTRACT_C, CONTRACT_D, CONTRACT_E),
        links=(),
        *,
        isolation="read committed",
        lock_held=True,
        insert_conflicts=False,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self.contracts = set(contracts)
        self.links: list[dict] = []
        for child, parent, *active in links:
            self.add_link(child, parent, active[0] if active else True)
        self.isolation = isolation
        self.lock_held = lock_held
        self.insert_conflicts = insert_conflicts

    def add_link(self, child: str, parent: str, is_active: bool = True) -> dict:
        link = {
            "contract_relationship_id": str(uuid.uuid4()), "tenant_id": _TENANT,
            "child_contract_id": child, "parent_contract_id": parent,
            "relationship_type": "overarching", "is_active": is_active,
            "created_at": _STORED_AT, "updated_at": _STORED_AT,
        }
        self.links.append(link)
        return link

    def execute(self, sql, params=None):
        sql = str(sql)
        self.calls.append((sql, params))
        if _ISOLATION in sql:
            return _Result([(self.isolation,)])
        if _HELD in sql:
            return _Result([(self.lock_held,)])
        if _INSERT in sql:
            return self._insert(params)
        if _DEACTIVATE in sql:
            return self._deactivate(params)
        if _RELATIONSHIP_TABLE in sql:
            return _Result([_row(link) for link in self._select_links(sql, params)])
        if _CONTRACT_LOOKUP in sql:
            return _Result([(params["contract_id"],)] if params["contract_id"] in self.contracts else [])
        return _Result([])

    def _select_links(self, sql: str, params: dict) -> list[dict]:
        if _ONE_LINK in sql:
            return [link for link in self.links if link["contract_relationship_id"] == params["contract_relationship_id"]]
        if _PARENT_LINKS in sql:
            return [link for link in self.links if link["child_contract_id"] == params["child_contract_id"] and link["is_active"]]
        if _CHILD_LINKS in sql:
            return [link for link in self.links if link["parent_contract_id"] == params["parent_contract_id"] and link["is_active"]]
        if _HISTORY in sql:
            return [link for link in self.links if link["child_contract_id"] == params["child_contract_id"]]
        raise AssertionError(f"unexpected relationship read: {sql}")

    def _insert(self, params: dict):
        if self.insert_conflicts:
            return _Result([])
        link = self.add_link(params["child_contract_id"], params["parent_contract_id"])
        link.update(contract_relationship_id=params["contract_relationship_id"], created_at=_STORED_AT)
        return _Result([_row(link)])

    def _deactivate(self, params: dict):
        for link in self.links:
            if link["contract_relationship_id"] == params["contract_relationship_id"] and link["is_active"]:
                link.update(is_active=False, updated_at=params["updated_at"])
                return _Result([_row(link)])
        return _Result([])

    def statements(self) -> list[str]:
        return [sql for sql, _ in self.calls]


def _row(link: dict) -> tuple:
    return (
        link["contract_relationship_id"], link["tenant_id"], link["child_contract_id"],
        link["parent_contract_id"], link["relationship_type"], link["is_active"],
        link["created_at"], link["updated_at"],
    )


def _add(spy, child, parent, **kwargs):
    actor = {"actor_id": _ACTOR, "actor_role": _ROLE, **kwargs}
    return add_contract_relationship(spy, _TENANT, child, parent, **actor)


def _deactivate(spy, relationship_id, **kwargs):
    actor = {"actor_id": _ACTOR, "actor_role": _ROLE, **kwargs}
    return deactivate_contract_relationship(spy, _TENANT, relationship_id, **actor)


def _index(spy: _GraphSpy, fragment: str) -> int:
    for position, sql in enumerate(spy.statements()):
        if fragment in sql:
            return position
    raise AssertionError(f"no statement containing {fragment!r}")


def _count(spy: _GraphSpy, fragment: str) -> int:
    return sum(fragment in sql for sql in spy.statements())


def _ledger_params(spy: _GraphSpy) -> list[dict]:
    return [params for sql, params in spy.calls if _LEDGER_INSERT in sql]


def _wrote_nothing(spy: _GraphSpy) -> bool:
    return _count(spy, _INSERT) + _count(spy, _DEACTIVATE) + _count(spy, _LEDGER_INSERT) == 0


# ── Input guards: refused before any SQL ────────────────────────────────────


@pytest.mark.parametrize("tenant", [None, "", "not-a-uuid", "a0000000-0000-1000-a000-000000000001"])
def test_an_invalid_tenant_is_refused_before_any_sql(tenant):
    spy = _GraphSpy()
    with pytest.raises(TenantContextMissingError):
        add_contract_relationship(spy, tenant, CONTRACT_A, CONTRACT_B, actor_id=_ACTOR, actor_role=_ROLE)
    with pytest.raises(TenantContextMissingError):
        deactivate_contract_relationship(spy, tenant, str(uuid.uuid4()), actor_id=_ACTOR, actor_role=_ROLE)
    assert spy.calls == []


@pytest.mark.parametrize("actor", [{"actor_id": None}, {"actor_id": "someone"}, {"actor_role": "  "}, {"actor_role": None}])
def test_an_unnamed_actor_is_refused_before_any_sql(actor):
    spy = _GraphSpy()
    with pytest.raises(ValueError):
        _add(spy, CONTRACT_A, CONTRACT_B, **actor)
    with pytest.raises(ValueError):
        _deactivate(spy, str(uuid.uuid4()), **actor)
    assert spy.calls == []


def test_a_direct_self_link_is_refused_before_any_sql_whatever_the_spelling():
    spy = _GraphSpy()
    with pytest.raises(ValueError, match="own overarching"):
        _add(spy, CONTRACT_A, CONTRACT_A.upper())
    assert spy.calls == []


@pytest.mark.parametrize("child, parent", [("not-a-uuid", CONTRACT_B), (CONTRACT_A, "12345")])
def test_a_malformed_contract_id_is_not_found_before_any_sql(child, parent):
    spy = _GraphSpy()
    with pytest.raises(EntryNotFoundError):
        _add(spy, child, parent)
    assert spy.calls == []


def test_the_write_roles_are_a_named_allow_list_of_their_own():
    assert CONTRACT_RELATIONSHIP_CAPABLE_ROLES == (RbacRole.COMPLIANCE_LEAD, RbacRole.VCISO)


# ── Serialisation: isolation, lock, proof, then the graph ───────────────────


def test_add_checks_isolation_then_locks_then_proves_the_lock_before_reading_anything_it_validates():
    spy = _GraphSpy()
    _add(spy, CONTRACT_A, CONTRACT_B)
    isolation, lock, held = _index(spy, _ISOLATION), _index(spy, _LEDGER_LOCK), _index(spy, _HELD)
    first_contract_read, first_graph_read = _index(spy, _CONTRACT_LOOKUP), _index(spy, _PARENT_LINKS)
    assert isolation == 0
    assert isolation < lock < held < first_contract_read < first_graph_read < _index(spy, _INSERT)
    assert _index(spy, _INSERT) < _index(spy, _LEDGER_INSERT)


def test_the_lock_statement_reads_nothing_and_the_graph_is_read_by_later_statements():
    spy = _GraphSpy()
    _add(spy, CONTRACT_A, CONTRACT_B)
    lock_statement = spy.statements()[_index(spy, _LEDGER_LOCK)]
    assert _RELATIONSHIP_TABLE not in lock_statement and _CONTRACT_LOOKUP not in lock_statement


def test_deactivate_serialises_the_same_way_before_its_locking_read():
    spy = _GraphSpy()
    link = spy.add_link(CONTRACT_A, CONTRACT_B)
    _deactivate(spy, link["contract_relationship_id"])
    assert _index(spy, _ISOLATION) == 0
    assert _index(spy, _ISOLATION) < _index(spy, _LEDGER_LOCK) < _index(spy, _HELD) < _index(spy, _ONE_LINK)
    assert "FOR NO KEY UPDATE" in spy.statements()[_index(spy, _ONE_LINK)]


def _add_a_under_b(spy):
    return _add(spy, CONTRACT_A, CONTRACT_B)


def _deactivate_any(spy):
    return _deactivate(spy, str(uuid.uuid4()))


@pytest.mark.parametrize("operation", [_add_a_under_b, _deactivate_any])
@pytest.mark.parametrize("level", ["repeatable read", "serializable", "read uncommitted"])
def test_an_unsupported_isolation_level_is_refused_after_one_read_and_never_changed(level, operation):
    spy = _GraphSpy(isolation=level)
    with pytest.raises(UnsupportedTransactionIsolationError, match="READ COMMITTED"):
        operation(spy)
    assert len(spy.calls) == 1 and _ISOLATION in spy.calls[0][0]
    assert not any("SET TRANSACTION" in sql or "SET SESSION" in sql for sql in spy.statements())


@pytest.mark.parametrize("operation", [_add_a_under_b, _deactivate_any])
def test_a_lock_that_did_not_outlive_its_statement_is_refused_before_any_graph_read(operation):
    spy = _GraphSpy(lock_held=False)
    with pytest.raises(UnsupportedTransactionIsolationError, match="autocommit"):
        operation(spy)
    assert spy.statements()[-1].count(_HELD) == 1
    assert _count(spy, _RELATIONSHIP_TABLE) == 0 and _count(spy, _CONTRACT_LOOKUP) == 0


# ── Endpoints ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("child, parent", [(str(uuid.uuid4()), CONTRACT_B), (CONTRACT_A, str(uuid.uuid4()))])
def test_an_endpoint_this_tenant_lacks_is_not_found_and_nothing_is_written(child, parent):
    spy = _GraphSpy()
    with pytest.raises(EntryNotFoundError, match="not found"):
        _add(spy, child, parent)
    assert _wrote_nothing(spy)


# ── Second parent and duplicates ────────────────────────────────────────────


def test_an_identical_active_link_is_a_conflict_and_nothing_is_written():
    spy = _GraphSpy(links=[(CONTRACT_A, CONTRACT_B)])
    with pytest.raises(DORAContractConflictError, match="already recorded under"):
        _add(spy, CONTRACT_A, CONTRACT_B)
    assert _wrote_nothing(spy)


def test_a_second_active_parent_is_a_conflict_and_nothing_is_written():
    spy = _GraphSpy(links=[(CONTRACT_A, CONTRACT_B)])
    with pytest.raises(DORAContractConflictError, match="already has an active overarching arrangement"):
        _add(spy, CONTRACT_A, CONTRACT_C)
    assert _wrote_nothing(spy)


def test_an_inactive_old_link_does_not_block_a_new_one():
    spy = _GraphSpy(links=[(CONTRACT_A, CONTRACT_B, False)])
    created = _add(spy, CONTRACT_A, CONTRACT_C)
    assert (created.child_contract_id, created.parent_contract_id, created.is_active) == (CONTRACT_A, CONTRACT_C, True)


def test_two_active_parents_already_stored_are_refused_for_review_not_resolved():
    spy = _GraphSpy(links=[(CONTRACT_A, CONTRACT_B), (CONTRACT_A, CONTRACT_C)])
    with pytest.raises(DORAContractConflictError, match="needs review"):
        _add(spy, CONTRACT_A, CONTRACT_D)
    assert _wrote_nothing(spy)


def test_when_the_unique_index_absorbs_the_insert_it_is_a_conflict_without_a_ledger_entry():
    spy = _GraphSpy(insert_conflicts=True)
    with pytest.raises(DORAContractConflictError, match="nothing was recorded"):
        _add(spy, CONTRACT_A, CONTRACT_B)
    assert _count(spy, _LEDGER_INSERT) == 0


def test_the_insert_names_the_partial_unique_index_and_nothing_broader():
    spy = _GraphSpy()
    _add(spy, CONTRACT_A, CONTRACT_B)
    insert = spy.statements()[_index(spy, _INSERT)]
    assert "ON CONFLICT (tenant_id, child_contract_id, relationship_type) WHERE is_active DO NOTHING" in insert
    assert "ON CONSTRAINT" not in insert and "DO UPDATE" not in insert


# ── Cycles ──────────────────────────────────────────────────────────────────


def test_a_two_node_cycle_is_a_conflict():
    spy = _GraphSpy(links=[(CONTRACT_B, CONTRACT_A)])
    with pytest.raises(DORAContractConflictError, match="would create a cycle"):
        _add(spy, CONTRACT_A, CONTRACT_B)
    assert _wrote_nothing(spy)


def test_a_longer_cycle_is_a_conflict():
    spy = _GraphSpy(links=[(CONTRACT_A, CONTRACT_B), (CONTRACT_B, CONTRACT_C), (CONTRACT_C, CONTRACT_D)])
    with pytest.raises(DORAContractConflictError, match="would create a cycle"):
        _add(spy, CONTRACT_D, CONTRACT_A)
    assert _wrote_nothing(spy)


def test_an_inactive_link_is_not_followed_but_an_active_link_to_an_inactive_contract_is():
    # The service never asks for a contract's own state: the walk is over links.
    spy = _GraphSpy(links=[(CONTRACT_B, CONTRACT_A, False)])
    assert _add(spy, CONTRACT_A, CONTRACT_B).is_active is True
    with pytest.raises(DORAContractConflictError, match="would create a cycle"):
        _add(spy, CONTRACT_B, CONTRACT_A)
    assert not any("is_active" in sql and "FROM dora_contracts" in sql for sql in spy.statements())


def test_a_valid_link_walks_the_whole_ancestry_and_records_three_levels():
    spy = _GraphSpy(links=[(CONTRACT_B, CONTRACT_C), (CONTRACT_C, CONTRACT_D)])
    created = _add(spy, CONTRACT_A, CONTRACT_B)
    assert created.parent_contract_id == CONTRACT_B
    walked = [params["child_contract_id"] for sql, params in spy.calls if _PARENT_LINKS in sql]
    assert walked == [CONTRACT_A, CONTRACT_B, CONTRACT_C, CONTRACT_D]


def test_a_loop_already_above_the_parent_ends_the_walk_and_is_refused_not_skipped():
    spy = _GraphSpy(links=[(CONTRACT_B, CONTRACT_C), (CONTRACT_C, CONTRACT_D), (CONTRACT_D, CONTRACT_B)])
    with pytest.raises(DORAContractConflictError, match="already loops"):
        _add(spy, CONTRACT_A, CONTRACT_B)
    assert _count(spy, _PARENT_LINKS) <= 1 + 3
    assert _wrote_nothing(spy)


def test_several_active_parents_above_the_parent_are_refused_not_resolved():
    spy = _GraphSpy(links=[(CONTRACT_B, CONTRACT_C), (CONTRACT_B, CONTRACT_D)])
    with pytest.raises(DORAContractConflictError, match="more than one active"):
        _add(spy, CONTRACT_A, CONTRACT_B)
    assert _wrote_nothing(spy)


# ── Ledger ──────────────────────────────────────────────────────────────────


def test_creation_is_ledgered_once_with_the_stored_row_in_utc():
    spy = _GraphSpy()
    created = _add(spy, CONTRACT_A, CONTRACT_B)
    [entry] = _ledger_params(spy)
    assert (entry["action_type"], entry["object_type"]) == (
        ACTION_CONTRACT_RELATIONSHIP_CREATED, OBJECT_TYPE_CONTRACT_RELATIONSHIP,
    )
    assert entry["object_id"] == created.contract_relationship_id
    assert entry["actor_id"] == str(_ACTOR) and entry["actor_role"] == _ROLE
    assert entry["before_state"] is None
    after = json.loads(entry["after_state"])
    assert after == {
        "contract_relationship_id": created.contract_relationship_id, "tenant_id": _TENANT,
        "child_contract_id": CONTRACT_A, "parent_contract_id": CONTRACT_B, "relationship_type": "overarching",
        "is_active": True, "created_at": "2026-10-01T12:00:00+00:00", "updated_at": "2026-10-01T12:00:00+00:00",
    }


def test_deactivation_ledgers_the_row_before_and_after_and_touches_no_contract():
    spy = _GraphSpy()
    link = spy.add_link(CONTRACT_A, CONTRACT_B)
    outcome = _deactivate(spy, link["contract_relationship_id"].upper())
    assert outcome.changed is True and outcome.relationship.is_active is False
    [entry] = _ledger_params(spy)
    before = json.loads(entry["before_state"])
    after = json.loads(entry["after_state"])
    assert entry["action_type"] == ACTION_CONTRACT_RELATIONSHIP_DEACTIVATED
    assert before["is_active"] is True and after["is_active"] is False
    assert {k: v for k, v in before.items() if k not in ("is_active", "updated_at")} == {
        k: v for k, v in after.items() if k not in ("is_active", "updated_at")
    }
    assert not any("UPDATE dora_contracts" in sql for sql in spy.statements())


def test_repeating_a_deactivation_is_an_explicit_no_op_with_no_update_and_no_entry():
    spy = _GraphSpy()
    link = spy.add_link(CONTRACT_A, CONTRACT_B, is_active=False)
    outcome = _deactivate(spy, link["contract_relationship_id"])
    assert outcome.changed is False and outcome.relationship.is_active is False
    assert _count(spy, _DEACTIVATE) == 0 and _ledger_params(spy) == []


def test_deactivating_an_unknown_link_returns_none_and_writes_nothing():
    spy = _GraphSpy()
    assert _deactivate(spy, str(uuid.uuid4())) is None
    assert _wrote_nothing(spy)
    spy = _GraphSpy()
    assert _deactivate(spy, "not-a-uuid") is None
    assert spy.calls == []


@pytest.mark.parametrize("links, child, parent", [
    ([(CONTRACT_A, CONTRACT_B)], CONTRACT_A, CONTRACT_C),
    ([(CONTRACT_B, CONTRACT_A)], CONTRACT_A, CONTRACT_B),
    ([], CONTRACT_A, "c0000000-0000-4000-c000-0000000000ff"),
], ids=["second-parent", "cycle", "unknown-parent"])
def test_no_refused_add_appends_a_ledger_entry(links, child, parent):
    spy = _GraphSpy(links=links)
    with pytest.raises((DORAContractConflictError, EntryNotFoundError)):
        _add(spy, child, parent)
    assert _ledger_params(spy) == []


# ── Reads: no locks, explicit tenant predicate, deterministic order ─────────


def test_reads_take_no_lock_and_check_no_isolation():
    spy = _GraphSpy(links=[(CONTRACT_A, CONTRACT_B), (CONTRACT_C, CONTRACT_B)])
    link_id = spy.links[0]["contract_relationship_id"]
    get_contract_relationship(spy, _TENANT, link_id)
    get_recorded_parent(spy, _TENANT, CONTRACT_A)
    list_recorded_children(spy, _TENANT, CONTRACT_B)
    list_contract_relationship_history(spy, _TENANT, CONTRACT_A)
    for sql in spy.statements():
        assert _LEDGER_LOCK not in sql and _ISOLATION not in sql and "FOR " not in sql.replace("FORCE", "")


def test_every_relationship_statement_carries_an_explicit_tenant_predicate():
    spy = _GraphSpy(links=[(CONTRACT_B, CONTRACT_C)])
    created = _add(spy, CONTRACT_A, CONTRACT_B)
    _deactivate(spy, created.contract_relationship_id)
    get_recorded_parent(spy, _TENANT, CONTRACT_A)
    list_recorded_children(spy, _TENANT, CONTRACT_B)
    list_contract_relationship_history(spy, _TENANT, CONTRACT_A)
    for sql, params in spy.calls:
        if _RELATIONSHIP_TABLE in sql or _CONTRACT_LOOKUP in sql:
            assert ":tenant_id" in sql and params["tenant_id"] == _TENANT


def test_list_reads_order_by_creation_then_id():
    spy = _GraphSpy()
    list_recorded_children(spy, _TENANT, CONTRACT_B)
    list_contract_relationship_history(spy, _TENANT, CONTRACT_A)
    for sql in spy.statements():
        if _RELATIONSHIP_TABLE in sql:
            assert "ORDER BY created_at ASC, contract_relationship_id ASC" in sql


def test_no_recorded_parent_is_none_and_two_recorded_parents_are_refused():
    assert get_recorded_parent(_GraphSpy(), _TENANT, CONTRACT_A) is None
    with pytest.raises(DORAContractConflictError, match="needs review"):
        get_recorded_parent(_GraphSpy(links=[(CONTRACT_A, CONTRACT_B), (CONTRACT_A, CONTRACT_C)]), _TENANT, CONTRACT_A)


def test_children_are_the_active_links_only_and_history_keeps_inactive_ones():
    spy = _GraphSpy(links=[(CONTRACT_A, CONTRACT_B, False), (CONTRACT_A, CONTRACT_C), (CONTRACT_D, CONTRACT_C)])
    assert [link.child_contract_id for link in list_recorded_children(spy, _TENANT, CONTRACT_C)] == [CONTRACT_A, CONTRACT_D]
    assert list_recorded_children(spy, _TENANT, CONTRACT_B) == []
    assert [(link.parent_contract_id, link.is_active) for link in list_contract_relationship_history(spy, _TENANT, CONTRACT_A)] == [
        (CONTRACT_B, False), (CONTRACT_C, True),
    ]


@pytest.mark.parametrize("read, empty", [
    (get_contract_relationship, None), (get_recorded_parent, None),
    (list_recorded_children, []), (list_contract_relationship_history, []),
])
def test_a_malformed_id_reads_as_nothing_without_sql(read, empty):
    spy = _GraphSpy()
    assert read(spy, _TENANT, "not-a-uuid") == empty
    assert spy.calls == []
