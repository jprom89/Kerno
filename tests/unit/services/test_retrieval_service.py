"""Unit tests for src/services/retrieval_service.py — get_similar_controls and retrieve_similar_records.

Seventeen tests cover both retrieval paths without a live database. Spy connections record
execute() calls so tests can assert SQL content, parameter values, and call ordering.
"""

from __future__ import annotations

import uuid

import pytest

from config.constants import (
    BIAS_INJECTION_COEFFICIENT,
    MAX_SIMILAR_CONTROLS_RETURNED,
    MAX_SIMILAR_RECORDS_RETURNED,
)
from src.exceptions import TenantContextMissingError
from src.services.retrieval_service import get_similar_controls, retrieve_similar_records

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_QUERY_VECTOR = [0.1, 0.2, 0.3]
_BIAS_VECTOR = [0.4, 0.5, 0.6]
_NON_V4_UUID = uuid.UUID("a0000000-0000-1000-8000-000000000001")


# ── Test infrastructure ───────────────────────────────────────────────────────


class _NullResult:
    def fetchone(self):
        return None

    def fetchall(self) -> list:
        return []


class _SelectResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _BiasResult:
    def __init__(self, bias_vector: list[float]) -> None:
        self._bias_vector = bias_vector

    def fetchone(self):
        return (self._bias_vector,)

    def fetchall(self) -> list:
        return []


