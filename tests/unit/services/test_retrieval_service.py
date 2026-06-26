"""Unit tests for src/services/retrieval_service.py.

Plain-English summary
---------------------
Ten tests verify the retrieval service without a live database. A spy
connection records every ``execute()`` call in order, allowing assertions about
SQL content, parameter values, and the order of operations (tenant context must
be set before any SELECT). A configurable ``_SpyConn`` returns a fake bias
vector from the ``retrieval_bias`` query when one is provided, or ``None`` to
trigger the unbiased path.

Tests cover: biased query uses ``:bias_vector`` parameter (not a column),
unbiased fallback for ``None`` and ``[]``, SET-LOCAL-before-SELECT ordering,
three invalid-tenant error cases, LIMIT value, bias coefficient value,
``tenant_embeddings`` table name, and no SQLAlchemy Session API calls.

How to run
----------
    pytest tests/unit/services/test_retrieval_service.py -v
"""

from __future__ import annotations

import uuid

import pytest

from config.constants import BIAS_INJECTION_COEFFICIENT, MAX_SIMILAR_CONTROLS_RETURNED
from src.exceptions import TenantContextMissingError
from src.services.retrieval_service import get_similar_controls

# A deterministic UUIDv4 for the test tenant — fixed for readable failure messages.
_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")

# Short float lists used as stand-ins for full-dimension vectors in unit tests.
# The spy connection does not execute real SQL, so the dimension does not matter.
_QUERY_VECTOR = [0.1, 0.2, 0.3]
_BIAS_VECTOR = [0.4, 0.5, 0.6]

# A UUIDv1 (time-based) — guaranteed not to be version 4, so it must be rejected.
_NON_V4_UUID = uuid.UUID("a0000000-0000-1000-8000-000000000001")


# ── Test infrastructure ───────────────────────────────────────────────────────


class _NullResult:
    """Simulates the return value of a non-SELECT execute call (INSERT, SET LOCAL).

    Services call ``fetchone()`` or ``fetchall()`` on every execute result. For
    non-SELECT statements these must return ``None`` / ``[]`` rather than raising.
    """

    def fetchone(self):
        """Return None — no rows from a non-SELECT statement."""
        return None

    def fetchall(self) -> list:
        """Return an empty list — no rows from a non-SELECT statement."""
        return []


class _BiasResult:
    """Simulates a retrieval_bias SELECT that returns one row containing the vector.

    ``_fetch_tenant_bias_vector`` does ``list(row[0])`` on the fetchone result.
    Returning ``(bias_vector,)`` places the vector at ``row[0]``.
    """

    def __init__(self, bias_vector: list[float]) -> None:
        """Store the fake bias vector to return from fetchone()."""
        self._bias_vector = bias_vector

    def fetchone(self):
        """Return a one-element tuple so row[0] is the bias vector."""
        return (self._bias_vector,)

    def fetchall(self) -> list:
        """Return an empty list — only fetchone is used for bias queries."""
        return []


