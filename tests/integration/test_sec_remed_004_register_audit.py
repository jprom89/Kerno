"""SEC-REMED-004 — a v1 register amendment ledgers the state it actually replaced.

What:  drives update_register_entry() from independent PostgreSQL sessions at
       READ COMMITTED. Two concurrent amendments of one synthetic entry are
       interleaved so that the second session starts while the first session's
       amendment is uncommitted; the second ledger entry must name the first
       amendment's committed row as its before_state, in every field. Also
       covers time-zone-independent ledger timestamps, rollback when the ledger
       append fails, tenant isolation, non-locking reads, and the documented
       per-transaction lock-order limitation.
Why:   regression for finding race.register-audit-before-state (low / high,
       source-established at 75f2bf18). The amendment read the row with a
       plain SELECT, so two writers could both capture state A; the second then
       overwrote B with C while recording A -> C. The tenant ledger lock inside
       append_audit_entry() keeps the hash chain linear but is taken after the
       state was captured, so the chain verifies either way. That is why the
       chain and the history are checked separately here.
How:   pytest tests/integration/test_sec_remed_004_register_audit.py -m integration -v

Synchronisation is explicit, never a sleep for effect: the main thread waits,
under a deadline, until the catalog says the second session is blocked on the
first, then releases the first. Both the old and the fixed implementation
reach that state (at the UPDATE and at the locking read respectively), so the
test does not need both writers to pass the read before the first is released.
Every session carries lock_timeout and statement_timeout, and every session is
rolled back and closed in a finally. Sessions run in a non-UTC time zone so a
timestamp's representation cannot hide or fake a difference between states.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
import uuid
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.errors
import pytest
from fastapi.testclient import TestClient

import src.services.dora_roi_service as dora_roi_service
from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
from src.api.routers.overrides import get_reviewer_id
from src.models.dora_register_entry import (
    CRITICALITY_CRITICAL,
    CRITICALITY_HIGH,
    CRITICALITY_STANDARD,
    PROVIDER_TYPE_CLOUD,
    PROVIDER_TYPE_SOFTWARE,
)
from src.services.audit_log import get_entries_by_actor, verify_audit_chain
from src.services.dora_roi_service import (
    ACTION_REGISTER_ENTRY_UPDATED,
    RegisterEntryInput,
    RegisterEntryOutput,
    create_register_entry,
    get_register_entry,
    list_active_register_entries,
    update_register_entry,
)
from tests.conftest import _DbConnection

_ACTOR = str(uuid.UUID("d0000000-0000-4000-d000-000000000404"))
_ROLE = "compliance_lead"

# The isolation level the application runs at (psycopg2 default; nothing in
# src/ overrides it), named so the test says which level it proves.
_ISOLATION_LEVEL = "read committed"

# A session time zone other than UTC: values read back from the database then
# carry a non-zero offset, which the ledger must not let differ from the UTC
# timestamps the service generates.
_SESSION_TIME_ZONE = "Europe/Berlin"
_UTC_SUFFIX = "+00:00"
_TIMESTAMP_FIELDS = ("created_at", "updated_at")

_LOCK_TIMEOUT_MS = 10_000
# A statement must be allowed to outlive the lock wait it may contain, or
# statement_timeout would fire first and hide which bound was hit.
_STATEMENT_TIMEOUT_FACTOR = 2
_SYNC_DEADLINE_SECONDS = 10.0
_SYNC_POLL_SECONDS = 0.01
_THREAD_JOIN_SECONDS = 15.0
# Short enough to turn an unexpected wait into an immediate failure.
_PROBE_LOCK_TIMEOUT_MS = 500

_STATE_A = {
    "provider_name": "State A Cloud", "service_name": "Hosting A", "provider_type": PROVIDER_TYPE_CLOUD,
    "criticality_level": CRITICALITY_CRITICAL, "business_function": "Payments",
    "data_types": ["operational"], "countries_supported": ["DE"],
    "contract_start_date": date(2026, 1, 1), "contract_end_date": date(2027, 1, 1),
    "exit_strategy_summary": "Exit plan A", "is_active": True, "source_record_id": None,
}
_STATE_B = {
    "provider_name": "State B Cloud", "service_name": "Hosting B", "provider_type": PROVIDER_TYPE_SOFTWARE,
    "criticality_level": CRITICALITY_HIGH, "business_function": "Lending",
    "data_types": ["operational", "personal"], "countries_supported": ["DE", "NL"],
    "contract_start_date": date(2026, 2, 1), "contract_end_date": date(2028, 1, 1),
    "exit_strategy_summary": "Exit plan B", "is_active": True, "source_record_id": None,
}
_STATE_C = {
    "provider_name": "State C Cloud", "service_name": "Hosting C", "provider_type": PROVIDER_TYPE_CLOUD,
    "criticality_level": CRITICALITY_STANDARD, "business_function": "Reporting",
    "data_types": ["financial"], "countries_supported": ["FR"],
    "contract_start_date": None, "contract_end_date": None,
    "exit_strategy_summary": None, "is_active": False, "source_record_id": None,
}
# Only the lock-order test needs a second entry; the names say who wrote what.
_OTHER_STATE_A = {**_STATE_A, "provider_name": "Other A Cloud"}
_FIRST_AMENDS_OTHER = {**_STATE_B, "provider_name": "First Session Amends Other"}
_SECOND_AMENDS_OTHER = {**_STATE_C, "provider_name": "Second Session Amends Other"}


def _session(lock_timeout_ms: int) -> _DbConnection:
    """Open an independent READ COMMITTED session with bounded waits and a non-UTC time zone."""
    raw = psycopg2.connect(os.environ["DATABASE_URL"])
    raw.set_session(isolation_level="READ COMMITTED", autocommit=False)
    conn = _DbConnection(raw)
    conn.execute("SET lock_timeout = %s", [f"{lock_timeout_ms}ms"])
    conn.execute("SET statement_timeout = %s", [f"{lock_timeout_ms * _STATEMENT_TIMEOUT_FACTOR}ms"])
    conn.execute("SET TIME ZONE %s", [_SESSION_TIME_ZONE])
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


class _CompetingAmendment(threading.Thread):
    """Runs one update_register_entry() on its own session and commits, recording the outcome."""

    def __init__(self, conn: _DbConnection, tenant_id, entry_id: str, values: dict) -> None:
        super().__init__(daemon=True)
        self._conn = conn
        self._tenant_id = tenant_id
        self._entry_id = entry_id
        self._values = values
        self.result: RegisterEntryOutput | None = None
        self.error: BaseException | None = None
        self.isolation: str | None = None

    def run(self) -> None:
        try:
            self.isolation = _isolation_level(self._conn)
            self.result = update_register_entry(
                self._conn, self._tenant_id, self._entry_id, RegisterEntryInput(**self._values),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
            self._conn.commit()
        except BaseException as exc:  # recorded for the main thread, never swallowed
            self.error = exc
            self._conn.rollback()


def _wait_until_blocked_by(
    monitor: _DbConnection, waiting: _CompetingAmendment, waiting_pid: int, holding_pid: int,
) -> None:
    """Block until the catalog reports waiting_pid waits on a lock holding_pid holds, under a deadline."""
    deadline = time.monotonic() + _SYNC_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        if not waiting.is_alive():
            raise AssertionError(f"backend {waiting_pid} finished before blocking on {holding_pid}") from waiting.error
        with monitor.transaction():
            blocked = monitor.execute(
                "SELECT %s = ANY (pg_blocking_pids(%s))", [holding_pid, waiting_pid]
            ).fetchone()[0]
        if blocked:
            return
        time.sleep(_SYNC_POLL_SECONDS)
    raise AssertionError(f"backend {waiting_pid} never blocked on backend {holding_pid}: the interleaving did not occur")


def _create(conn: _DbConnection, tenant_id, values: dict) -> str:
    """Create a committed register entry with the given values and return its id."""
    with conn.transaction():
        created = create_register_entry(
            conn, tenant_id, RegisterEntryInput(**values), actor_id=_ACTOR, actor_role=_ROLE
        )
    return created.register_entry_id


def _stored(conn: _DbConnection, tenant_id, entry_id: str) -> RegisterEntryOutput:
    """Read an entry's committed row through the ordinary getter."""
    with conn.transaction():
        return get_register_entry(conn, tenant_id, entry_id)


