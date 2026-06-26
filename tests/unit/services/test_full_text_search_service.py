"""Unit tests for src/services/full_text_search_service.py.

Plain-English summary
---------------------
Seven tests verify the full-text search service without a live database. A spy
connection records every execute() call. Tests cover: the plainto_tsquery
expression appearing in SQL, source_system and record_type filter clauses,
the is_deleted = FALSE guard, the LIMIT :limit binding, SET LOCAL ordering,
and the raw-connection contract (no SQLAlchemy Session API).

How to run
----------
    pytest tests/unit/services/test_full_text_search_service.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from config.constants import FULL_TEXT_SEARCH_LIMIT
from src.exceptions import TenantContextMissingError
from src.services.full_text_search_service import search_records

# A deterministic UUIDv4 tenant for all tests.
_TENANT_ID = "c0000000-0000-4000-a000-000000000088"
_NOW = datetime.now(timezone.utc)


# ── Test infrastructure ───────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT or empty SELECT result."""

    def fetchone(self):
        """Return None."""
        return None

    def fetchall(self) -> list:
        """Return an empty list."""
        return []


class _SelectResult:
    """Simulates a SELECT result returning a fixed list of row tuples."""

    def __init__(self, rows: list) -> None:
        """Store the rows to return."""
        self._rows = rows

    def fetchone(self):
        """Return the first row, or None."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        """Return all rows."""
        return self._rows


class _SpyConn:
    """Records execute() calls; raises on SQLAlchemy Session API usage."""

    def __init__(self, responses: list[tuple[str, object]] | None = None) -> None:
        """Initialise with an empty call log and optional response configuration."""
        self.calls: list[tuple[str, object]] = []
        self._responses = responses or []

    def execute(self, sql: str, params=None) -> object:
        """Record the call and return the first matching configured response."""
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in sql:
                return result
        return _NullResult()

    def add(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.add() called — full_text_search_service must use conn.execute()")

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.flush() called — full_text_search_service must use conn.execute()")


def _make_record_row() -> tuple:
    """Return an 8-column row tuple matching the context_records query column order."""
    return (
        "record-uuid-001",
        "jira",
        "JIRA-101",
        "issue",
        "Patch deployed for CVE-2024-1234",
        "The security patch was deployed to all production instances.",
        _NOW,
        "sha256hexhashvalue",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_search_returns_matching_records() -> None:
    """Query string must appear in SQL as a plainto_tsquery call."""
    row = _make_record_row()
    spy = _SpyConn(responses=[("plainto_tsquery", _SelectResult([row]))])
    results = search_records(spy, _TENANT_ID, "security patch")
    search_calls = [(s, p) for s, p in spy.calls if "plainto_tsquery" in s]
    assert len(search_calls) == 1, "Exactly one full-text search SQL call expected"
    sql, params = search_calls[0]
    assert "plainto_tsquery" in sql, "SQL must use plainto_tsquery for full-text search"
    assert params.get("query") == "security patch"
    assert len(results) == 1
    assert results[0]["source_system"] == "jira"


def test_search_filter_source_system() -> None:
    """Providing source_system adds a source_system filter clause to the SQL."""
    spy = _SpyConn(responses=[("plainto_tsquery", _SelectResult([]))])
    search_records(spy, _TENANT_ID, "patch", source_system="jira")
    search_calls = [(s, p) for s, p in spy.calls if "plainto_tsquery" in s]
    assert len(search_calls) == 1
    sql, params = search_calls[0]
    assert "source_system = :source_system" in sql
    assert params.get("source_system") == "jira"


def test_search_filter_record_type() -> None:
    """Providing record_type adds a record_type filter clause to the SQL."""
    spy = _SpyConn(responses=[("plainto_tsquery", _SelectResult([]))])
    search_records(spy, _TENANT_ID, "issue", record_type="issue")
    search_calls = [(s, p) for s, p in spy.calls if "plainto_tsquery" in s]
    assert len(search_calls) == 1
    sql, params = search_calls[0]
    assert "record_type = :record_type" in sql
    assert params.get("record_type") == "issue"


def test_search_excludes_deleted() -> None:
    """The SQL must always include is_deleted = FALSE to exclude soft-deleted records."""
    spy = _SpyConn(responses=[("plainto_tsquery", _SelectResult([]))])
    search_records(spy, _TENANT_ID, "any query")
    search_calls = [(s, p) for s, p in spy.calls if "plainto_tsquery" in s]
    assert len(search_calls) == 1
    sql, _ = search_calls[0]
    assert "is_deleted = FALSE" in sql, "is_deleted filter must always be present"


def test_search_limit_applied() -> None:
    """The LIMIT clause must use :limit bound to FULL_TEXT_SEARCH_LIMIT by default."""
    spy = _SpyConn(responses=[("plainto_tsquery", _SelectResult([]))])
    search_records(spy, _TENANT_ID, "any query")
    search_calls = [(s, p) for s, p in spy.calls if "plainto_tsquery" in s]
    assert len(search_calls) == 1
    sql, params = search_calls[0]
    assert "LIMIT :limit" in sql, "SQL must use :limit placeholder for the row limit"
    assert params.get("limit") == FULL_TEXT_SEARCH_LIMIT


def test_tenant_context_set_before_query() -> None:
    """SET LOCAL must be the first SQL call — tenant context before any SELECT."""
    spy = _SpyConn(responses=[("plainto_tsquery", _SelectResult([]))])
    search_records(spy, _TENANT_ID, "query")
    assert len(spy.calls) > 0
    assert "SET LOCAL" in spy.calls[0][0], "First call must be SET LOCAL"
    subsequent = spy.calls[1:]
    assert all("SET LOCAL" not in call[0] for call in subsequent)


def test_no_sqlalchemy_session_api() -> None:
    """conn.add() and conn.flush() must never be called by the search service.

    _SpyConn.add() and .flush() raise AssertionError if invoked. A clean return
    from search_records proves only the raw-connection API was used.
    """
    spy = _SpyConn(responses=[("plainto_tsquery", _SelectResult([]))])
    search_records(spy, _TENANT_ID, "any query")
