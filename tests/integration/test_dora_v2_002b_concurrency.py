"""DORA-V2-002B — contract hierarchy writes under real concurrency, independent sessions at READ COMMITTED.

What:  drives dora_contract_hierarchy_service from two PostgreSQL sessions and
       proves, against the live database: two identical links leave one active
       relationship; two different parents for one child leave one; A -> B and
       B -> A cannot both commit; a cycle assembled from writes to different
       rows (existing A -> B and C -> D, then B -> C against D -> A) cannot
       commit; a waiting valid link proceeds once the first transaction rolls
       back, including the D -> A half of that fixture; and a deactivation
       serialises against an add for the same child. Each loser commits
       nothing and appends no success entry; the hash chain verifies.
Why:   A read-only cycle check passes every one of these interleavings one
       transaction at a time; only serialising before the graph read stops
       them, and only interleavings prove it.
How:   pytest tests/integration/test_dora_v2_002b_concurrency.py -m integration -v

Synchronisation is explicit: the main thread waits, under a deadline, until
pg_blocking_pids() says the competitor is blocked by the first session, and
asserts the wait is on an advisory lock — the tenant ledger lock, taken
before any graph read — then commits or rolls the first session back. There
are no sleeps for effect. Every session is opened at an explicitly set READ
COMMITTED level (asserted), carries lock_timeout and statement_timeout, and is
rolled back and closed in a finally, the holding session first. All sessions
are opened inside this pytest workflow, under its test-database guard and
exclusive lock.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import psycopg2
import pytest

from src.exceptions import DORAContractConflictError
from src.services.audit_log import get_entries_by_actor, verify_audit_chain
from src.services.dora_contract_hierarchy_service import (
    ACTION_CONTRACT_RELATIONSHIP_CREATED,
    OBJECT_TYPE_CONTRACT_RELATIONSHIP,
    add_contract_relationship,
    deactivate_contract_relationship,
    get_recorded_parent,
    list_contract_relationship_history,
)
from src.services.dora_contract_service import ContractInput, create_contract
from tests.conftest import _DbConnection

_ACTOR = uuid.UUID("d0000000-0000-4000-d000-000000000007")
_ROLE = "compliance_lead"
_ISOLATION_LEVEL = "read committed"
_LOCK_TIMEOUT_MS = 10_000
_STATEMENT_TIMEOUT_FACTOR = 2
_SYNC_DEADLINE_SECONDS = 10.0
_SYNC_POLL_SECONDS = 0.01
_THREAD_JOIN_SECONDS = 15.0


# ── Sessions and synchronisation ────────────────────────────────────────────


def _session() -> _DbConnection:
    """Open an independent READ COMMITTED session with bounded lock and statement waits."""
    raw = psycopg2.connect(os.environ["DATABASE_URL"])
    raw.set_session(isolation_level="READ COMMITTED", autocommit=False)
    conn = _DbConnection(raw)
    conn.execute("SET lock_timeout = %s", [f"{_LOCK_TIMEOUT_MS}ms"])
    conn.execute("SET statement_timeout = %s", [f"{_LOCK_TIMEOUT_MS * _STATEMENT_TIMEOUT_FACTOR}ms"])
    conn.commit()
    return conn


def _close_quietly(conn: _DbConnection | None) -> None:
    """Roll back whatever a session still holds and close it; never raise from cleanup."""
    if conn is None:
        return
    try:
        conn.rollback()
    except psycopg2.Error:
        pass
    finally:
        conn._conn.close()


def _backend_pid(conn: _DbConnection) -> int:
    """Return the session's backend pid (ends the implicit transaction)."""
    pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    conn.commit()
    return pid


def _isolation_level(conn: _DbConnection) -> str:
    """Return the isolation level of the session's current transaction."""
    return conn.execute("SHOW transaction_isolation").fetchone()[0]


class _InSession(threading.Thread):
    """Runs one unit of work on its own session and commits; records result, error and isolation."""

    def __init__(self, conn: _DbConnection, work) -> None:
        super().__init__(daemon=True)
        self._conn = conn
        self._work = work
        self.result = None
        self.error: BaseException | None = None
        self.isolation: str | None = None

    def run(self) -> None:
        try:
            self.isolation = _isolation_level(self._conn)
            self.result = self._work(self._conn)
            self._conn.commit()
        except BaseException as exc:  # recorded for the main thread, never swallowed
            self.error = exc
            try:
                self._conn.rollback()
            except psycopg2.Error:
                pass


def _wait_until_blocked_on_advisory_lock(monitor, waiting: _InSession, waiting_pid: int, holding_pid: int) -> None:
    """Block until waiting_pid is waiting on holding_pid, and prove the wait is for an advisory lock.

    Fails at once, with the real cause, if the competitor finished without
    blocking, and at the deadline if the interleaving never happened.
    """
    deadline = time.monotonic() + _SYNC_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if not waiting.is_alive():
            raise AssertionError(f"backend {waiting_pid} finished before blocking on {holding_pid}") from waiting.error
        with monitor.transaction():
            blocked = monitor.execute(
                "SELECT %s = ANY (pg_blocking_pids(%s))", [holding_pid, waiting_pid]
            ).fetchone()[0]
            waits = [row[0] for row in monitor.execute(
                "SELECT locktype FROM pg_locks WHERE pid = %s AND NOT granted", [waiting_pid]
            ).fetchall()]
        if blocked:
            assert waits == ["advisory"], f"backend {waiting_pid} is waiting on {waits}, not the tenant lock"
            return
        time.sleep(_SYNC_POLL_SECONDS)
    raise AssertionError(f"backend {waiting_pid} never blocked on {holding_pid} within {_SYNC_DEADLINE_SECONDS}s")