def _ledger_state(entry: RegisterEntryOutput) -> dict:
    """Return a stored entry in ledger form, built here independently: timestamps in UTC, dates in ISO 8601."""
    state = dataclasses.asdict(entry)
    for name, value in state.items():
        if isinstance(value, datetime):
            state[name] = value.astimezone(timezone.utc).isoformat()
        elif isinstance(value, date):
            state[name] = value.isoformat()
    return state


def _business_values(entry: RegisterEntryOutput) -> dict:
    """Return only the fields a caller supplies, for comparison with an input state."""
    return {name: getattr(entry, name) for name in _STATE_A}


def _business_part(state: dict) -> dict:
    """Return the caller-supplied fields of a ledger state, leaving out ids and timestamps."""
    return {name: state[name] for name in _STATE_A}


def _as_ledgered(values: dict) -> dict:
    """Return an input state as the ledger renders it: dates as ISO 8601 strings."""
    return {name: value.isoformat() if isinstance(value, date) else value for name, value in values.items()}


def _amendments(conn: _DbConnection, tenant_id, entry_id: str) -> list:
    """Return this entry's amendment ledger entries, oldest first."""
    with conn.transaction():
        entries = get_entries_by_actor(conn, tenant_id, _ACTOR)
    return [e for e in entries if e.object_id == entry_id and e.action_type == ACTION_REGISTER_ENTRY_UPDATED]


