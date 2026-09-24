"""DORA-V2-001 — two concurrent amendments of one organisation must both ledger the truth.

What:  drives update_organization() from two independent PostgreSQL sessions at
       READ COMMITTED, interleaved so that the second session's read happens
       while the first session's write is uncommitted. Asserts that the second
       ledger entry's before_state is the state the second write actually
       replaced — the first session's committed values — and not the state both
       sessions could originally see. Checks the hash chain as well, and keeps
       the two checks separate: the chain is valid either way, because a stale
       before_state is a correctly hashed, correctly linked, wrong record.
Why:   An amendment to a legal identity is reconstructable only if the ledger
       says what it replaced. Under READ COMMITTED a plain SELECT before the
       UPDATE lets two amendments capture the same old row; the per-tenant
       advisory lock inside append_audit_entry() serialises the ledger writes
       but is taken after the state was captured, so it cannot protect it. The
       mutation path takes a row lock on the read instead. This file is the
       regression test for that: it failed against the plain-SELECT
       implementation and passes against the locking read.
How:   pytest tests/integration/test_dora_v2_001_update_concurrency.py -m integration -v

Synchronisation is explicit, never a sleep for effect: the main thread waits,
under a deadline, until the catalog says the second session is blocked on the
first, then releases the first. Every session carries lock_timeout and
statement_timeout, so a broken interleaving fails with an error instead of
hanging the suite, and every session is rolled back and closed in a finally.

The last test pins the lock order the service docstring records per
transaction, not per call: a caller-owned transaction that has already
ledgered holds the tenant advisory lock, so amending a row a competing session
holds closes a cycle. PostgreSQL resolves it; the test proves the loser leaves
nothing behind and the winner's trail is still accurate.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

import psycopg2
import psycopg2.errors
import pytest

from src.services.audit_log import get_entries_by_actor, verify_audit_chain
from src.services.dora_organization_service import (
    ACTION_ORGANIZATION_UPDATED,
    OrganizationInput,
    create_organization,
    get_organization,
    update_organization,
)
from tests.conftest import _DbConnection

_ACTOR = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_ROLE = "compliance_lead"

# The isolation level the application runs at (psycopg2 default, nothing in
# src/ overrides it). Named here so the test says which level it proves.
_ISOLATION_LEVEL = "read committed"

# Bounds. A session that waits longer than this for a lock errors out with
# LockNotAvailable rather than hanging; the synchronisation deadline is what
# the main thread will wait for the catalog to confirm a session is blocked.
_LOCK_TIMEOUT_MS = 10_000
# A statement must be allowed to outlive the lock wait it may contain, or
# statement_timeout would fire first and hide which bound was hit.
_STATEMENT_TIMEOUT_FACTOR = 2
_SYNC_DEADLINE_SECONDS = 10.0
_SYNC_POLL_SECONDS = 0.01
_THREAD_JOIN_SECONDS = 15.0

# A lock_timeout short enough to reject a competing session deliberately.
_REJECTION_LOCK_TIMEOUT_MS = 500

_STATE_ZERO = "Version Zero Ltd"
_STATE_ONE = "Version One Ltd"
_STATE_TWO = "Version Two Ltd"

# Only the lock-order test needs a second row; the names say who wrote what.
_OTHER_STATE_ZERO = "Other Zero Ltd"
_FIRST_AMENDS_OTHER = "First Session Amends Other"
_SECOND_AMENDS_OTHER = "Second Session Amends Other"


def _session(lock_timeout_ms: int) -> _DbConnection:
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
    """Return the PostgreSQL backend pid of this session (ends the implicit transaction)."""
    pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    conn.commit()
    return pid


def _isolation_level(conn: _DbConnection) -> str:
    """Return the isolation level the session's current transaction is running at."""
    return conn.execute("SHOW transaction_isolation").fetchone()[0]


