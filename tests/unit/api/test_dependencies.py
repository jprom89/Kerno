"""Unit tests for _ExecutableConn and _convert_named_params in src/api/dependencies.py.

Six tests prove that vector parameters are serialized as [v1,...,vN] strings and wrapped
in CAST(... AS vector), scalars use %(name)s, and _ExecutableConn routes converted SQL
with all param types through to the psycopg2 cursor correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from config.constants import EMBEDDING_DIMENSION
from src.api.dependencies import (
    _ExecutableConn,
    _convert_named_params,
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
