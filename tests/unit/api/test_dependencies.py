"""Unit tests for _ExecutableConn, _convert_named_params and pooled_transaction in src/api/dependencies.py.

Proves that vector parameters are serialized as [v1,...,vN] strings and wrapped
in CAST(... AS vector), scalars use %(name)s, and _ExecutableConn routes converted SQL
with all param types through to the psycopg2 cursor correctly; and that the
SEC-REMED-001 transaction factory leases nothing until entered, then commits or
rolls back and returns its one lease exactly once.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import src.api.dependencies as dependencies
from config.constants import EMBEDDING_DIMENSION
from src.api.dependencies import (
    _ExecutableConn,
    _convert_named_params,
    get_transaction_factory,
    pooled_transaction,
)

_FULL_VECTOR = [0.1] * EMBEDDING_DIMENSION
_SHORT_VECTOR = [0.1, 0.2, 0.3]

_SIMILARITY_SQL = (
    "SELECT control_id, (embedding <=> :query_vector) AS dist "
    "FROM tenant_embeddings "
    "WHERE tenant_id = :tenant_id "
    "ORDER BY dist ASC "
    "LIMIT :result_limit"
)


def test_scalar_param_produces_percent_style():
    sql, params = _convert_named_params(
        "WHERE tenant_id = :tenant_id", {"tenant_id": "abc"}
    )
    assert "%(tenant_id)s" in sql
    assert params["tenant_id"] == "abc"


def test_vector_param_produces_cast_form():
    sql, _ = _convert_named_params(
        "embedding <=> :query_vector", {"query_vector": _FULL_VECTOR}
    )
    assert "CAST(%(query_vector)s AS vector)" in sql
    assert "::vector" not in sql


def test_vector_param_value_is_bracket_string():
    _, params = _convert_named_params(
        "embedding <=> :query_vector", {"query_vector": _FULL_VECTOR}
    )
    serialized = params["query_vector"]
    assert isinstance(serialized, str)
    assert serialized.startswith("[")
    assert serialized.endswith("]")


def test_short_list_not_treated_as_vector():
    sql, params = _convert_named_params("col = :val", {"val": _SHORT_VECTOR})
    assert "CAST(" not in sql
    assert "%(val)s" in sql
    assert params["val"] == _SHORT_VECTOR


def test_double_colon_cast_in_sql_template_preserved():
    # ::uuid already present in the SQL template must not be mangled.
    sql, _ = _convert_named_params(
        "current_setting('app.tenant', true)::uuid = :tenant_id",
        {"tenant_id": "abc"},
    )
    assert "::uuid" in sql
    assert "%(tenant_id)s" in sql


def test_full_similarity_query_with_all_param_types():
    rows = [("c1", 0.1), ("c2", 0.2), ("c3", 0.3)]
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows
    mock_raw_conn = MagicMock()
    mock_raw_conn.cursor.return_value = mock_cursor

    conn = _ExecutableConn(mock_raw_conn)
    result = conn.execute(
        _SIMILARITY_SQL,
        {"query_vector": _FULL_VECTOR, "tenant_id": "t-uuid", "result_limit": 5},
    )

    call_sql, call_params = mock_cursor.execute.call_args[0]
    assert "CAST(%(query_vector)s AS vector)" in call_sql
    assert "%(tenant_id)s" in call_sql
    assert "%(result_limit)s" in call_sql
    assert "ORDER BY" in call_sql
    assert isinstance(call_params["query_vector"], str)
    assert call_params["query_vector"].startswith("[")
    assert call_params["tenant_id"] == "t-uuid"
    assert call_params["result_limit"] == 5
    assert result.fetchall() == rows


# ── pooled_transaction (SEC-REMED-001) ────────────────────────────────────────


class _RecordingRawConn:
    def __init__(self) -> None:
        self.events: list[str] = []

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")


class _RecordingPool:
    def __init__(self) -> None:
        self.raw = _RecordingRawConn()
        self.getconn_calls = 0
        self.returned: list[object] = []

    def getconn(self):
        self.getconn_calls += 1
        return self.raw

    def putconn(self, conn) -> None:
        self.returned.append(conn)


class _Abort(BaseException):
    pass


@pytest.fixture
def pool(monkeypatch) -> _RecordingPool:
    recording = _RecordingPool()
    monkeypatch.setattr(dependencies, "_pool", recording)
    return recording


def test_the_transaction_factory_dependency_leases_nothing(pool):
    factory = get_transaction_factory()
    assert factory is pooled_transaction
    pending = factory()
    assert pool.getconn_calls == 0, "creating the context manager must not lease"
    with pending:
        assert pool.getconn_calls == 1


def test_a_clean_block_commits_then_returns_the_lease_once(pool):
    with pooled_transaction() as conn:
        assert isinstance(conn, _ExecutableConn)
    assert pool.raw.events == ["commit"]
    assert pool.returned == [pool.raw]


@pytest.mark.parametrize("failure", [RuntimeError("write failed"), HTTPException(status_code=422), _Abort()])
def test_any_exception_rolls_back_then_returns_the_lease_once_and_propagates(pool, failure):
    with pytest.raises(type(failure)):
        with pooled_transaction():
            raise failure
    assert pool.raw.events == ["rollback"]
    assert pool.returned == [pool.raw]


def test_a_failed_commit_rolls_back_and_still_returns_the_lease_once(pool):
    def failing_commit():
        pool.raw.events.append("commit")
        raise RuntimeError("commit failed")

    pool.raw.commit = failing_commit
    with pytest.raises(RuntimeError, match="commit failed"):
        with pooled_transaction():
            pass
    assert pool.raw.events == ["commit", "rollback"]
    assert pool.returned == [pool.raw]