def _wait_until_blocked_by(
    monitor: _DbConnection, waiting: _CompetingUpdate, waiting_pid: int, holding_pid: int
) -> None:
    """Block until the catalog reports waiting_pid is waiting on a lock held by holding_pid.

    Polls pg_blocking_pids() under a deadline. Raises AssertionError if the
    deadline passes, which is the honest outcome when the interleaving under
    test never happened — and at once, carrying the real cause, if the
    competing thread finished or failed before it ever blocked.
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
        f"backend {waiting_pid} never blocked on backend {holding_pid} within "
        f"{_SYNC_DEADLINE_SECONDS}s — the interleaving under test did not occur"
    )


class _CompetingUpdate(threading.Thread):
    """Runs one update_organization() on its own session and commits, recording the outcome."""

    def __init__(self, conn: _DbConnection, tenant_id, organization_id: str, legal_name: str) -> None:
        super().__init__(daemon=True)
        self._conn = conn
        self._tenant_id = tenant_id
        self._organization_id = organization_id
        self._legal_name = legal_name
        self.result = None
        self.error: BaseException | None = None
        self.isolation: str | None = None

    def run(self) -> None:
        try:
            self.isolation = _isolation_level(self._conn)
            self.result = update_organization(
                self._conn, self._tenant_id, self._organization_id,
                OrganizationInput(legal_name=self._legal_name),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
            self._conn.commit()
        except BaseException as exc:  # recorded for the main thread, never swallowed
            self.error = exc
            self._conn.rollback()


def _create_state_zero(conn: _DbConnection, tenant_id) -> str:
    """Create the organisation at state zero, committed, and return its id."""
    with conn.transaction():
        created = create_organization(
            conn, tenant_id, OrganizationInput(legal_name=_STATE_ZERO),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    return created.organization_id


def _update_entries_for(conn: _DbConnection, tenant_id, organization_id: str) -> list:
    """Return this organisation's update entries, oldest first."""
    with conn.transaction():
        entries = get_entries_by_actor(conn, tenant_id, _ACTOR)
    return [
        e for e in entries
        if e.object_id == organization_id and e.action_type == ACTION_ORGANIZATION_UPDATED
    ]


# ── The regression ──────────────────────────────────────────────────────────


@pytest.mark.integration
def test_second_of_two_concurrent_updates_ledgers_the_state_it_actually_replaced(
    db_connection, tenant_a_id
):
    organization_id = _create_state_zero(db_connection, tenant_a_id)
    first = second = None
    competitor = None
    try:
        first = _session(_LOCK_TIMEOUT_MS)
        second = _session(_LOCK_TIMEOUT_MS)
        first_pid = _backend_pid(first)
        second_pid = _backend_pid(second)

        # Session one amends to state one and holds the transaction open.
        assert _isolation_level(first) == _ISOLATION_LEVEL
        update_organization(
            first, tenant_a_id, organization_id, OrganizationInput(legal_name=_STATE_ONE),
            actor_id=_ACTOR, actor_role=_ROLE,
        )

        # Session two amends to state two while session one is uncommitted.
        # Under READ COMMITTED it can only see state zero as committed data.
        competitor = _CompetingUpdate(second, tenant_a_id, organization_id, _STATE_TWO)
        competitor.start()
        _wait_until_blocked_by(
            db_connection, competitor, waiting_pid=second_pid, holding_pid=first_pid
        )

        # Release session one; session two proceeds against the committed row.
        first.commit()
        competitor.join(_THREAD_JOIN_SECONDS)
        assert not competitor.is_alive(), "the competing update never finished"
        if competitor.error is not None:
            raise competitor.error
        assert competitor.isolation == _ISOLATION_LEVEL
    finally:
        # Release session one's locks first so a still-waiting competitor is
        # unblocked at once rather than after its lock_timeout; only then is
        # its session closed, and only from this thread once it has stopped.
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    with db_connection.transaction():
        final = get_organization(db_connection, tenant_a_id, organization_id)
    assert final is not None and final.legal_name == _STATE_TWO

    # The hash chain is valid in both the broken and the fixed implementation
    # — that is why it is checked first and separately. A stale before_state
    # is hashed and linked correctly; only the history is wrong.
    with db_connection.transaction():
        chain = verify_audit_chain(db_connection, tenant_a_id)
    assert chain.is_valid, chain.failure_reason

    entries = _update_entries_for(db_connection, tenant_a_id, organization_id)
    assert [e.after_state["legal_name"] for e in entries] == [_STATE_ONE, _STATE_TWO]
    assert entries[0].before_state["legal_name"] == _STATE_ZERO
    assert entries[1].before_state["legal_name"] == _STATE_ONE, (
        "hash chain verified valid, yet the second amendment recorded "
        f"before_state={entries[1].before_state['legal_name']!r}: it replaced "
        f"{_STATE_ONE!r}, the first amendment's committed value"
    )
    # Contiguity, not just names: the second entry's before_state is the
    # first entry's after_state in every field the row carries.
    assert entries[1].before_state == entries[0].after_state