class _SpyConn:
    """Records every execute() call; raises on SQLAlchemy Session API usage.

    When ``bias_vector`` is provided, returns it from the ``retrieval_bias``
    SELECT so the biased query path is exercised. When ``bias_vector`` is
    ``None``, all selects return ``_NullResult`` so the unbiased path is taken.
    """

    def __init__(self, bias_vector: list[float] | None = None) -> None:
        """Initialise the spy with an empty call log and optional bias vector."""
        self.calls: list[tuple[str, object]] = []
        self._bias_vector = bias_vector

    def execute(self, sql: str, params=None) -> object:
        """Record (sql, params) and return appropriate result based on query type."""
        self.calls.append((sql, params))
        if "retrieval_bias" in sql and self._bias_vector is not None:
            return _BiasResult(self._bias_vector)
        return _NullResult()

    def commit(self) -> None:
        """No-op — unit tests do not require real transaction control."""

    def rollback(self) -> None:
        """No-op — unit tests do not require real transaction control."""

    def add(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError(
            "conn.add() was called. The retrieval service must use "
            "conn.execute(sql, params) — not the SQLAlchemy Session API."
        )

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError(
            "conn.flush() was called. The retrieval service must use "
            "conn.execute(sql, params) — not the SQLAlchemy Session API."
        )


class _FakeSession:
    """Minimal session that supplies a tenant UUID via resolve_tenant_id().

    ``resolve_and_set_tenant_context()`` reads the tenant identity by calling
    ``session.resolve_tenant_id()``. This class implements that interface.
    """

    def __init__(self, tenant_id: uuid.UUID = _TENANT_ID) -> None:
        """Store the tenant UUID that resolve_tenant_id() will return."""
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> uuid.UUID:
        """Return the configured tenant UUID."""
        return self._tenant_id


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_biased_query_uses_bias_vector_parameter() -> None:
    """When a bias row exists, SQL must use :bias_vector as a parameter, not a column.

    The dead-parameter bug (PROMPT_doc9 §2.2) caused the SQL to reference
    retrieval_bias_vector as a column name inside tenant_embeddings. This test
    verifies that :bias_vector appears as a bound parameter and that the column
    name never appears in the query.
    """
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    biased_sql, biased_params = spy.calls[2]
    assert ":bias_vector" in biased_sql
    assert "retrieval_bias_vector" not in biased_sql
    assert "bias_vector" in biased_params


def test_unbiased_fallback_when_no_bias_row() -> None:
    """Both None and [] bias values must trigger the unbiased similarity path.

    None means no row exists in retrieval_bias. [] means the stored vector is
    empty. Both must fall back to cosine similarity without error (§4.2).
    """
    spy_none = _SpyConn(bias_vector=None)
    get_similar_controls(_FakeSession(), spy_none, _QUERY_VECTOR)
    assert ":bias_vector" not in spy_none.calls[2][0]

    spy_empty = _SpyConn(bias_vector=[])
    get_similar_controls(_FakeSession(), spy_empty, _QUERY_VECTOR)
    assert ":bias_vector" not in spy_empty.calls[2][0]


def test_tenant_context_set_before_query() -> None:
    """SET LOCAL must be the first SQL call — tenant context before any SELECT."""
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    first_sql = spy.calls[0][0]
    assert "SET LOCAL" in first_sql
    assert all("SET LOCAL" not in call[0] for call in spy.calls[1:])


def test_none_tenant_raises_tenant_context_missing_error() -> None:
    """A session that returns None for tenant_id must raise TenantContextMissingError."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        get_similar_controls(_FakeSession(tenant_id=None), spy, _QUERY_VECTOR)
    assert len(spy.calls) == 0


def test_empty_tenant_id_raises_tenant_context_missing_error() -> None:
    """A session that returns an empty string must raise TenantContextMissingError."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        get_similar_controls(_FakeSession(tenant_id=""), spy, _QUERY_VECTOR)
    assert len(spy.calls) == 0


def test_non_v4_uuid_raises_tenant_context_missing_error() -> None:
    """A version-1 UUID must raise TenantContextMissingError — only UUIDv4 accepted."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        get_similar_controls(_FakeSession(tenant_id=_NON_V4_UUID), spy, _QUERY_VECTOR)
    assert len(spy.calls) == 0


def test_limit_respected() -> None:
    """The SQL LIMIT parameter must equal MAX_SIMILAR_CONTROLS_RETURNED."""
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    biased_params = spy.calls[2][1]
    assert biased_params["result_limit"] == MAX_SIMILAR_CONTROLS_RETURNED


def test_bias_injection_coefficient_applied() -> None:
    """The bias coefficient parameter must equal BIAS_INJECTION_COEFFICIENT, no bare float."""
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    biased_sql, biased_params = spy.calls[2]
    assert biased_params["bias_coefficient"] == BIAS_INJECTION_COEFFICIENT
    assert str(float(BIAS_INJECTION_COEFFICIENT)) not in biased_sql


def test_table_name_is_tenant_embeddings() -> None:
    """All similarity SELECTs must reference tenant_embeddings, not embeddings."""
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
    for sql, _ in spy.calls:
        if "SELECT" in sql.upper() and "control_id" in sql:
            assert "tenant_embeddings" in sql
            stripped = sql.replace("tenant_embeddings", "")
            assert "embeddings" not in stripped


def test_no_sqlalchemy_session_api_called() -> None:
    """conn.add() and conn.flush() must never be called by the retrieval service.

    _SpyConn.add() and .flush() raise AssertionError if invoked. A clean return
    from get_similar_controls() proves only the raw-connection API was used.
    """
    spy = _SpyConn(bias_vector=_BIAS_VECTOR)
    get_similar_controls(_FakeSession(), spy, _QUERY_VECTOR)