def _race(monitor, first_work, competitor_work, *, first_commits: bool = True) -> tuple[object, _InSession]:
    """Run first_work on one session, start competitor_work on another until it blocks, then end the first.

    Returns (first result, competitor thread). The first session commits, or
    rolls back when first_commits is False. Sessions are closed whatever happens.
    """
    first = second = competitor = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        assert _isolation_level(first) == _ISOLATION_LEVEL
        first_result = first_work(first)
        competitor = _InSession(second, competitor_work)
        competitor.start()
        _wait_until_blocked_on_advisory_lock(monitor, competitor, second_pid, first_pid)
        if first_commits:
            first.commit()
        else:
            first.rollback()
        competitor.join(_THREAD_JOIN_SECONDS)
        assert not competitor.is_alive(), "the competing session never finished"
        assert competitor.isolation == _ISOLATION_LEVEL
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)
    return first_result, competitor


# ── Domain helpers ──────────────────────────────────────────────────────────


def _contracts(conn, tenant_id, *references: str) -> list[str]:
    """Create and commit one contract per reference on the fixture connection; return their ids."""
    with conn.transaction():
        return [
            create_contract(conn, tenant_id, ContractInput(contract_reference=reference), actor_id=_ACTOR, actor_role=_ROLE).contract_id
            for reference in references
        ]


def _adding(tenant_id, child: str, parent: str):
    """Return a unit of work that links child -> parent."""
    def work(conn):
        return add_contract_relationship(conn, tenant_id, child, parent, actor_id=_ACTOR, actor_role=_ROLE)
    return work


def _deactivating(tenant_id, relationship_id: str):
    """Return a unit of work that deactivates one link."""
    def work(conn):
        return deactivate_contract_relationship(conn, tenant_id, relationship_id, actor_id=_ACTOR, actor_role=_ROLE)
    return work


def _committed_link(conn, tenant_id, child: str, parent: str):
    """Link child -> parent on the fixture connection and commit."""
    with conn.transaction():
        return _adding(tenant_id, child, parent)(conn)


def _parent_of(conn, tenant_id, child: str) -> str | None:
    """Return the child's committed active parent, or None."""
    with conn.transaction():
        link = get_recorded_parent(conn, tenant_id, child)
    return link.parent_contract_id if link else None


def _active_links(conn, tenant_id, *children: str) -> list[tuple[str, str]]:
    """Return every committed active (child, parent) pair among the given children."""
    pairs = []
    with conn.transaction():
        for child in children:
            pairs += [(h.child_contract_id, h.parent_contract_id)
                      for h in list_contract_relationship_history(conn, tenant_id, child) if h.is_active]
    return pairs


def _created(conn, tenant_id) -> list[tuple[str, str]]:
    """Return the (child, parent) of every committed creation entry, oldest first."""
    with conn.transaction():
        entries = get_entries_by_actor(conn, tenant_id, _ACTOR)
    return [(e.after_state["child_contract_id"], e.after_state["parent_contract_id"]) for e in entries
            if e.object_type == OBJECT_TYPE_CONTRACT_RELATIONSHIP and e.action_type == ACTION_CONTRACT_RELATIONSHIP_CREATED]


def _chain_is_valid(conn, tenant_id) -> bool:
    """Verify the tenant's whole hash chain on the fixture connection."""
    with conn.transaction():
        result = verify_audit_chain(conn, tenant_id)
    assert result.is_valid, result.failure_reason
    return True


def _walks_to_a_root(conn, tenant_id, start: str) -> bool:
    """Follow committed active parent links from start; True if they end at a contract with no parent."""
    seen: set[str] = set()
    current: str | None = start
    while current is not None:
        if current in seen:
            return False
        seen.add(current)
        current = _parent_of(conn, tenant_id, current)
    return True


# ── Duplicates and second parents ───────────────────────────────────────────


@pytest.mark.integration
def test_two_identical_concurrent_links_leave_one_active_relationship(db_connection, tenant_a_id):
    order, msa = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA")
    winner, competitor = _race(db_connection, _adding(tenant_a_id, order, msa), _adding(tenant_a_id, order, msa))
    assert isinstance(competitor.error, DORAContractConflictError), competitor.error
    assert _active_links(db_connection, tenant_a_id, order) == [(order, msa)]
    assert _created(db_connection, tenant_a_id) == [(order, msa)]
    assert winner.child_contract_id == order
    assert _chain_is_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_two_different_concurrent_parents_leave_only_one_active_parent(db_connection, tenant_a_id):
    order, msa_one, msa_two = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA-1", "MSA-2")
    _, competitor = _race(db_connection, _adding(tenant_a_id, order, msa_one), _adding(tenant_a_id, order, msa_two))
    assert isinstance(competitor.error, DORAContractConflictError), competitor.error
    assert _active_links(db_connection, tenant_a_id, order) == [(order, msa_one)]
    assert _created(db_connection, tenant_a_id) == [(order, msa_one)]
    assert _chain_is_valid(db_connection, tenant_a_id)


