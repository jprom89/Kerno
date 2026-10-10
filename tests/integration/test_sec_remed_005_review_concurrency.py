"""SEC-REMED-005: approval and replacement of a recommendation under real concurrency, two READ COMMITTED sessions.

Synchronisation is explicit: the main thread waits under a deadline until pg_blocking_pids() shows one session
blocked on the other's review lock, or until a mocked LLM call is provably in flight, and the LLM is never called
for real. Run: pytest tests/integration/test_sec_remed_005_review_concurrency.py --require-live-database -m integration
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.errors
import pytest

from src.exceptions import StaleRecommendationError
from src.services.audit_log import verify_audit_chain
from src.services.coverage_service import get_coverage_controls
from src.services.override_service import OverrideInput, capture_override
from src.services.recommendation_service import (
    acquire_control_review_lock,
    generate_recommendation,
    list_open_recommendations,
)
from tests.conftest import _DbConnection

_CATEGORY = "sec_remed_005_concurrency"
_CONTROL = "c5050000-0000-4000-c000-000000000101"
_OTHER_CONTROL = "c5050000-0000-4000-c000-000000000102"
_RECORD = "c5050000-0000-4000-c000-000000000111"
_LINK = "c5050000-0000-4000-c000-000000000112"
_REVIEWER = uuid.UUID("d5050000-0000-4000-d000-000000000002")
_ROLE = "compliance_lead"
_QUEUE_PAGE_SIZE = 100

_MET_RELEVANCE = 0.9
_GAP_RELEVANCE = 0.2

_ISOLATION_LEVEL = "read committed"
_LOCK_TIMEOUT_MS = 10_000
_STATEMENT_TIMEOUT_FACTOR = 2
# Short enough that a review lock held by the other session would fail the
# step outright instead of quietly waiting for it.
_MUST_NOT_WAIT_LOCK_TIMEOUT_MS = 500
_SYNC_DEADLINE_SECONDS = 10.0
_SYNC_POLL_SECONDS = 0.01
_THREAD_JOIN_SECONDS = 15.0


class _Session:
    def __init__(self, tenant_id) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self):
        return self._tenant_id


@pytest.fixture
def concurrency_seed(db_connection, tenant_a_id, tenant_b_id, monkeypatch):
    """Seed two catalogue controls and one met-scored Tenant A evidence link; remove everything afterwards.

    The template rationale path is forced unless a test patches in a mocked
    LLM. recommendations and links are outside the shared teardown, and bound
    decisions must go first.
    """
    monkeypatch.delenv("KERNO_LLM_MODEL", raising=False)
    _seed_controls_and_evidence(db_connection, tenant_a_id)
    yield
    _remove_seeded_rows(db_connection, (tenant_a_id, tenant_b_id))


def _seed_controls_and_evidence(db_connection, tenant_a_id) -> None:
    with db_connection.transaction():
        for control_id, ref in ((_CONTROL, "SR005-C1"), (_OTHER_CONTROL, "SR005-C2")):
            db_connection.execute(
                """INSERT INTO compliance_controls
                   (control_id, framework, control_ref, category, title,
                    obligation_text, entity_types, is_active)
                   VALUES (%s, 'NIS2', %s, %s, 'Review concurrency test control',
                           'Test obligation.', %s, TRUE)
                   ON CONFLICT (control_id) DO NOTHING""",
                [control_id, ref, _CATEGORY, ["essential"]],
            )
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        db_connection.execute(
            """INSERT INTO context_records
               (record_id, tenant_id, source_system, record_type, title, body)
               VALUES (%s, %s, 'confluence', 'policy', 'Backup policy', 'Reviewed backup policy.')""",
            [_RECORD, str(tenant_a_id)],
        )
        db_connection.execute(
            """INSERT INTO control_evidence_links
               (link_id, control_id, record_id, linked_by, linked_at, relevance_score)
               VALUES (%s, %s, %s, 'integration-test', now(), %s)""",
            [_LINK, _CONTROL, _RECORD, _MET_RELEVANCE],
        )


def _remove_seeded_rows(db_connection, tenant_ids) -> None:
    db_connection.rollback()
    for tenant_id in tenant_ids:
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
            db_connection.execute(
                "DELETE FROM overrides WHERE original_control_id IN (%s, %s)", [_CONTROL, _OTHER_CONTROL]
            )
            db_connection.execute(
                "DELETE FROM recommendations WHERE control_id IN (%s, %s)", [_CONTROL, _OTHER_CONTROL]
            )
            db_connection.execute("DELETE FROM control_evidence_links WHERE link_id = %s", [_LINK])
            db_connection.execute("DELETE FROM context_records WHERE record_id = %s", [_RECORD])
    with db_connection.transaction():
        db_connection.execute(
            "DELETE FROM compliance_controls WHERE control_id IN (%s, %s)", [_CONTROL, _OTHER_CONTROL]
        )


# ── Sessions and synchronisation (mirrors test_dora_v2_002a_concurrency.py) ──


def _session(lock_timeout_ms: int = _LOCK_TIMEOUT_MS) -> _DbConnection:
    """Open an independent READ COMMITTED session with bounded lock and statement waits."""
    raw = psycopg2.connect(os.environ["DATABASE_URL"])
    raw.set_session(isolation_level="READ COMMITTED", autocommit=False)
    conn = _DbConnection(raw)
    conn.execute("SET lock_timeout = %s", [f"{lock_timeout_ms}ms"])
    conn.execute("SET statement_timeout = %s", [f"{lock_timeout_ms * _STATEMENT_TIMEOUT_FACTOR}ms"])
    conn.commit()
    return conn


def _close_quietly(conn: _DbConnection | None) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    except psycopg2.Error:
        pass
    finally:
        conn._conn.close()


def _backend_pid(conn: _DbConnection) -> int:
    pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    conn.commit()
    return pid


def _isolation_level(conn: _DbConnection) -> str:
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
    """Block until the catalog shows waiting_pid waiting on a lock held by holding_pid, or fail with the cause."""
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
    raise AssertionError(f"backend {waiting_pid} never blocked on {holding_pid} within {_SYNC_DEADLINE_SECONDS}s")


def _finish(thread: threading.Thread) -> None:
    thread.join(_THREAD_JOIN_SECONDS)
    assert not thread.is_alive(), "the competing session never finished"


# ── Domain helpers ──────────────────────────────────────────────────────────


def _generate(conn, tenant_id, control_id: str = _CONTROL):
    return generate_recommendation(
        conn, tenant_id, control_id, triggered_by_user_id=str(_REVIEWER), triggered_by_role=_ROLE
    )


def _approve(conn, tenant_id, recommendation_id: str, control_id: str = _CONTROL):
    return capture_override(
        _Session(tenant_id),
        conn,
        OverrideInput(
            reviewer_id=_REVIEWER,
            reviewer_role="vciso",
            action_type="approve",
            original_control_id=control_id,
            recommendation_id=recommendation_id,
        ),
    )


def _committed_generation(conn, tenant_id, control_id: str = _CONTROL):
    with conn.transaction():
        return _generate(conn, tenant_id, control_id)


def _set_relevance(conn, tenant_id, score: float) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        conn.execute("UPDATE control_evidence_links SET relevance_score = %s WHERE link_id = %s", [score, _LINK])


def _recommendation_rows(conn, tenant_id, control_id: str = _CONTROL) -> dict[str, bool]:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        rows = conn.execute(
            "SELECT recommendation_id, is_superseded FROM recommendations WHERE control_id = %s",
            [control_id],
        ).fetchall()
    return {str(row[0]): row[1] for row in rows}


def _bound_decisions(conn, tenant_id, control_id: str = _CONTROL) -> list[str | None]:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        rows = conn.execute(
            "SELECT recommendation_id FROM overrides WHERE original_control_id = %s ORDER BY created_at",
            [control_id],
        ).fetchall()
    return [str(row[0]) if row[0] is not None else None for row in rows]


def _decision_events(conn, tenant_id, control_id: str = _CONTROL) -> list[dict]:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        rows = conn.execute(
            """SELECT before_state FROM audit_log
               WHERE object_type = 'override' AND control_id = %s ORDER BY sequence_number""",
            [control_id],
        ).fetchall()
    return [row[0] for row in rows]


def _coverage_row(conn, tenant_id, control_id: str = _CONTROL):
    with conn.transaction():
        controls = get_coverage_controls(conn, tenant_id, category=_CATEGORY)
    return next(control for control in controls if control.control_id == control_id)


def _queue_ids(conn, tenant_id, control_id: str = _CONTROL) -> list[str]:
    with conn.transaction():
        items, _ = list_open_recommendations(conn, tenant_id, 1, _QUEUE_PAGE_SIZE)
    return [item.recommendation_id for item in items if item.control_id == control_id]


def _assert_chain_valid(conn, tenant_id) -> None:
    with conn.transaction():
        chain = verify_audit_chain(conn, tenant_id)
    assert chain.is_valid, chain.failure_reason


def _assert_r2_is_current_unconfirmed_and_open(conn, tenant_id, r1_id: str, r2_id: str) -> None:
    assert _recommendation_rows(conn, tenant_id) == {r1_id: True, r2_id: False}
    coverage = _coverage_row(conn, tenant_id)
    assert (coverage.recommendation_id, coverage.human_confirmed) == (r2_id, False)
    assert _queue_ids(conn, tenant_id) == [r2_id]


def _waiting_llm_client(entered: threading.Event, release: threading.Event) -> MagicMock:
    def complete(**_kwargs):
        entered.set()
        assert release.wait(_SYNC_DEADLINE_SECONDS), "the test never released the LLM call"
        choice = MagicMock()
        choice.message.content = json.dumps(
            {"rationale": "The backup policy is out of scope.", "own_status": "gap", "own_confidence": 0.3}
        )
        return MagicMock(choices=[choice])

    client = MagicMock()
    client.chat.complete.side_effect = complete
    return client


def _advisory_locks_held_by(monitor: _DbConnection, pid: int) -> int:
    with monitor.transaction():
        return monitor.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = %s AND granted", [pid]
        ).fetchone()[0]


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_an_r1_approval_committed_first_stays_r1s_and_leaves_the_replacement_unconfirmed(
    db_connection, tenant_a_id, concurrency_seed
):
    r1 = _committed_generation(db_connection, tenant_a_id)
    _set_relevance(db_connection, tenant_a_id, _GAP_RELEVANCE)
    first = second = competitor = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        assert _isolation_level(first) == _ISOLATION_LEVEL
        _approve(first, tenant_a_id, r1.recommendation_id)
        competitor = _InSession(second, lambda conn: _generate(conn, tenant_a_id))
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, second_pid, first_pid)
        first.commit()
        _finish(competitor)
        if competitor.error is not None:
            raise competitor.error
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    r2 = competitor.result
    assert competitor.isolation == _ISOLATION_LEVEL
    assert r2.status == "gap"
    assert _bound_decisions(db_connection, tenant_a_id) == [r1.recommendation_id]
    events = _decision_events(db_connection, tenant_a_id)
    assert [event["recommendation_id"] for event in events] == [r1.recommendation_id]
    assert events[0]["recommendation_status"] == "met"
    _assert_r2_is_current_unconfirmed_and_open(db_connection, tenant_a_id, r1.recommendation_id, r2.recommendation_id)
    _assert_chain_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_a_replacement_committed_first_refuses_the_stale_r1_approval_and_writes_nothing(
    db_connection, tenant_a_id, concurrency_seed
):
    r1 = _committed_generation(db_connection, tenant_a_id)
    _set_relevance(db_connection, tenant_a_id, _GAP_RELEVANCE)
    first = second = competitor = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        r2 = _generate(first, tenant_a_id)
        competitor = _InSession(second, lambda conn: _approve(conn, tenant_a_id, r1.recommendation_id))
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, second_pid, first_pid)
        first.commit()
        _finish(competitor)
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    assert isinstance(competitor.error, StaleRecommendationError)
    assert competitor.isolation == _ISOLATION_LEVEL
    assert _bound_decisions(db_connection, tenant_a_id) == []
    assert _decision_events(db_connection, tenant_a_id) == []
    _assert_r2_is_current_unconfirmed_and_open(db_connection, tenant_a_id, r1.recommendation_id, r2.recommendation_id)
    _assert_chain_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_generation_holds_no_review_lock_while_its_llm_call_is_in_flight(
    db_connection, tenant_a_id, concurrency_seed, monkeypatch
):
    r1 = _committed_generation(db_connection, tenant_a_id)
    _set_relevance(db_connection, tenant_a_id, _GAP_RELEVANCE)
    monkeypatch.setenv("KERNO_LLM_MODEL", "mistral-large-latest")
    entered, release = threading.Event(), threading.Event()
    first = second = generation = None
    try:
        with patch(
            "src.services.recommendation_service.get_llm_client",
            return_value=_waiting_llm_client(entered, release),
        ):
            first, second = _session(), _session(_MUST_NOT_WAIT_LOCK_TIMEOUT_MS)
            first_pid = _backend_pid(first)
            generation = _InSession(first, lambda conn: _generate(conn, tenant_a_id))
            generation.start()
            assert entered.wait(_SYNC_DEADLINE_SECONDS), "generation never reached its LLM call"
            assert _advisory_locks_held_by(db_connection, first_pid) == 0
            _approve(second, tenant_a_id, r1.recommendation_id)
            second.commit()
            release.set()
            _finish(generation)
            if generation.error is not None:
                raise generation.error
    finally:
        release.set()
        _close_quietly(second)
        if generation is not None:
            generation.join(_THREAD_JOIN_SECONDS)
        _close_quietly(first)

    r2 = generation.result
    assert r2.input_snapshot["rationale_source"] == "llm"
    assert _bound_decisions(db_connection, tenant_a_id) == [r1.recommendation_id]
    _assert_r2_is_current_unconfirmed_and_open(db_connection, tenant_a_id, r1.recommendation_id, r2.recommendation_id)
    _assert_chain_valid(db_connection, tenant_a_id)


@pytest.mark.integration
def test_concurrent_generations_serialise_and_leave_exactly_one_current_recommendation(
    db_connection, tenant_a_id, concurrency_seed
):
    r1 = _committed_generation(db_connection, tenant_a_id)
    first = second = competitor = None
    try:
        first, second = _session(), _session()
        first_pid, second_pid = _backend_pid(first), _backend_pid(second)
        r2 = _generate(first, tenant_a_id)
        competitor = _InSession(second, lambda conn: _generate(conn, tenant_a_id))
        competitor.start()
        _wait_until_blocked_by(db_connection, competitor, second_pid, first_pid)
        first.commit()
        _finish(competitor)
        if competitor.error is not None:
            raise competitor.error
    finally:
        _close_quietly(first)
        if competitor is not None:
            competitor.join(_THREAD_JOIN_SECONDS)
        _close_quietly(second)

    r3 = competitor.result
    assert _recommendation_rows(db_connection, tenant_a_id) == {
        r1.recommendation_id: True, r2.recommendation_id: True, r3.recommendation_id: False,
    }
    assert _queue_ids(db_connection, tenant_a_id) == [r3.recommendation_id]
    assert _coverage_row(db_connection, tenant_a_id).recommendation_id == r3.recommendation_id


def _approve_while_another_session_holds_the_review_lock(db_connection, tenant_a_id, r1_id, holder_tenant, holder_control):
    first = second = None
    try:
        first, second = _session(), _session(_MUST_NOT_WAIT_LOCK_TIMEOUT_MS)
        first_pid = _backend_pid(first)
        acquire_control_review_lock(first, holder_tenant, holder_control)
        assert _advisory_locks_held_by(db_connection, first_pid) == 1
        _approve(second, tenant_a_id, r1_id)
        second.commit()
    finally:
        _close_quietly(first)
        _close_quietly(second)


@pytest.mark.integration
@pytest.mark.parametrize("holder", ["other_control", "other_tenant"])
def test_the_review_lock_of_another_control_or_tenant_does_not_delay_a_decision(
    db_connection, tenant_a_id, tenant_b_id, concurrency_seed, holder
):
    r1 = _committed_generation(db_connection, tenant_a_id)
    holder_tenant, holder_control = (
        (tenant_a_id, _OTHER_CONTROL) if holder == "other_control" else (tenant_b_id, _CONTROL)
    )

    _approve_while_another_session_holds_the_review_lock(
        db_connection, tenant_a_id, r1.recommendation_id, holder_tenant, holder_control
    )

    assert _bound_decisions(db_connection, tenant_a_id) == [r1.recommendation_id]
    assert _coverage_row(db_connection, tenant_a_id).human_confirmed is True
    assert _queue_ids(db_connection, tenant_a_id) == []


@pytest.mark.integration
def test_a_decision_that_times_out_on_the_same_review_lock_writes_nothing(
    db_connection, tenant_a_id, concurrency_seed
):
    r1 = _committed_generation(db_connection, tenant_a_id)

    with pytest.raises(psycopg2.errors.LockNotAvailable):
        _approve_while_another_session_holds_the_review_lock(
            db_connection, tenant_a_id, r1.recommendation_id, tenant_a_id, _CONTROL
        )

    assert _bound_decisions(db_connection, tenant_a_id) == []
    assert _decision_events(db_connection, tenant_a_id) == []
    assert _queue_ids(db_connection, tenant_a_id) == [r1.recommendation_id]
