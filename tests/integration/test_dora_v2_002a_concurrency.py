"""DORA-V2-002A — contract writes under real concurrency, two sessions at READ COMMITTED.

What:  drives the contract service from independent PostgreSQL sessions and
       proves, against the live database: two amendments of one contract ledger
       contiguous history (the second's before_state is the first's
       after_state) with the hash chain verified separately; two creations of
       one reference, and two additions of one party tuple, each produce one
       row, one success entry and one controlled DORAContractConflictError; a
       duplicate that waited on an insert which then rolled back is created,
       not refused; a competitor rejected at a lock, and the loser of a
       deadlock, commit nothing; and the update path's row lock does not queue
       a party insert.
Why:   The guarantees in dora_contract_service's docstring are claims about
       interleavings, and only interleavings prove them. Accurate audit
       before-state is the guarantee; rejecting a stale edit is not.
How:   pytest tests/integration/test_dora_v2_002a_concurrency.py -m integration -v

Synchronisation is explicit: the main thread waits, under a deadline, until
pg_blocking_pids() says one session is blocked on the other, then releases
it. There are no sleeps for effect. Every session is opened at an explicitly
set READ COMMITTED isolation level (asserted), carries lock_timeout and
statement_timeout, and is rolled back and closed in a finally — the holding
session first, so a still-waiting competitor is released at once.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import psycopg2
import psycopg2.errors
import pytest

from src.exceptions import DORAContractConflictError
from src.services.audit_log import get_entries_by_actor, verify_audit_chain
from src.services.dora_contract_service import (
    ACTION_CONTRACT_CREATED,
    ACTION_CONTRACT_PARTY_ADDED,
    ACTION_CONTRACT_UPDATED,
    ContractInput,
    ContractUpdate,
    add_contract_party,
    create_contract,
    get_contract,
    list_contract_parties,
    list_contracts,
    update_contract,
)
from src.services.dora_organization_service import OrganizationInput, create_organization
from tests.conftest import _DbConnection

_ACTOR = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_ROLE = "compliance_lead"

# The level the application runs at (psycopg2's default; nothing in src/
# overrides it). Set explicitly on every session and asserted in each test.
_ISOLATION_LEVEL = "read committed"

_LOCK_TIMEOUT_MS = 10_000
# A statement must be allowed to outlive the lock wait it may contain, or
# statement_timeout would fire first and hide which bound was hit.
_STATEMENT_TIMEOUT_FACTOR = 2
_REJECTION_LOCK_TIMEOUT_MS = 500
_SYNC_DEADLINE_SECONDS = 10.0
_SYNC_POLL_SECONDS = 0.01
_THREAD_JOIN_SECONDS = 15.0


# ── Sessions and synchronisation ────────────────────────────────────────────


def _session(lock_timeout_ms: int = _LOCK_TIMEOUT_MS) -> _DbConnection:
    """Open an independent READ COMMITTED session with bounded lock and statement waits."""
    raw = psycopg2.connect(os.environ["DATABASE_URL"])
    raw.set_session(isolation_level="READ COMMITTED", autocommit=False)
    conn = _DbConnection(raw)
    conn.execute("SET lock_timeout = %s", [f"{lock_timeout_ms}ms"])
    conn.execute(
        "SET statement_timeout = %s", [f"{lock_timeout_ms * _STATEMENT_TIMEOUT_FACTOR}ms"]
    )
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


def _wait_until_blocked_by(monitor: _DbConnection, waiting: _InSession, waiting_pid: int, holding_pid: int) -> None:
    """Block until the catalog shows waiting_pid waiting on a lock held by holding_pid.

    Polls pg_blocking_pids() under a deadline. Fails at once, with the real
    cause, if the competing thread finished before it ever blocked; fails at
    the deadline if the interleaving under test never happened.
    """
    deadline = time.monotonic() + _SYNC_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if not waiting.is_alive():
            raise AssertionError(
                f"backend {waiting_pid} finished before blocking on {holding_pid}"
            ) from waiting.error
        with monitor.transaction():
            blocked = monitor.execute(
                "SELECT %s = ANY (pg_blocking_pids(%s))", [holding_pid, waiting_pid]
            ).fetchone()[0]
        if blocked:
            return
        time.sleep(_SYNC_POLL_SECONDS)
    raise AssertionError(
        f"backend {waiting_pid} never blocked on {holding_pid} within {_SYNC_DEADLINE_SECONDS}s"
    )


def _finish(thread: _InSession) -> None:
    """Join a competing thread within the bound and fail if it is still running."""
    thread.join(_THREAD_JOIN_SECONDS)
    assert not thread.is_alive(), "the competing session never finished"


# ── Domain helpers ──────────────────────────────────────────────────────────


def _create(conn, tenant_id, reference: str, display_name: str | None = None):
    """create_contract with this test file's actor."""
    return create_contract(
        conn, tenant_id, ContractInput(contract_reference=reference, display_name=display_name),
        actor_id=_ACTOR, actor_role=_ROLE,
    )