# ── A competitor that is rejected leaves nothing behind ─────────────────────


@pytest.mark.integration
def test_a_competing_update_rejected_at_the_lock_writes_neither_row_nor_ledger_entry(
    db_connection, tenant_a_id
):
    # A competitor whose wait is cut short aborts before its UPDATE and before
    # its ledger append, so it can leave neither a business change nor a
    # success entry. lock_timeout is the cut this test can make on demand;
    # the application sets none, so the rejection production actually sees
    # is deadlock detection, proven in the last test of this file. Stale user
    # edits are NOT rejected by this design: a competitor that waits out the
    # lock proceeds against the fresh row (previous test). This test passes
    # on the plain-SELECT implementation too — it pins requirement 4, not the
    # fix.
    organization_id = _create_state_zero(db_connection, tenant_a_id)
    first = second = None
    try:
        first = _session(_LOCK_TIMEOUT_MS)
        second = _session(_REJECTION_LOCK_TIMEOUT_MS)
        update_organization(
            first, tenant_a_id, organization_id, OrganizationInput(legal_name=_STATE_ONE),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            update_organization(
                second, tenant_a_id, organization_id, OrganizationInput(legal_name=_STATE_TWO),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
        second.rollback()
        first.commit()
    finally:
        _close_quietly(second)
        _close_quietly(first)

    with db_connection.transaction():
        final = get_organization(db_connection, tenant_a_id, organization_id)
    assert final is not None and final.legal_name == _STATE_ONE
    entries = _update_entries_for(db_connection, tenant_a_id, organization_id)
    assert [e.after_state["legal_name"] for e in entries] == [_STATE_ONE]
    with db_connection.transaction():
        assert verify_audit_chain(db_connection, tenant_a_id).is_valid


# ── Lock strength: the read takes the lock the write needs, no more ─────────


@pytest.mark.integration
def test_the_locking_read_does_not_block_a_child_row_referencing_the_organisation(
    db_connection, tenant_a_id
):
    # FOR NO KEY UPDATE is the lock an UPDATE of non-key columns takes itself.
    # A child INSERT's foreign-key check takes FOR KEY SHARE on the parent,
    # which is compatible, so the row lock alone does not queue a child row
    # behind an amendment; FOR UPDATE would make this insert wait out
    # lock_timeout and fail. The insert is raw on purpose: through the
    # service, add_organization_identifier would wait on the tenant's ledger
    # advisory lock, which the uncommitted amendment already holds, and that
    # wait would hide which lock was being measured.
    organization_id = _create_state_zero(db_connection, tenant_a_id)
    first = second = None
    try:
        first = _session(_LOCK_TIMEOUT_MS)
        second = _session(_REJECTION_LOCK_TIMEOUT_MS)
        amended = update_organization(
            first, tenant_a_id, organization_id, OrganizationInput(legal_name=_STATE_ONE),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
        assert amended is not None
        with second.transaction():
            second.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            second.execute(
                "INSERT INTO dora_organization_identifiers "
                "(tenant_id, organization_id, identifier_type, identifier_value) "
                "VALUES (%s, %s, 'LEI', '5493001KJTIIGC8Y1R12')",
                [str(tenant_a_id), organization_id],
            )
        # Session one's amendment must still have been open while the child
        # row landed — otherwise the insert measured nothing.
        uncommitted = first.execute(
            "SELECT legal_name FROM dora_organizations WHERE organization_id = %s",
            [organization_id],
        ).fetchone()[0]
        assert uncommitted == _STATE_ONE
        first.commit()
    finally:
        _close_quietly(second)
        _close_quietly(first)

    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        landed = db_connection.execute(
            "SELECT identifier_value FROM dora_organization_identifiers "
            "WHERE organization_id = %s",
            [organization_id],
        ).fetchall()
    assert [row[0] for row in landed] == ["5493001KJTIIGC8Y1R12"]


# ── Lock order per transaction: a cycle is possible, and it resolves cleanly ─


def _deadlock_timeout_ms(conn: _DbConnection) -> int:
    """Return the server's deadlock_timeout in milliseconds."""
    with conn.transaction():
        setting = conn.execute(
            "SELECT setting FROM pg_settings WHERE name = 'deadlock_timeout'"
        ).fetchone()[0]
    return int(setting)


def _legal_names(conn: _DbConnection, tenant_id, *organization_ids: str) -> list[str]:
    """Return the committed legal names of the given organisations, in the order asked."""
    names = []
    for organization_id in organization_ids:
        with conn.transaction():
            names.append(get_organization(conn, tenant_id, organization_id).legal_name)
    return names


@pytest.mark.integration
def test_a_transaction_that_already_ledgered_can_deadlock_and_the_loser_leaves_nothing(
    db_connection, tenant_a_id
):
    # Per call, the row lock precedes the tenant advisory lock. Per
    # transaction it need not: session one amends X and so holds the advisory
    # lock until it commits; session two amends Y, holds Y's row lock, and
    # waits on that advisory lock; session one then amends Y too and waits on
    # Y's row lock. That is a cycle. PostgreSQL aborts one side with 40P01
    # after deadlock_timeout — which side depends on whose wait timer fires
    # first, so both outcomes are accepted and the invariants are the same:
    # the loser wrote neither row nor ledger entry, every committed entry's
    # before_state is the state it really replaced, and the chain verifies.
    if _deadlock_timeout_ms(db_connection) >= _LOCK_TIMEOUT_MS:
        pytest.skip("deadlock_timeout is not below this test's lock_timeout")
    org_x = _create_state_zero(db_connection, tenant_a_id)
    with db_connection.transaction():
        org_y = create_organization(
            db_connection, tenant_a_id, OrganizationInput(legal_name=_OTHER_STATE_ZERO),
            actor_id=_ACTOR, actor_role=_ROLE,
        ).organization_id

    first = second = None
    competitor = None
    first_error: BaseException | None = None
    try:
        first = _session(_LOCK_TIMEOUT_MS)
        second = _session(_LOCK_TIMEOUT_MS)
        first_pid = _backend_pid(first)
        second_pid = _backend_pid(second)

        update_organization(
            first, tenant_a_id, org_x, OrganizationInput(legal_name=_STATE_ONE),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
        competitor = _CompetingUpdate(second, tenant_a_id, org_y, _SECOND_AMENDS_OTHER)
        competitor.start()
        _wait_until_blocked_by(
            db_connection, competitor, waiting_pid=second_pid, holding_pid=first_pid
        )
        try:
            update_organization(
                first, tenant_a_id, org_y, OrganizationInput(legal_name=_FIRST_AMENDS_OTHER),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
            first.commit()
        except psycopg2.errors.DeadlockDetected as exc:
            first_error = exc
            first.rollback()
        competitor.join(_THREAD_JOIN_SECONDS)
        assert not competitor.is_alive(), "the competing update never finished"
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    losers = [error for error in (first_error, competitor.error) if error is not None]
    assert len(losers) == 1, f"expected exactly one aborted side, got {losers!r}"
    assert isinstance(losers[0], psycopg2.errors.DeadlockDetected)
    first_lost = first_error is not None

    expected_names = (
        [_STATE_ZERO, _SECOND_AMENDS_OTHER] if first_lost else [_STATE_ONE, _FIRST_AMENDS_OTHER]
    )
    assert _legal_names(db_connection, tenant_a_id, org_x, org_y) == expected_names

    entries_x = _update_entries_for(db_connection, tenant_a_id, org_x)
    entries_y = _update_entries_for(db_connection, tenant_a_id, org_y)
    loser_name = _FIRST_AMENDS_OTHER if first_lost else _SECOND_AMENDS_OTHER
    assert loser_name not in [e.after_state["legal_name"] for e in entries_x + entries_y]
    assert [e.after_state["legal_name"] for e in entries_x] == ([] if first_lost else [_STATE_ONE])
    assert [e.after_state["legal_name"] for e in entries_y] == [expected_names[1]]
    # The winner's before_state is the state it really replaced: the loser
    # never committed, so that is the original in both cases.
    assert [e.before_state["legal_name"] for e in entries_x] == ([] if first_lost else [_STATE_ZERO])
    assert [e.before_state["legal_name"] for e in entries_y] == [_OTHER_STATE_ZERO]
    with db_connection.transaction():
        assert verify_audit_chain(db_connection, tenant_a_id).is_valid
