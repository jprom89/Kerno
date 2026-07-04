"""Unit tests for run_recalculation_stub in src/scheduler/nightly_bias_recalculation.py (KER-114).

Seven tests prove the stub runs cleanly, emits the structured start/complete log lines,
records the run in the KER-107 audit ledger with the required fields, counts pending
overrides since the last run, never modifies a bias vector, and enforces tenant isolation.
Spy connections only; no database required.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from src.exceptions import TenantContextMissingError
from src.scheduler.nightly_bias_recalculation import run_recalculation_stub

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_PENDING_OVERRIDES = 7


# ── Test infrastructure ───────────────────────────────────────────────────────


class _RowsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _StubSpyConn:
    def __init__(self, override_count: int = _PENDING_OVERRIDES) -> None:
        self.calls: list[tuple[str, object]] = []
        self._override_count = override_count

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "count(*)" in sql and "FROM overrides" in sql:
            return _RowsResult([(self._override_count,)])
        return _RowsResult([])

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _FakeSession:
    def __init__(self, tenant_id=_TENANT_ID) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self):
        return self._tenant_id


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_stub_runs_without_error_and_reports_stub_status():
    result = run_recalculation_stub(_StubSpyConn(), _FakeSession())
    assert result.status == "stub"
    assert result.tenant_id == str(_TENANT_ID)
    assert result.override_count == _PENDING_OVERRIDES
    assert result.duration_ms >= 0


def test_stub_emits_structured_log_entries(caplog):
    with caplog.at_level(logging.INFO, logger="src.scheduler.nightly_bias_recalculation"):
        run_recalculation_stub(_StubSpyConn(), _FakeSession())
    messages = [record.getMessage() for record in caplog.records]
    started = next(m for m in messages if m.startswith("NIGHTLY_RECALCULATION started"))
    completed = next(m for m in messages if m.startswith("NIGHTLY_RECALCULATION completed"))
    assert f"tenant={_TENANT_ID}" in started
    assert f"override_count={_PENDING_OVERRIDES}" in started
    assert f"tenant={_TENANT_ID}" in completed
    assert "duration_ms=" in completed
    assert "status=stub" in completed


def test_stub_writes_audit_entry_with_required_fields():
    spy = _StubSpyConn()
    run_recalculation_stub(spy, _FakeSession())
    params = next(p for s, p in spy.calls if "INSERT INTO audit_log" in s)
    assert params["action_type"] == "nightly_recalculation_stub_ran"
    assert params["object_type"] == "bias_vector"
    assert params["object_id"] == str(_TENANT_ID)
    assert params["actor_role"] == "system"
    after_state = json.loads(params["after_state"])
    assert after_state == {
        "override_count": _PENDING_OVERRIDES,
        "status": "stub",
        "note": "full recalculation deferred to post-Sprint 1",
    }


def test_stub_does_not_modify_bias_vectors():
    spy = _StubSpyConn()
    run_recalculation_stub(spy, _FakeSession())
    for sql, _ in spy.calls:
        normalized = " ".join(sql.upper().split())
        assert "INSERT INTO RETRIEVAL_BIAS" not in normalized
        assert "UPDATE RETRIEVAL_BIAS" not in normalized
        assert "DELETE FROM RETRIEVAL_BIAS" not in normalized


def test_stub_counts_overrides_since_last_recalculation():
    spy = _StubSpyConn(override_count=0)
    result = run_recalculation_stub(spy, _FakeSession())
    assert result.override_count == 0
    count_sql, count_params = next(
        (s, p) for s, p in spy.calls if "count(*)" in s and "FROM overrides" in s
    )
    assert "last_recalculated_at" in count_sql
    assert count_params["tenant_id"] == str(_TENANT_ID)


def test_stub_sets_tenant_context_before_any_query():
    spy = _StubSpyConn()
    run_recalculation_stub(spy, _FakeSession())
    assert "SET LOCAL" in spy.calls[0][0]


def test_none_session_raises_before_sql():
    spy = _StubSpyConn()
    with pytest.raises(TenantContextMissingError):
        run_recalculation_stub(spy, None)
    assert len(spy.calls) == 0