def _audit_row_count(conn: _DbConnection, tenant_id) -> int:
    """Return the tenant's total ledger row count, whatever the actor or action."""
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return conn.execute("SELECT count(*) FROM audit_log WHERE tenant_id = %s", [str(tenant_id)]).fetchone()[0]


def _assert_chain_valid(conn: _DbConnection, tenant_id) -> None:
    with conn.transaction():
        chain = verify_audit_chain(conn, tenant_id)
    assert chain.is_valid, chain.failure_reason


def _assert_lockable_now(tenant_id, entry_id: str) -> None:
    """Prove no other session holds the entry's row lock: a fresh session takes it without waiting."""
    probe = _session(_PROBE_LOCK_TIMEOUT_MS)
    try:
        with probe.transaction():
            probe.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
            locked = probe.execute(
                "SELECT register_entry_id FROM dora_register_entries "
                "WHERE register_entry_id = %s FOR NO KEY UPDATE NOWAIT",
                [entry_id],
            ).fetchone()
        assert locked is not None
    finally:
        _close_quietly(probe)


# ── The regression ──────────────────────────────────────────────────────────


@pytest.mark.integration
def test_second_of_two_concurrent_amendments_ledgers_the_state_it_actually_replaced(db_connection, tenant_a_id):
    entry_id = _create(db_connection, tenant_a_id, _STATE_A)
    state_a = _stored(db_connection, tenant_a_id, entry_id)
    first = second = competitor = None
    try:
        first = _session(_LOCK_TIMEOUT_MS)
        second = _session(_LOCK_TIMEOUT_MS)
        first_pid = _backend_pid(first)
        second_pid = _backend_pid(second)

        # Session one amends A -> B and holds its transaction open.
        assert _isolation_level(first) == _ISOLATION_LEVEL
        state_b = update_register_entry(
            first, tenant_a_id, entry_id, RegisterEntryInput(**_STATE_B), actor_id=_ACTOR, actor_role=_ROLE,
        )

        # Session two amends to C while B is uncommitted; A is the only committed state it can see.
        competitor = _CompetingAmendment(second, tenant_a_id, entry_id, _STATE_C)
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, waiting_pid=second_pid, holding_pid=first_pid)

        first.commit()
        competitor.join(_THREAD_JOIN_SECONDS)
        assert not competitor.is_alive(), "the competing amendment never finished"
        if competitor.error is not None:
            raise competitor.error
        assert competitor.isolation == _ISOLATION_LEVEL
    finally:
        # Release session one first so a still-waiting competitor is unblocked
        # at once; only then close its session, from this thread.
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    final = _stored(db_connection, tenant_a_id, entry_id)
    assert _business_values(final) == _STATE_C

    # Checked first and separately: a stale before_state is hashed and linked
    # correctly, so the chain verifies in the broken implementation too.
    _assert_chain_valid(db_connection, tenant_a_id)

    entries = _amendments(db_connection, tenant_a_id, entry_id)
    assert len(entries) == 2
    recorded = [(_business_part(e.before_state), _business_part(e.after_state)) for e in entries]
    assert recorded[0] == (_as_ledgered(_STATE_A), _as_ledgered(_STATE_B))
    assert recorded[1] == (_as_ledgered(_STATE_B), _as_ledgered(_STATE_C)), (
        "the chain verified, yet the second amendment did not record B, the committed row it replaced: "
        f"before_state.provider_name={entries[1].before_state['provider_name']!r}"
    )
    # Every field, ids and timestamps included, against rows read back separately.
    assert entries[0].before_state == _ledger_state(state_a)
    assert entries[0].after_state == _ledger_state(state_b)
    assert entries[1].before_state == _ledger_state(state_b)
    assert entries[1].after_state == _ledger_state(final)
    assert entries[1].before_state == entries[0].after_state