def _rename(conn, tenant_id, contract_id: str, display_name: str):
    """update_contract changing only the display name."""
    return update_contract(
        conn, tenant_id, contract_id,
        ContractUpdate(display_name=display_name, contract_start_date=None, contract_end_date=None, is_active=True),
        actor_id=_ACTOR, actor_role=_ROLE,
    )


def _sign(conn, tenant_id, contract_id: str, organization_id: str):
    """add_contract_party as recipient signatory."""
    return add_contract_party(
        conn, tenant_id, contract_id, organization_id, "recipient_signatory", actor_id=_ACTOR, actor_role=_ROLE,
    )


def _committed_contract(conn, tenant_id, reference: str, display_name: str | None = None) -> str:
    """Create and commit one contract on the fixture connection; return its id."""
    with conn.transaction():
        return _create(conn, tenant_id, reference, display_name).contract_id


def _committed_organization(conn, tenant_id, name: str) -> str:
    """Create and commit one organisation on the fixture connection; return its id."""
    with conn.transaction():
        return create_organization(
            conn, tenant_id, OrganizationInput(legal_name=name), actor_id=_ACTOR, actor_role=_ROLE
        ).organization_id


def _entries(conn, tenant_id, action: str) -> list:
    """Return this actor's committed entries for one action, oldest first."""
    with conn.transaction():
        return [e for e in get_entries_by_actor(conn, tenant_id, _ACTOR) if e.action_type == action]


def _chain_is_valid(conn, tenant_id) -> bool:
    """Verify the tenant's whole hash chain on the fixture connection."""
    with conn.transaction():
        result = verify_audit_chain(conn, tenant_id)
    assert result.is_valid, result.failure_reason
    return True


def _references(conn, tenant_id) -> list[str]:
    """Return every committed contract reference for the tenant."""
    with conn.transaction():
        return [c.contract_reference for c in list_contracts(conn, tenant_id)]


def _deadlock_timeout_ms(conn) -> int:
    """Return the server's deadlock_timeout in milliseconds."""
    with conn.transaction():
        return int(conn.execute("SELECT setting FROM pg_settings WHERE name = 'deadlock_timeout'").fetchone()[0])


# ── Competing amendments: contiguous history ────────────────────────────────


@pytest.mark.integration
def test_the_second_of_two_concurrent_updates_ledgers_the_state_it_actually_replaced(db_connection, tenant_a_id):
    contract_id = _committed_contract(db_connection, tenant_a_id, "MSA-1", "Name Zero")
    first = second = competitor = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        assert _isolation_level(first) == _ISOLATION_LEVEL
        _rename(first, tenant_a_id, contract_id, "Name One")
        competitor = _InSession(second, lambda conn: _rename(conn, tenant_a_id, contract_id, "Name Two"))
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, second_pid, first_pid)
        first.commit()
        _finish(competitor)
        if competitor.error is not None:
            raise competitor.error
        assert competitor.isolation == _ISOLATION_LEVEL
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    with db_connection.transaction():
        assert get_contract(db_connection, tenant_a_id, contract_id).display_name == "Name Two"
    # The chain is checked first and separately: a stale before_state would
    # be hashed and linked correctly, so a valid chain alone proves nothing.
    assert _chain_is_valid(db_connection, tenant_a_id)
    updates = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_UPDATED)
    assert [e.after_state["display_name"] for e in updates] == ["Name One", "Name Two"]
    assert updates[0].before_state["display_name"] == "Name Zero"
    assert updates[1].before_state["display_name"] == "Name One"
    assert updates[1].before_state == updates[0].after_state


