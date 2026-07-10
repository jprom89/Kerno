"""Unit tests for src/services/ai_decision_log_service.py (KER-203).

Covers the three service functions without a database:
  - emit_decision_log: tenant-context-first ordering, generated correlation_id,
    full column mapping, and the fail-closed guard (no SQL on invalid tenant);
  - query_decision_logs: every filter combination composes the right SQL with
    bound parameters only, results map to DecisionLogEntry newest-first;
  - prune_old_logs: cutoff is exactly the retention window, deletions counted
    via RETURNING, guard behaviour matches emit.

Spy connections only; live-database proof lives in
tests/integration/test_ker203_ai_decision_log.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from config.constants import AI_DECISION_LOG_RETENTION_DAYS
from src.exceptions import TenantContextMissingError
from src.services.ai_decision_log_service import (
    DecisionLogEntry,
    emit_decision_log,
    prune_old_logs,
    query_decision_logs,
)

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_CREATED_AT = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

_SAMPLE_ROW = (
    "e0000000-0000-4000-e000-000000000001",  # correlation_id
    "ctrl-001",                               # control_id
    ["rec-001", "rec-002"],                   # evidence_ids
    "a" * 64,                                 # input_snapshot_hash
    "met",                                    # output_status
    0.92,                                     # confidence_score
    "Evidence covers the control.",           # rationale_extract
    "mistral-large-latest",                   # model_version
    _CREATED_AT,                              # created_at
)


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _SpyConn:
    """Records every statement; serves canned rows for SELECT and DELETE...RETURNING."""

    def __init__(self, select_rows=None, delete_rows=None):
        self.calls: list[tuple[str, object]] = []
        self._select_rows = select_rows or []
        self._delete_rows = delete_rows or []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.strip().startswith("SELECT"):
            return _RowsResult(self._select_rows)
        if "DELETE FROM ai_decision_log" in sql:
            return _RowsResult(self._delete_rows)
        return _RowsResult([])


def _emit(spy):
    return emit_decision_log(
        spy,
        _TENANT_ID,
        control_id="ctrl-001",
        evidence_ids=["rec-001"],
        input_snapshot_hash="b" * 64,
        output_status="partial",
        confidence_score=0.55,
        rationale_extract="Partial coverage.",
        model_version="mistral-large-latest",
    )


# ── emit_decision_log ─────────────────────────────────────────────────────────


def test_emit_sets_tenant_context_before_insert():
    spy = _SpyConn()
    _emit(spy)
    assert "SET LOCAL" in spy.calls[0][0]
    assert "INSERT INTO ai_decision_log" in spy.calls[1][0]


def test_emit_binds_all_columns_and_returns_correlation_id():
    spy = _SpyConn()
    correlation_id = _emit(spy)
    params = next(p for s, p in spy.calls if "INSERT INTO ai_decision_log" in s)
    assert params["correlation_id"] == correlation_id
    assert uuid.UUID(correlation_id)  # well-formed
    assert params["tenant_id"] == str(_TENANT_ID)
    assert params["control_id"] == "ctrl-001"
    assert params["evidence_ids"] == ["rec-001"]
    assert params["input_snapshot_hash"] == "b" * 64
    assert params["output_status"] == "partial"
    assert params["confidence_score"] == 0.55
    assert params["rationale_extract"] == "Partial coverage."
    assert params["model_version"] == "mistral-large-latest"


def test_emit_invalid_tenant_raises_before_sql():
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        emit_decision_log(
            spy, None, control_id="c", evidence_ids=[], input_snapshot_hash="h",
            output_status="met", confidence_score=0.9, rationale_extract="r",
            model_version="m",
        )
    assert len(spy.calls) == 0


# ── query_decision_logs ───────────────────────────────────────────────────────


def test_query_without_filters_selects_only_by_tenant():
    spy = _SpyConn(select_rows=[_SAMPLE_ROW])
    results = query_decision_logs(spy, _TENANT_ID)
    sql, params = next((s, p) for s, p in spy.calls if s.strip().startswith("SELECT"))
    assert "control_id = :control_id" not in sql
    assert ":after" not in sql
    assert ":confidence_gte" not in sql
    assert "ORDER BY created_at DESC" in sql
    assert params == {"tenant_id": str(_TENANT_ID)}
    assert len(results) == 1


def test_query_composes_each_filter_independently():
    for kwargs, fragment, param_key in (
        ({"control_id": "ctrl-001"}, "control_id = :control_id", "control_id"),
        ({"after": _CREATED_AT}, "created_at >= :after", "after"),
        ({"confidence_gte": 0.8}, "confidence_score >= :confidence_gte", "confidence_gte"),
    ):
        spy = _SpyConn(select_rows=[])
        query_decision_logs(spy, _TENANT_ID, **kwargs)
        sql, params = next((s, p) for s, p in spy.calls if s.strip().startswith("SELECT"))
        assert fragment in sql
        assert param_key in params


def test_query_composes_all_filters_together():
    spy = _SpyConn(select_rows=[])
    query_decision_logs(
        spy, _TENANT_ID, control_id="ctrl-001", after=_CREATED_AT, confidence_gte=0.8
    )
    sql, params = next((s, p) for s, p in spy.calls if s.strip().startswith("SELECT"))
    assert "control_id = :control_id" in sql
    assert "created_at >= :after" in sql
    assert "confidence_score >= :confidence_gte" in sql
    assert params == {
        "tenant_id": str(_TENANT_ID),
        "control_id": "ctrl-001",
        "after": _CREATED_AT,
        "confidence_gte": 0.8,
    }


def test_query_maps_rows_to_entries():
    spy = _SpyConn(select_rows=[_SAMPLE_ROW])
    (entry,) = query_decision_logs(spy, _TENANT_ID)
    assert isinstance(entry, DecisionLogEntry)
    assert entry.correlation_id == _SAMPLE_ROW[0]
    assert entry.control_id == "ctrl-001"
    assert entry.evidence_ids == ["rec-001", "rec-002"]
    assert entry.output_status == "met"
    assert entry.confidence_score == pytest.approx(0.92)
    assert entry.created_at == _CREATED_AT


def test_query_sets_tenant_context_first():
    spy = _SpyConn()
    query_decision_logs(spy, _TENANT_ID)
    assert "SET LOCAL" in spy.calls[0][0]


def test_query_invalid_tenant_raises_before_sql():
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        query_decision_logs(spy, "not-a-uuid")
    assert len(spy.calls) == 0


# ── prune_old_logs ────────────────────────────────────────────────────────────


def test_prune_deletes_with_retention_cutoff_and_counts():
    spy = _SpyConn(delete_rows=[("id1",), ("id2",), ("id3",)])
    before = datetime.now(timezone.utc)
    deleted = prune_old_logs(spy, _TENANT_ID)
    after = datetime.now(timezone.utc)
    assert deleted == 3
    sql, params = next((s, p) for s, p in spy.calls if "DELETE FROM ai_decision_log" in s)
    assert "RETURNING" in sql
    assert params["tenant_id"] == str(_TENANT_ID)
    expected_low = before - timedelta(days=AI_DECISION_LOG_RETENTION_DAYS)
    expected_high = after - timedelta(days=AI_DECISION_LOG_RETENTION_DAYS)
    assert expected_low <= params["retention_cutoff"] <= expected_high


def test_prune_sets_tenant_context_first():
    spy = _SpyConn()
    prune_old_logs(spy, _TENANT_ID)
    assert "SET LOCAL" in spy.calls[0][0]


def test_prune_invalid_tenant_raises_before_sql():
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        prune_old_logs(spy, "")
    assert len(spy.calls) == 0
