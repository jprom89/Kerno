"""Unit tests for src/services/control_service.py.

Plain-English summary
---------------------
Ten tests verify the control service without a live database. A spy connection
records every execute() call and returns configurable rows for SELECT queries.
Tests cover: inserting new controls, skipping duplicate (framework, control_ref)
pairs, retrieving a single control as a dict, returning None for unknown IDs,
listing all active controls, filtering by framework and category, confirming
inactive controls are never returned, inserting crosswalk rows, and retrieving
crosswalk lists.

Important: control_service does NOT call resolve_and_set_tenant_context.
Controls are global platform data. These tests must NOT assert that SET LOCAL
appears in any conn.execute call.

How to run
----------
    pytest tests/unit/services/test_control_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from src.services.control_service import (
    ControlInput,
    add_crosswalk,
    get_control,
    get_crosswalks,
    list_controls,
    load_controls,
)


# ── Test infrastructure ───────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT result (INSERT, UPDATE) — fetchone/fetchall return empty."""

    def fetchone(self):
        """Return None — no rows from a non-SELECT statement."""
        return None

    def fetchall(self) -> list:
        """Return an empty list — no rows from a non-SELECT statement."""
        return []


class _SelectResult:
    """Simulates a SELECT result that returns a fixed list of row tuples."""

    def __init__(self, rows: list) -> None:
        """Store the rows that fetchone/fetchall will return."""
        self._rows = rows

    def fetchone(self):
        """Return the first row, or None if no rows."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        """Return all rows."""
        return self._rows


class _SpyConn:
    """Records every execute() call; returns configured rows for SQL fragments.

    responses is a list of (fragment, result) pairs tried in order. The first
    pair whose fragment is found in the SQL is returned. If no fragment matches,
    _NullResult is returned. Raises AssertionError if the SQLAlchemy Session API
    (add, flush) is called — the service must use raw execute() only.
    """

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
        raise AssertionError(
            "conn.add() was called. control_service must use conn.execute() only."
        )

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError(
            "conn.flush() was called. control_service must use conn.execute() only."
        )


def _make_control_input(control_ref: str = "NIS2-Art21-1") -> ControlInput:
    """Return a minimal valid ControlInput for the given control_ref."""
    return ControlInput(
        framework="nis2",
        control_ref=control_ref,
        category="risk_management",
        title="Risk Management Measures",
        obligation_text="Take appropriate and proportionate technical measures.",
        entity_types=["essential", "important"],
    )


def _make_control_row(
    control_id: str | None = None,
    framework: str = "nis2",
    control_ref: str = "NIS2-Art21-1",
    category: str = "risk_management",
    is_active: bool = True,
) -> tuple:
    """Return a row tuple matching the column order in _control_row_to_dict."""
    return (
        control_id or str(uuid.uuid4()),
        framework,
        control_ref,
        category,
        "Risk Management Measures",
        "Obligation text.",
        ["essential", "important"],
        is_active,
        datetime.now(timezone.utc),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_load_controls_inserts_new_rows() -> None:
    """New controls are inserted and their control_ids returned.

    When the existence-check SELECT returns no row, load_controls must issue an
    INSERT and return the generated UUID string.
    """
    spy = _SpyConn()  # default NullResult → fetchone() returns None (no existing row)
    result = load_controls(spy, [_make_control_input()])
    assert len(result) == 1, "One control_id expected for one inserted row"
    assert all(isinstance(cid, str) for cid in result), "control_ids must be strings"
    inserts = [sql for sql, _ in spy.calls if "INSERT INTO compliance_controls" in sql]
    assert len(inserts) == 1, "Exactly one INSERT expected for one new control"


def test_load_controls_skips_duplicates() -> None:
    """Same (framework, control_ref) pair triggers no second INSERT.

    When the existence-check SELECT returns an existing row, load_controls must
    skip the INSERT and return an empty list.
    """
    existing_id = str(uuid.uuid4())
    existing_row = (existing_id,)  # SELECT control_id returns one column
    spy = _SpyConn(
        responses=[("WHERE framework = :framework", _SelectResult([existing_row]))]
    )
    result = load_controls(spy, [_make_control_input()])
    assert result == [], "No IDs expected when control already exists"
    inserts = [sql for sql, _ in spy.calls if "INSERT" in sql]
    assert len(inserts) == 0, "No INSERT expected for a duplicate control"


def test_get_control_returns_dict() -> None:
    """A known control_id returns a dict with all expected fields populated."""
    control_id = str(uuid.uuid4())
    row = _make_control_row(control_id=control_id)
    spy = _SpyConn(
        responses=[("WHERE control_id = :control_id", _SelectResult([row]))]
    )
    result = get_control(spy, control_id)
    assert result is not None
    assert result["control_id"] == control_id
    assert result["framework"] == "nis2"
    assert result["category"] == "risk_management"
    assert "obligation_text" in result
    assert "entity_types" in result


def test_get_control_returns_none_for_unknown() -> None:
    """An unknown control_id returns None without raising."""
    spy = _SpyConn()  # NullResult → fetchone() returns None
    result = get_control(spy, str(uuid.uuid4()))
    assert result is None


def test_list_controls_no_filter() -> None:
    """Calling list_controls with no arguments returns all active controls."""
    rows = [_make_control_row(), _make_control_row(control_ref="NIS2-Art20-1")]
    spy = _SpyConn(responses=[("is_active = :is_active", _SelectResult(rows))])
    result = list_controls(spy)
    assert len(result) == 2
    assert all("control_id" in c for c in result)


def test_list_controls_by_framework() -> None:
    """Passing framework= adds a framework filter clause to the SQL and params."""
    spy = _SpyConn(responses=[("is_active = :is_active", _SelectResult([]))])
    list_controls(spy, framework="nis2")
    assert len(spy.calls) == 1, "list_controls must issue exactly one SQL call"
    list_sql, list_params = spy.calls[0]
    assert "framework = :framework" in list_sql
    assert list_params.get("framework") == "nis2"


def test_list_controls_by_category() -> None:
    """Passing category= adds a category filter clause to the SQL and params."""
    spy = _SpyConn(responses=[("is_active = :is_active", _SelectResult([]))])
    list_controls(spy, category="governance")
    assert len(spy.calls) == 1
    list_sql, list_params = spy.calls[0]
    assert "category = :category" in list_sql
    assert list_params.get("category") == "governance"


def test_list_controls_inactive_excluded() -> None:
    """is_active = True is always applied — inactive controls must never appear."""
    spy = _SpyConn(responses=[("is_active = :is_active", _SelectResult([]))])
    list_controls(spy)
    list_sql, list_params = spy.calls[0]
    assert "is_active = :is_active" in list_sql
    assert list_params.get("is_active") is True, "is_active filter must be True"


def test_add_crosswalk_inserts_row() -> None:
    """add_crosswalk issues an INSERT and returns a crosswalk_id string."""
    spy = _SpyConn()
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    result = add_crosswalk(spy, source_id, target_id, "equivalent", note="Test note")
    assert isinstance(result, str), "crosswalk_id must be a string"
    inserts = [sql for sql, _ in spy.calls if "INSERT INTO control_crosswalks" in sql]
    assert len(inserts) == 1, "Exactly one INSERT expected"
    _, params = spy.calls[0]
    assert params["source_control_id"] == source_id
    assert params["target_control_id"] == target_id
    assert params["relationship"] == "equivalent"


def test_get_crosswalks_returns_list() -> None:
    """Known source_control_id returns a list of crosswalk dicts."""
    source_id = str(uuid.uuid4())
    target_id = str(uuid.uuid4())
    crosswalk_id = str(uuid.uuid4())
    row = (
        crosswalk_id,
        source_id,
        target_id,
        "equivalent",
        "Crosswalk note",
        datetime.now(timezone.utc),
    )
    spy = _SpyConn(responses=[("FROM control_crosswalks", _SelectResult([row]))])
    result = get_crosswalks(spy, source_id)
    assert len(result) == 1
    assert result[0]["crosswalk_id"] == crosswalk_id
    assert result[0]["source_control_id"] == source_id
    assert result[0]["relationship"] == "equivalent"
    assert result[0]["note"] == "Crosswalk note"