@pytest.mark.integration
def test_a_competing_update_rejected_at_the_lock_writes_neither_row_nor_ledger_entry(db_connection, tenant_a_id):
    # lock_timeout is the cut this test can make on demand; the application
    # sets none, so in production the rejection is deadlock detection (below).
    # This test passes on a plain-SELECT implementation too — it pins "an
    # aborted competitor leaves nothing", not the locking read.
    contract_id = _committed_contract(db_connection, tenant_a_id, "MSA-1", "Name Zero")
    first = second = None
    try:
        first, second = _session(), _session(_REJECTION_LOCK_TIMEOUT_MS)
        _rename(first, tenant_a_id, contract_id, "Name One")
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            _rename(second, tenant_a_id, contract_id, "Name Two")
        second.rollback()
        first.commit()
    finally:
        _close_quietly(second)
        _close_quietly(first)

    with db_connection.transaction():
        assert get_contract(db_connection, tenant_a_id, contract_id).display_name == "Name One"
    assert [e.after_state["display_name"] for e in _entries(db_connection, tenant_a_id, ACTION_CONTRACT_UPDATED)] == ["Name One"]
    assert _chain_is_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_the_update_row_lock_does_not_queue_a_party_insert_on_that_contract(db_connection, tenant_a_id):
    # FOR NO KEY UPDATE is compatible with the FOR KEY SHARE a party insert's
    # foreign-key check takes on its contract; FOR UPDATE would make this raw
    # insert wait out lock_timeout. Raw on purpose: through the service the
    # insert would also wait on the tenant ledger lock the open amendment
    # holds, which would hide which lock was being measured.
    contract_id = _committed_contract(db_connection, tenant_a_id, "MSA-1")
    organization_id = _committed_organization(db_connection, tenant_a_id, "Alpha Bank AG")
    first = second = None
    try:
        first, second = _session(), _session(_REJECTION_LOCK_TIMEOUT_MS)
        assert _rename(first, tenant_a_id, contract_id, "Being amended") is not None
        with second.transaction():
            second.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            second.execute(
                "INSERT INTO dora_contract_parties (tenant_id, contract_id, organization_id, party_role) "
                "VALUES (%s, %s, %s, 'recipient_signatory')",
                [str(tenant_a_id), contract_id, organization_id],
            )
        still_open = first.execute(
            "SELECT display_name FROM dora_contracts WHERE contract_id = %s", [contract_id]
        ).fetchone()[0]
        assert still_open == "Being amended", "the amendment was not still open when the party landed"
        first.commit()
    finally:
        _close_quietly(second)
        _close_quietly(first)
    with db_connection.transaction():
        assert [p.organization_id for p in list_contract_parties(db_connection, tenant_a_id, contract_id)] == [organization_id]


# ── Competing duplicates: the constraint decides, one conflict ──────────────


@pytest.mark.integration
def test_two_concurrent_creations_of_one_reference_give_one_row_one_event_one_conflict(db_connection, tenant_a_id):
    first = second = competitor = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        winner = _create(first, tenant_a_id, "MSA-RACE", "first")
        competitor = _InSession(second, lambda conn: _create(conn, tenant_a_id, "MSA-RACE", "second"))
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, second_pid, first_pid)
        first.commit()
        _finish(competitor)
        assert competitor.isolation == _ISOLATION_LEVEL
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    assert isinstance(competitor.error, DORAContractConflictError), competitor.error
    assert competitor.result is None
    assert _references(db_connection, tenant_a_id) == ["MSA-RACE"]
    with db_connection.transaction():
        assert get_contract(db_connection, tenant_a_id, winner.contract_id).display_name == "first"
    creations = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_CREATED)
    assert [(e.object_id, e.after_state["display_name"]) for e in creations] == [(winner.contract_id, "first")]
    assert _chain_is_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_two_concurrent_additions_of_one_party_give_one_row_one_event_one_conflict(db_connection, tenant_a_id):
    contract_id = _committed_contract(db_connection, tenant_a_id, "MSA-1")
    organization_id = _committed_organization(db_connection, tenant_a_id, "Alpha Bank AG")
    first = second = competitor = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        winner = _sign(first, tenant_a_id, contract_id, organization_id)
        competitor = _InSession(second, lambda conn: _sign(conn, tenant_a_id, contract_id, organization_id))
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, second_pid, first_pid)
        first.commit()
        _finish(competitor)
        assert competitor.isolation == _ISOLATION_LEVEL
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    assert isinstance(competitor.error, DORAContractConflictError), competitor.error
    with db_connection.transaction():
        parties = list_contract_parties(db_connection, tenant_a_id, contract_id)
    assert [p.contract_party_id for p in parties] == [winner.contract_party_id]
    additions = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_PARTY_ADDED)
    assert [e.object_id for e in additions] == [winner.contract_party_id]
    assert _chain_is_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_a_duplicate_that_waited_on_a_rolled_back_insert_is_created_not_refused(db_connection, tenant_a_id):
    # The conflict outcome is the constraint's verdict on committed data, not
    # "someone else tried": if the first insert rolls back, the waiter wins.
    first = second = competitor = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        _create(first, tenant_a_id, "MSA-RACE", "first")
        competitor = _InSession(second, lambda conn: _create(conn, tenant_a_id, "MSA-RACE", "second"))
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, second_pid, first_pid)
        first.rollback()
        _finish(competitor)
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    assert competitor.error is None, competitor.error
    assert _references(db_connection, tenant_a_id) == ["MSA-RACE"]
    creations = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_CREATED)
    assert [e.after_state["display_name"] for e in creations] == ["second"]
    assert creations[0].object_id == competitor.result.contract_id
    assert _chain_is_valid(db_connection, tenant_a_id)