# ── Ledger timestamps do not depend on the session's time zone ──────────────


@pytest.mark.integration
def test_consecutive_amendments_chain_their_states_in_utc_whatever_the_session_time_zone(db_connection, tenant_a_id):
    entry_id = _create(db_connection, tenant_a_id, _STATE_A)
    session = _session(_LOCK_TIMEOUT_MS)
    try:
        update_register_entry(session, tenant_a_id, entry_id, RegisterEntryInput(**_STATE_B),
                              actor_id=_ACTOR, actor_role=_ROLE)
        session.commit()
        update_register_entry(session, tenant_a_id, entry_id, RegisterEntryInput(**_STATE_C),
                              actor_id=_ACTOR, actor_role=_ROLE)
        session.commit()
    finally:
        _close_quietly(session)

    entries = _amendments(db_connection, tenant_a_id, entry_id)
    assert len(entries) == 2
    states = [state for entry in entries for state in (entry.before_state, entry.after_state)]
    for state in states:
        for field in _TIMESTAMP_FIELDS:
            assert state[field].endswith(_UTC_SUFFIX), f"{field}={state[field]!r} is not rendered in UTC"
    assert len({state["created_at"] for state in states}) == 1, "created_at never changes, so it must read the same"
    assert entries[1].before_state == entries[0].after_state
    assert entries[1].after_state == _ledger_state(_stored(db_connection, tenant_a_id, entry_id))


# ── A failed ledger append takes the amendment with it ──────────────────────


@pytest.mark.integration
def test_a_failed_ledger_append_rolls_back_the_amendment_and_releases_the_row(
    db_connection, tenant_a_id, monkeypatch,
):
    entry_id = _create(db_connection, tenant_a_id, _STATE_A)
    state_a = _stored(db_connection, tenant_a_id, entry_id)
    rows_before = _audit_row_count(db_connection, tenant_a_id)

    def failing_append(*args, **kwargs):
        raise RuntimeError("simulated ledger failure")

    monkeypatch.setattr(dora_roi_service, "append_audit_entry", failing_append)
    with pytest.raises(RuntimeError, match="simulated ledger failure"):
        with db_connection.transaction():
            update_register_entry(db_connection, tenant_a_id, entry_id, RegisterEntryInput(**_STATE_B),
                                  actor_id=_ACTOR, actor_role=_ROLE)
    monkeypatch.undo()

    assert _ledger_state(_stored(db_connection, tenant_a_id, entry_id)) == _ledger_state(state_a)
    assert _audit_row_count(db_connection, tenant_a_id) == rows_before
    _assert_lockable_now(tenant_a_id, entry_id)
    _assert_chain_valid(db_connection, tenant_a_id)