# ── Cycles across transactions ──────────────────────────────────────────────


@pytest.mark.integration
def test_a_to_b_and_b_to_a_cannot_both_commit(db_connection, tenant_a_id):
    contract_a, contract_b = _contracts(db_connection, tenant_a_id, "A", "B")
    _, competitor = _race(db_connection, _adding(tenant_a_id, contract_a, contract_b), _adding(tenant_a_id, contract_b, contract_a))
    assert isinstance(competitor.error, DORAContractConflictError), competitor.error
    assert "cycle" in str(competitor.error)
    assert _active_links(db_connection, tenant_a_id, contract_a, contract_b) == [(contract_a, contract_b)]
    assert _created(db_connection, tenant_a_id) == [(contract_a, contract_b)]
    assert _chain_is_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_a_cycle_assembled_from_writes_to_different_rows_cannot_commit(db_connection, tenant_a_id):
    # Against the initial graph A -> B and C -> D, B -> C alone is valid and
    # D -> A alone is valid; together they close A -> B -> C -> D -> A.
    contract_a, contract_b, contract_c, contract_d = _contracts(db_connection, tenant_a_id, "A", "B", "C", "D")
    _committed_link(db_connection, tenant_a_id, contract_a, contract_b)
    _committed_link(db_connection, tenant_a_id, contract_c, contract_d)
    _, competitor = _race(db_connection, _adding(tenant_a_id, contract_b, contract_c), _adding(tenant_a_id, contract_d, contract_a))
    assert isinstance(competitor.error, DORAContractConflictError), competitor.error
    assert "cycle" in str(competitor.error)
    assert sorted(_active_links(db_connection, tenant_a_id, contract_a, contract_b, contract_c, contract_d)) == sorted([(contract_a, contract_b), (contract_b, contract_c), (contract_c, contract_d)])
    assert _parent_of(db_connection, tenant_a_id, contract_d) is None
    assert all(_walks_to_a_root(db_connection, tenant_a_id, node) for node in (contract_a, contract_b, contract_c, contract_d))
    assert (contract_d, contract_a) not in _created(db_connection, tenant_a_id)
    assert _chain_is_valid(db_connection, tenant_a_id)


# ── A waiter proceeds once the holder rolls back ────────────────────────────


@pytest.mark.integration
def test_a_waiting_valid_link_proceeds_after_the_first_transaction_rolls_back(db_connection, tenant_a_id):
    order, msa_one, msa_two = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA-1", "MSA-2")
    _, competitor = _race(
        db_connection, _adding(tenant_a_id, order, msa_one), _adding(tenant_a_id, order, msa_two), first_commits=False,
    )
    assert competitor.error is None, competitor.error
    assert _active_links(db_connection, tenant_a_id, order) == [(order, msa_two)]
    assert _created(db_connection, tenant_a_id) == [(order, msa_two)]
    assert _chain_is_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_the_cycle_fixture_s_second_half_proceeds_when_the_first_half_rolls_back(db_connection, tenant_a_id):
    contract_a, contract_b, contract_c, contract_d = _contracts(db_connection, tenant_a_id, "A", "B", "C", "D")
    _committed_link(db_connection, tenant_a_id, contract_a, contract_b)
    _committed_link(db_connection, tenant_a_id, contract_c, contract_d)
    _, competitor = _race(db_connection, _adding(tenant_a_id, contract_b, contract_c), _adding(tenant_a_id, contract_d, contract_a), first_commits=False)
    assert competitor.error is None, competitor.error
    assert sorted(_active_links(db_connection, tenant_a_id, contract_a, contract_b, contract_c, contract_d)) == sorted([(contract_a, contract_b), (contract_c, contract_d), (contract_d, contract_a)])
    assert _parent_of(db_connection, tenant_a_id, contract_b) is None
    assert _chain_is_valid(db_connection, tenant_a_id)


# ── Deactivation is serialised too ──────────────────────────────────────────


@pytest.mark.integration
def test_a_deactivation_serialises_against_an_add_for_the_same_child(db_connection, tenant_a_id):
    order, msa_one, msa_two = _contracts(db_connection, tenant_a_id, "ORDER-1", "MSA-1", "MSA-2")
    original = _committed_link(db_connection, tenant_a_id, order, msa_one)
    outcome, competitor = _race(
        db_connection, _deactivating(tenant_a_id, original.contract_relationship_id), _adding(tenant_a_id, order, msa_two),
    )
    assert outcome.changed is True
    assert competitor.error is None, competitor.error
    assert _active_links(db_connection, tenant_a_id, order) == [(order, msa_two)]
    assert _chain_is_valid(db_connection, tenant_a_id)
