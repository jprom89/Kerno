"""Unit tests for the real nightly bias recalculation (KER-201).

Covers the three layers without a database:
  - the pure §5.2 formula in bias_recalculation_service.py (worked example,
    zero-vector seeding, no-overrides pass-through, reviewer weighting,
    malformed-input rejection) and the pgvector text coercion (coerce_vector);
  - run_tenant_recalculation against a spy connection serving pgvector TEXT
    values, proving the fetch → recalculate → persist → ledger path end to end,
    the no-new-overrides skip, tenant-context ordering, and session validation;
  - run_nightly_bias_recalculation per-tenant failure isolation and its
    success/failure summary.

Renamed from test_nightly_recalculation_stub.py when KER-201 replaced the
Sprint 1 observe-only stub with the real recalculation.
"""

from __future__ import annotations

import contextlib
import json
import uuid

import pytest

from config.constants import DECAY_FACTOR, LEARNING_RATE
from src.exceptions import TenantContextMissingError
from src.scheduler.nightly_bias_recalculation import (
    STATUS_NO_NEW_OVERRIDES,
    STATUS_RECALCULATED,
    run_nightly_bias_recalculation,
    run_tenant_recalculation,
)
from src.services.bias_recalculation_service import (
    coerce_vector,
    recalculate_retrieval_bias,
)

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_TENANT_TWO_ID = uuid.UUID("d0000000-0000-4000-a000-000000000004")

# Worked example (LEARNING_PIPELINE_SPEC.md §5.2): current bias [1, 0], one
# senior override correcting the AI's [1, 0] recommendation to [0, 1].
#   updated = 0.85 * [1, 0] + 0.15 * 1.0 * ([0, 1] - [1, 0]) = [0.70, 0.15]
_CURRENT_BIAS_TEXT = "[1.0,0.0]"
_TARGET_TEXT = "[0.0,1.0]"
_SOURCE_TEXT = "[1.0,0.0]"
_EXPECTED_UPDATED_BIAS = [
    DECAY_FACTOR * 1.0 + LEARNING_RATE * (0.0 - 1.0),
    LEARNING_RATE * (1.0 - 0.0),
]


# ── Test infrastructure ───────────────────────────────────────────────────────


class _RowsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _RecalcSpyConn:
    """Serves canned pgvector TEXT rows for the recalculation queries; records all SQL.

    Emulating the text form psycopg2 actually returns for vector columns makes
    these tests prove the coercion path, not just the arithmetic.
    """

    def __init__(self, override_rows: list | None = None, bias_row=None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._override_rows = override_rows if override_rows is not None else []
        self._bias_row = bias_row

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "FROM overrides o" in sql:
            return _RowsResult(self._override_rows)
        if "SELECT bias_vector FROM retrieval_bias" in sql:
            return _RowsResult([self._bias_row] if self._bias_row else [])
        return _RowsResult([])

    def sql_statements(self) -> list[str]:
        return [sql for sql, _ in self.calls]


class _FakeSession:
    def __init__(self, tenant_id=_TENANT_ID) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self):
        return self._tenant_id


def _one_senior_override_row() -> tuple:
    return (1.0, _TARGET_TEXT, _SOURCE_TEXT)


def _spy_with_one_override() -> _RecalcSpyConn:
    return _RecalcSpyConn(
        override_rows=[_one_senior_override_row()], bias_row=(_CURRENT_BIAS_TEXT,)
    )


# ── coerce_vector ─────────────────────────────────────────────────────────────


def test_coerce_vector_parses_pgvector_text():
    assert coerce_vector("[1.5,2.5,3.5]") == [1.5, 2.5, 3.5]


def test_coerce_vector_handles_none_and_empty():
    assert coerce_vector(None) == []
    assert coerce_vector("[]") == []
    assert coerce_vector("") == []


def test_coerce_vector_accepts_numeric_sequences():
    assert coerce_vector([1, 2.5]) == [1.0, 2.5]
    assert coerce_vector((0.5,)) == [0.5]


def test_coerce_vector_rejects_malformed_text():
    with pytest.raises(ValueError):
        coerce_vector("[1.0,not_a_number]")


# ── The §5.2 formula ──────────────────────────────────────────────────────────


def test_formula_matches_worked_example():
    updated = recalculate_retrieval_bias(
        [1.0, 0.0],
        [{
            "reviewer_confidence_weight": 1.0,
            "target_control_vector": [0.0, 1.0],
            "source_recommendation_vector": [1.0, 0.0],
        }],
    )
    assert updated == pytest.approx(_EXPECTED_UPDATED_BIAS)


def test_new_tenant_seeds_zero_vector_from_first_override():
    updated = recalculate_retrieval_bias(
        [],
        [{
            "reviewer_confidence_weight": 1.0,
            "target_control_vector": [0.0, 1.0],
            "source_recommendation_vector": [1.0, 0.0],
        }],
    )
    # decay * 0 + learning_rate * (target - source)
    assert updated == pytest.approx([LEARNING_RATE * -1.0, LEARNING_RATE * 1.0])


def test_no_overrides_returns_current_bias_unchanged():
    assert recalculate_retrieval_bias([0.3, 0.4], []) == [0.3, 0.4]
    assert recalculate_retrieval_bias([], []) == []