# ── Tenant isolation ────────────────────────────────────────────────────────


@pytest.mark.integration
def test_an_amendment_neither_changes_nor_locks_another_tenants_entry(db_connection, tenant_a_id, tenant_b_id):
    other_tenants_entry = _create(db_connection, tenant_b_id, _STATE_A)
    state_before = _stored(db_connection, tenant_b_id, other_tenants_entry)
    rows_a, rows_b = _audit_row_count(db_connection, tenant_a_id), _audit_row_count(db_connection, tenant_b_id)

    with db_connection.transaction():
        result = update_register_entry(
            db_connection, tenant_a_id, other_tenants_entry, RegisterEntryInput(**_STATE_C),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
        # Still inside tenant A's transaction: it must hold no lock on tenant B's row.
        _assert_lockable_now(tenant_b_id, other_tenants_entry)

    assert result is None
    assert _ledger_state(_stored(db_connection, tenant_b_id, other_tenants_entry)) == _ledger_state(state_before)
    assert _audit_row_count(db_connection, tenant_a_id) == rows_a
    assert _audit_row_count(db_connection, tenant_b_id) == rows_b


@pytest.mark.integration
def test_patching_another_tenants_entry_through_the_route_is_a_404_that_writes_nothing(
    db_connection, tenant_a_id, tenant_b_id,
):
    other_tenants_entry = _create(db_connection, tenant_b_id, _STATE_A)
    state_before = _stored(db_connection, tenant_b_id, other_tenants_entry)
    rows_a = _audit_row_count(db_connection, tenant_a_id)
    app = create_app()

    def _conn():
        yield db_connection

    app.dependency_overrides[get_conn] = _conn
    app.dependency_overrides[get_tenant_id] = lambda: str(tenant_a_id)
    app.dependency_overrides[get_reviewer_id] = lambda: _ACTOR
    app.dependency_overrides[get_role] = lambda: _ROLE
    body = dict(_STATE_C)
    response = TestClient(app).patch(f"/api/v1/register/entries/{other_tenants_entry}", json=body)

    assert response.status_code == 404
    assert _ledger_state(_stored(db_connection, tenant_b_id, other_tenants_entry)) == _ledger_state(state_before)
    assert _audit_row_count(db_connection, tenant_a_id) == rows_a


# ── Reads stay non-locking ──────────────────────────────────────────────────


@pytest.mark.integration
def test_reads_neither_wait_for_an_amendment_in_flight_nor_take_a_row_lock(db_connection, tenant_a_id):
    entry_id = _create(db_connection, tenant_a_id, _STATE_A)
    writer = reader = None
    try:
        writer = _session(_LOCK_TIMEOUT_MS)
        reader = _session(_PROBE_LOCK_TIMEOUT_MS)
        update_register_entry(writer, tenant_a_id, entry_id, RegisterEntryInput(**_STATE_B),
                              actor_id=_ACTOR, actor_role=_ROLE)

        # The amendment holds the row lock; readers neither wait for it nor see it.
        seen = get_register_entry(reader, tenant_a_id, entry_id)
        listed = [e for e in list_active_register_entries(reader, tenant_a_id) if e.register_entry_id == entry_id]
        assert seen.provider_name == _STATE_A["provider_name"]
        assert [e.provider_name for e in listed] == [_STATE_A["provider_name"]]
        reader.rollback()
        writer.rollback()

        # A transaction that has just read the entry holds no lock on it.
        get_register_entry(reader, tenant_a_id, entry_id)
        _assert_lockable_now(tenant_a_id, entry_id)
        reader.rollback()
    finally:
        _close_quietly(reader)
        _close_quietly(writer)


# ── Lock order per transaction: a cycle is possible, and it resolves cleanly ─


def _deadlock_timeout_ms(conn: _DbConnection) -> int:
    """Return the server's deadlock_timeout in milliseconds."""
    with conn.transaction():
        return int(conn.execute("SELECT setting FROM pg_settings WHERE name = 'deadlock_timeout'").fetchone()[0])


@pytest.mark.integration
def test_a_transaction_that_already_ledgered_can_deadlock_and_the_loser_leaves_nothing(db_connection, tenant_a_id):
    # Per call, the amendment's row lock precedes the tenant's ledger advisory
    # lock. Per transaction it need not: session one amends X and so holds the
    # advisory lock until it commits; session two amends Y, holds Y's row lock
    # and waits on that advisory lock; session one then amends Y and waits on
    # Y's row lock. PostgreSQL aborts one side with 40P01, whichever wait timer
    # fires first, so both outcomes are accepted with the same invariants: the
    # loser wrote neither row nor ledger entry, every committed entry's
    # before_state is the state it replaced, and the chain verifies.
    if _deadlock_timeout_ms(db_connection) >= _LOCK_TIMEOUT_MS:
        pytest.skip("deadlock_timeout is not below this test's lock_timeout")
    entry_x = _create(db_connection, tenant_a_id, _STATE_A)
    entry_y = _create(db_connection, tenant_a_id, _OTHER_STATE_A)
    state_x, state_y = _stored(db_connection, tenant_a_id, entry_x), _stored(db_connection, tenant_a_id, entry_y)
    first = second = competitor = None
    first_error: BaseException | None = None
    try:
        first = _session(_LOCK_TIMEOUT_MS)
        second = _session(_LOCK_TIMEOUT_MS)
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        update_register_entry(first, tenant_a_id, entry_x, RegisterEntryInput(**_STATE_B),
                              actor_id=_ACTOR, actor_role=_ROLE)
        competitor = _CompetingAmendment(second, tenant_a_id, entry_y, _SECOND_AMENDS_OTHER)
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, waiting_pid=second_pid, holding_pid=first_pid)
        try:
            update_register_entry(first, tenant_a_id, entry_y, RegisterEntryInput(**_FIRST_AMENDS_OTHER),
                                  actor_id=_ACTOR, actor_role=_ROLE)
            first.commit()
        except psycopg2.errors.DeadlockDetected as exc:
            first_error = exc
            first.rollback()
        competitor.join(_THREAD_JOIN_SECONDS)
        assert not competitor.is_alive(), "the competing amendment never finished"
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    losers = [error for error in (first_error, competitor.error) if error is not None]
    assert len(losers) == 1, f"expected exactly one aborted side, got {losers!r}"
    assert isinstance(losers[0], psycopg2.errors.DeadlockDetected)
    first_lost = first_error is not None

    winner_y = _SECOND_AMENDS_OTHER if first_lost else _FIRST_AMENDS_OTHER
    assert _business_values(_stored(db_connection, tenant_a_id, entry_x)) == (_STATE_A if first_lost else _STATE_B)
    assert _business_values(_stored(db_connection, tenant_a_id, entry_y)) == winner_y
    entries_x = _amendments(db_connection, tenant_a_id, entry_x)
    entries_y = _amendments(db_connection, tenant_a_id, entry_y)
    assert [_business_part(e.before_state) for e in entries_x] == ([] if first_lost else [_as_ledgered(_STATE_A)])
    assert [_business_part(e.before_state) for e in entries_y] == [_as_ledgered(_OTHER_STATE_A)]
    assert [_business_part(e.after_state) for e in entries_y] == [_as_ledgered(winner_y)]
    assert [e.before_state for e in entries_x] == ([] if first_lost else [_ledger_state(state_x)])
    assert [e.before_state for e in entries_y] == [_ledger_state(state_y)]
    _assert_chain_valid(db_connection, tenant_a_id)