class _SpyConn:
    """Records execute() calls for get_similar_controls tests; raises on SQLAlchemy Session API."""

    def __init__(self, bias_vector: list[float] | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._bias_vector = bias_vector

    def execute(self, sql: str, params=None) -> object:
        self.calls.append((sql, params))
        if "retrieval_bias" in sql and self._bias_vector is not None:
            return _BiasResult(self._bias_vector)
        return _NullResult()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def add(self, *args, **kwargs) -> None:
        raise AssertionError(
            "conn.add() was called. The retrieval service must use "
            "conn.execute(sql, params) — not the SQLAlchemy Session API."
        )

    def flush(self, *args, **kwargs) -> None:
        raise AssertionError(
            "conn.flush() was called. The retrieval service must use "
            "conn.execute(sql, params) — not the SQLAlchemy Session API."
        )


class _ContextSpyConn:
    """Records execute() calls for retrieve_similar_records tests."""

    def __init__(self, rows: list | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self._rows = rows or []

    def execute(self, sql: str, params=None) -> object:
        self.calls.append((sql, params))
        if "context_records" in sql and self._rows:
            return _SelectResult(self._rows)
        return _NullResult()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def add(self, *args, **kwargs) -> None:
        raise AssertionError("conn.add() called — retrieve_similar_records must use conn.execute()")

    def flush(self, *args, **kwargs) -> None:
        raise AssertionError("conn.flush() called — retrieve_similar_records must use conn.execute()")


class _FakeSession:
    def __init__(self, tenant_id: uuid.UUID = _TENANT_ID) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> uuid.UUID:
        return self._tenant_id


# ── get_similar_controls tests ────────────────────────────────────────────────


def test_biased_query_uses_bias_vector_parameter() -> None:
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    biased_sql, biased_params = spy.calls[2]
    assert ":bias_vector" in biased_sql
    assert "retrieval_bias_vector" not in biased_sql
    assert "bias_vector" in biased_params


def test_unbiased_fallback_when_no_bias_row() -> None:
    spy_none = _SpyConn(bias_vector=None)
    get_similar_controls(_FakeSession(), spy_none, _QUERY_VECTOR)
    assert ":bias_vector" not in spy_none.calls[2][0]

    spy_empty = _SpyConn(bias_vector=[])
    get_similar_controls(_FakeSession(), spy_empty, _QUERY_VECTOR)
    assert ":bias_vector" not in spy_empty.calls[2][0]


def test_tenant_context_set_before_query() -> None:
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    first_sql = spy.calls[0][0]
    assert "SET LOCAL" in first_sql
    assert all("SET LOCAL" not in call[0] for call in spy.calls[1:])


def test_none_tenant_raises_tenant_context_missing_error() -> None:
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        get_similar_controls(_FakeSession(tenant_id=None), spy, _QUERY_VECTOR)
    assert len(spy.calls) == 0


def test_empty_tenant_id_raises_tenant_context_missing_error() -> None:
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        get_similar_controls(_FakeSession(tenant_id=""), spy, _QUERY_VECTOR)
    assert len(spy.calls) == 0


def test_non_v4_uuid_raises_tenant_context_missing_error() -> None:
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        get_similar_controls(_FakeSession(tenant_id=_NON_V4_UUID), spy, _QUERY_VECTOR)
    assert len(spy.calls) == 0


def test_limit_respected() -> None:
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    biased_params = spy.calls[2][1]
    assert biased_params["result_limit"] == MAX_SIMILAR_CONTROLS_RETURNED


def test_bias_injection_coefficient_applied() -> None:
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    biased_sql, biased_params = spy.calls[2]
    assert biased_params["bias_coefficient"] == BIAS_INJECTION_COEFFICIENT
    assert str(float(BIAS_INJECTION_COEFFICIENT)) not in biased_sql


def test_table_name_is_tenant_embeddings() -> None:
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    for sql, _ in spy.calls:
        if "SELECT" in sql.upper() and "control_id" in sql:
            assert "tenant_embeddings" in sql
            stripped = sql.replace("tenant_embeddings", "")
            assert "embeddings" not in stripped


def test_no_sqlalchemy_session_api_called() -> None:
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)


# ── retrieve_similar_records tests ────────────────────────────────────────────

_FAKE_CONTEXT_ROWS = [
    ("r1-uuid", "Policy Document", "Body of policy 1", 0.05),
    ("r2-uuid", "Security Guide", "Body of guide 2", 0.42),
]


def test_context_set_local_before_select() -> None:
    spy = _ContextSpyConn(rows=_FAKE_CONTEXT_ROWS)
    retrieve_similar_records(spy, _TENANT_ID, _QUERY_VECTOR)
    assert "SET LOCAL" in spy.calls[0][0]
    assert all("SET LOCAL" not in call[0] for call in spy.calls[1:])


def test_context_query_targets_context_records_table() -> None:
    spy = _ContextSpyConn()
    retrieve_similar_records(spy, _TENANT_ID, _QUERY_VECTOR)
    select_calls = [(s, p) for s, p in spy.calls if "context_records" in s]
    assert len(select_calls) == 1


def test_context_query_has_is_deleted_guard() -> None:
    spy = _ContextSpyConn()
    retrieve_similar_records(spy, _TENANT_ID, _QUERY_VECTOR)
    select_sql = next(s for s, _ in spy.calls if "context_records" in s)
    assert "is_deleted = FALSE" in select_sql


def test_context_query_has_embedding_not_null_guard() -> None:
    spy = _ContextSpyConn()
    retrieve_similar_records(spy, _TENANT_ID, _QUERY_VECTOR)
    select_sql = next(s for s, _ in spy.calls if "context_records" in s)
    assert "embedding IS NOT NULL" in select_sql


def test_context_query_limit_uses_bound_param() -> None:
    spy = _ContextSpyConn()
    retrieve_similar_records(spy, _TENANT_ID, _QUERY_VECTOR, limit=3)
    _, params = next((s, p) for s, p in spy.calls if "context_records" in s)
    assert params["result_limit"] == 3


def test_context_query_default_limit_is_constant() -> None:
    spy = _ContextSpyConn()
    retrieve_similar_records(spy, _TENANT_ID, _QUERY_VECTOR)
    _, params = next((s, p) for s, p in spy.calls if "context_records" in s)
    assert params["result_limit"] == MAX_SIMILAR_RECORDS_RETURNED


def test_context_query_returns_dicts_with_correct_keys() -> None:
    spy = _ContextSpyConn(rows=_FAKE_CONTEXT_ROWS)
    results = retrieve_similar_records(spy, _TENANT_ID, _QUERY_VECTOR)
    assert len(results) == 2
    assert results[0] == {
        "record_id": "r1-uuid",
        "title": "Policy Document",
        "body": "Body of policy 1",
        "distance": 0.05,
    }


def test_context_query_none_tenant_raises() -> None:
    spy = _ContextSpyConn()
    with pytest.raises(TenantContextMissingError):
        retrieve_similar_records(spy, None, _QUERY_VECTOR)
    assert len(spy.calls) == 0