def test_junior_reviewer_weight_halves_the_correction():
    senior = recalculate_retrieval_bias(
        [0.0, 0.0],
        [{
            "reviewer_confidence_weight": 1.0,
            "target_control_vector": [0.0, 1.0],
            "source_recommendation_vector": [1.0, 0.0],
        }],
    )
    junior = recalculate_retrieval_bias(
        [0.0, 0.0],
        [{
            "reviewer_confidence_weight": 0.5,
            "target_control_vector": [0.0, 1.0],
            "source_recommendation_vector": [1.0, 0.0],
        }],
    )
    assert junior == pytest.approx([component / 2 for component in senior])


def test_malformed_current_bias_raises_value_error():
    with pytest.raises(ValueError):
        recalculate_retrieval_bias(
            ["not", "numbers"],
            [{
                "reviewer_confidence_weight": 1.0,
                "target_control_vector": [0.0, 1.0],
                "source_recommendation_vector": [1.0, 0.0],
            }],
        )


# ── run_tenant_recalculation (manual trigger path) ────────────────────────────


def test_recalculation_persists_updated_bias_vector():
    spy = _spy_with_one_override()
    result = run_tenant_recalculation(spy, _FakeSession())
    assert result.status == STATUS_RECALCULATED
    assert result.override_count == 1
    assert result.dimensions == 2
    persist_params = next(p for s, p in spy.calls if "INSERT INTO retrieval_bias" in s)
    assert persist_params["bias_vector"] == pytest.approx(_EXPECTED_UPDATED_BIAS)
    assert persist_params["override_count"] == 1
    assert persist_params["tenant_id"] == str(_TENANT_ID)


def test_recalculation_writes_bias_recalculated_ledger_entry():
    spy = _spy_with_one_override()
    run_tenant_recalculation(spy, _FakeSession())
    audit_params = next(p for s, p in spy.calls if "INSERT INTO audit_log" in s)
    assert audit_params["action_type"] == "bias_recalculated"
    assert audit_params["object_type"] == "bias_vector"
    assert audit_params["object_id"] == str(_TENANT_ID)
    assert audit_params["actor_role"] == "system"
    assert audit_params["actor_id"] is None
    after_state = json.loads(audit_params["after_state"])
    assert after_state["override_count"] == 1
    assert after_state["dimensions"] == 2
    assert after_state["updated_at"]  # ISO timestamp of the persisted row


def test_no_new_overrides_skips_all_writes():
    spy = _RecalcSpyConn(override_rows=[], bias_row=(_CURRENT_BIAS_TEXT,))
    result = run_tenant_recalculation(spy, _FakeSession())
    assert result.status == STATUS_NO_NEW_OVERRIDES
    assert result.override_count == 0
    assert result.dimensions == 2
    statements = " ".join(spy.sql_statements()).upper()
    assert "INSERT INTO RETRIEVAL_BIAS" not in statements
    assert "INSERT INTO AUDIT_LOG" not in statements


def test_uncalibrated_tenant_with_no_overrides_reports_zero_dimensions():
    spy = _RecalcSpyConn(override_rows=[], bias_row=None)
    result = run_tenant_recalculation(spy, _FakeSession())
    assert result.status == STATUS_NO_NEW_OVERRIDES
    assert result.dimensions == 0


def test_sets_tenant_context_before_any_query():
    spy = _spy_with_one_override()
    run_tenant_recalculation(spy, _FakeSession())
    assert "SET LOCAL" in spy.calls[0][0]


def test_none_session_raises_before_sql():
    spy = _spy_with_one_override()
    with pytest.raises(TenantContextMissingError):
        run_tenant_recalculation(spy, None)
    assert len(spy.calls) == 0


# ── run_nightly_bias_recalculation (batch path) ───────────────────────────────


class _FailingConn(_RecalcSpyConn):
    """Raises on the overrides fetch to simulate one tenant's recalculation failing."""

    def execute(self, sql: str, params=None):
        if "FROM overrides o" in sql:
            raise RuntimeError("simulated per-tenant failure")
        return super().execute(sql, params)


class _TenantListConn(_RecalcSpyConn):
    """Serves the batch's active-tenant lookup."""

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "FROM tenants" in sql:
            return _RowsResult([(str(_TENANT_ID),), (str(_TENANT_TWO_ID),)])
        return _RowsResult([])


def _factory_from_queue(conns: list):
    """Return a db_session_factory that hands out the given connections in order."""

    @contextlib.contextmanager
    def _factory():
        yield conns.pop(0)

    return _factory


def test_batch_isolates_per_tenant_failures_and_reports_counts():
    conns = [
        _TenantListConn(),                       # active-tenant lookup
        _FailingConn(),                          # tenant one: recalculation raises
        _spy_with_one_override(),                # tenant two: succeeds
    ]
    summary = run_nightly_bias_recalculation(
        _factory_from_queue(conns), _FakeSession()
    )
    assert summary == {"success_count": 1, "failure_count": 1}


def test_batch_survives_tenant_lookup_failure():
    @contextlib.contextmanager
    def _broken_factory():
        raise RuntimeError("database unreachable")
        yield  # pragma: no cover

    summary = run_nightly_bias_recalculation(_broken_factory, _FakeSession())
    assert summary == {"success_count": 0, "failure_count": 0}