# ── Lock order per transaction: cycles resolve, and losers leave nothing ────


def _run_cycle(db_connection, tenant_id, first_before, competitor_work, first_closing):
    """Drive the documented KER-107 cycle and return (first_error, competitor).

    Session one runs first_before (which ledgers, so it holds the tenant
    advisory lock), the competitor runs competitor_work on its own thread
    until it blocks on session one, then session one runs first_closing,
    which waits on the competitor. PostgreSQL aborts one side.
    """
    first = second = competitor = None
    first_error: BaseException | None = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        first_before(first)
        competitor = _InSession(second, competitor_work)
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, second_pid, first_pid)
        try:
            first_closing(first)
            first.commit()
        except psycopg2.errors.DeadlockDetected as exc:
            first_error = exc
            first.rollback()
        _finish(competitor)
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)
    losers = [error for error in (first_error, competitor.error) if error is not None]
    assert len(losers) == 1 and isinstance(losers[0], psycopg2.errors.DeadlockDetected), losers
    return first_error, competitor


@pytest.mark.integration
def test_an_update_after_a_ledgered_write_can_deadlock_and_the_loser_leaves_nothing(db_connection, tenant_a_id):
    if _deadlock_timeout_ms(db_connection) >= _LOCK_TIMEOUT_MS:
        pytest.skip("deadlock_timeout is not below this test's lock_timeout")
    contract_x = _committed_contract(db_connection, tenant_a_id, "MSA-X", "X zero")
    contract_y = _committed_contract(db_connection, tenant_a_id, "MSA-Y", "Y zero")
    first_error, competitor = _run_cycle(
        db_connection, tenant_a_id,
        lambda conn: _rename(conn, tenant_a_id, contract_x, "X by first"),
        lambda conn: _rename(conn, tenant_a_id, contract_y, "Y by second"),
        lambda conn: _rename(conn, tenant_a_id, contract_y, "Y by first"),
    )
    first_lost = first_error is not None
    expected_x, expected_y = ("X zero", "Y by second") if first_lost else ("X by first", "Y by first")
    with db_connection.transaction():
        assert get_contract(db_connection, tenant_a_id, contract_x).display_name == expected_x
        assert get_contract(db_connection, tenant_a_id, contract_y).display_name == expected_y
    updates = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_UPDATED)
    names = [e.after_state["display_name"] for e in updates]
    assert ("Y by first" if first_lost else "Y by second") not in names
    y_updates = [e for e in updates if e.object_id == contract_y]
    assert [e.before_state["display_name"] for e in y_updates] == ["Y zero"]
    assert _chain_is_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_a_creation_after_a_ledgered_write_can_deadlock_on_a_duplicate_and_the_loser_leaves_nothing(
    db_connection, tenant_a_id
):
    # The duplicate-key wait is the lock this slice adds. Session one has
    # ledgered MSA-A (holding the advisory lock); the competitor has inserted
    # MSA-B and waits for the advisory lock; session one then inserts MSA-B
    # and waits on the competitor's uncommitted key.
    if _deadlock_timeout_ms(db_connection) >= _LOCK_TIMEOUT_MS:
        pytest.skip("deadlock_timeout is not below this test's lock_timeout")
    first_error, competitor = _run_cycle(
        db_connection, tenant_a_id,
        lambda conn: _create(conn, tenant_a_id, "MSA-A", "A by first"),
        lambda conn: _create(conn, tenant_a_id, "MSA-B", "B by second"),
        lambda conn: _create(conn, tenant_a_id, "MSA-B", "B by first"),
    )
    first_lost = first_error is not None
    creations = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_CREATED)
    created_names = sorted(e.after_state["display_name"] for e in creations)
    if first_lost:
        assert _references(db_connection, tenant_a_id) == ["MSA-B"]
        assert created_names == ["B by second"]
    else:
        assert _references(db_connection, tenant_a_id) == ["MSA-A", "MSA-B"]
        assert created_names == ["A by first", "B by first"]
    assert _chain_is_valid(db_connection, tenant_a_id)
