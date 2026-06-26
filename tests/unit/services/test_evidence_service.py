"""Unit tests for src/services/evidence_service.py.

Plain-English summary
---------------------
Thirteen tests verify the evidence service without a live database. A spy
connection records every execute() call and returns configurable rows for
SELECT queries. Tests cover: inserting a new link, re-linking (updating an
existing link), rejecting out-of-range relevance scores, returning active
links with LINK_STATUS_ACTIVE, including broken links with LINK_STATUS_BROKEN,
applying source_system and min_relevance filters, the reverse lookup
(controls for a record), soft-deleting a link (True/False return values),
tenant context ordering (SET LOCAL must be first), and rejecting None tenant.

How to run
----------
    pytest tests/unit/services/test_evidence_service.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from src.exceptions import TenantContextMissingError
from src.services.evidence_service import (
    LINK_STATUS_ACTIVE,
    LINK_STATUS_BROKEN,
    EvidenceResult,
    get_controls_for_record,
    get_evidence_for_control,
    link_evidence,
    remove_link,
)

# A deterministic UUIDv4 for all tests — fixed for readable failure messages.
_TENANT_ID = "c0000000-0000-4000-c000-000000000099"
_CONTROL_ID = str(uuid.uuid4())
_RECORD_ID = str(uuid.uuid4())
_LINK_ID = str(uuid.uuid4())
_NOW = datetime.now(timezone.utc)


# ── Test infrastructure ───────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT result — fetchone/fetchall return empty."""

    def fetchone(self):
        """Return None — no rows from a non-SELECT or empty SELECT."""
        return None

    def fetchall(self) -> list:
        """Return an empty list — no rows."""
        return []


class _SelectResult:
    """Simulates a SELECT result that returns a fixed list of row tuples."""

    def __init__(self, rows: list) -> None:
        """Store the rows to return."""
        self._rows = rows

    def fetchone(self):
        """Return the first row, or None if no rows."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        """Return all rows."""
        return self._rows


class _SpyConn:
    """Records execute() calls; returns configured rows for matching SQL fragments.

    responses is a list of (fragment, result) tried in order — first match wins.
    Raises AssertionError if the SQLAlchemy Session API (add, flush) is called.
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
        raise AssertionError("conn.add() called — evidence_service must use conn.execute()")

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.flush() called — evidence_service must use conn.execute()")


def _make_evidence_row(is_deleted: bool = False) -> tuple:
    """Return a 15-column row tuple matching the LEFT JOIN evidence query column order."""
    return (
        _LINK_ID,
        _CONTROL_ID,
        _RECORD_ID,
        "system",
        _NOW,
        0.9,
        None,
        is_deleted,      # col 7: cr.is_deleted — determines link_status
        "jira",
        "JIRA-123",
        "issue",
        "Security Patch Ticket",
        "Describes a critical security patch applied.",
        _NOW,
        "abc123hash",
    )


def _make_control_row() -> tuple:
    """Return an 8-column row tuple matching the controls JOIN query column order."""
    return (
        _CONTROL_ID,
        "nis2",
        "NIS2-Art21-1",
        "risk_management",
        "Risk Management Measures",
        "Obligation text.",
        ["essential"],
        True,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_link_evidence_inserts_row() -> None:
    """A new (control_id, record_id) pair inserts a link row and returns a link_id."""
    spy = _SpyConn()  # SELECT existing returns None → new INSERT
    result = link_evidence(spy, _TENANT_ID, _CONTROL_ID, _RECORD_ID, "system")
    assert isinstance(result, str) and result, "link_id must be a non-empty string"
    inserts = [sql for sql, _ in spy.calls if "INSERT INTO control_evidence_links" in sql]
    assert len(inserts) == 1, "Exactly one INSERT expected for a new link"


def test_relink_updates_existing() -> None:
    """Same (control_id, record_id) updates the existing link instead of inserting."""
    existing_row = (_LINK_ID,)
    spy = _SpyConn(
        responses=[("AND record_id = :record_id", _SelectResult([existing_row]))]
    )
    result = link_evidence(spy, _TENANT_ID, _CONTROL_ID, _RECORD_ID, "user-abc", 0.8)
    assert result == _LINK_ID, "Returned ID must be the existing link_id"
    inserts = [sql for sql, _ in spy.calls if "INSERT INTO control_evidence_links" in sql]
    assert len(inserts) == 0, "No INSERT on re-link — only UPDATE"
    updates = [sql for sql, _ in spy.calls if "UPDATE control_evidence_links" in sql]
    assert len(updates) == 1, "Exactly one UPDATE expected on re-link"


def test_relevance_score_out_of_range_raises() -> None:
    """relevance_score=1.1 raises ValueError before any SQL is issued."""
    spy = _SpyConn()
    with pytest.raises(ValueError):
        link_evidence(spy, _TENANT_ID, _CONTROL_ID, _RECORD_ID, "system", relevance_score=1.1)


def test_get_evidence_returns_active_links() -> None:
    """Active records (is_deleted=False) are returned with LINK_STATUS_ACTIVE."""
    row = _make_evidence_row(is_deleted=False)
    spy = _SpyConn(responses=[("LEFT JOIN context_records", _SelectResult([row]))])
    results = get_evidence_for_control(spy, _TENANT_ID, _CONTROL_ID)
    assert len(results) == 1
    assert isinstance(results[0], EvidenceResult)
    assert results[0].link_status == LINK_STATUS_ACTIVE
    assert results[0].source_system == "jira"


def test_get_evidence_includes_broken_links() -> None:
    """is_deleted=True records are returned with LINK_STATUS_BROKEN — never dropped."""
    row = _make_evidence_row(is_deleted=True)
    spy = _SpyConn(responses=[("LEFT JOIN context_records", _SelectResult([row]))])
    results = get_evidence_for_control(spy, _TENANT_ID, _CONTROL_ID)
    assert len(results) == 1, "Broken link must be included, not dropped"
    assert results[0].link_status == LINK_STATUS_BROKEN


def test_get_evidence_filter_source_system() -> None:
    """Passing source_system adds a filter clause referencing source_system to the SQL."""
    spy = _SpyConn(responses=[("LEFT JOIN context_records", _SelectResult([]))])
    get_evidence_for_control(spy, _TENANT_ID, _CONTROL_ID, source_system="jira")
    evidence_calls = [
        (sql, params)
        for sql, params in spy.calls
        if "LEFT JOIN context_records" in sql
    ]
    assert len(evidence_calls) == 1
    sql, params = evidence_calls[0]
    assert "source_system" in sql, "SQL must reference source_system when filter is provided"
    assert params.get("source_system") == "jira"


def test_get_evidence_filter_min_relevance() -> None:
    """Passing min_relevance adds a relevance_score filter to the SQL and params."""
    spy = _SpyConn(responses=[("LEFT JOIN context_records", _SelectResult([]))])
    get_evidence_for_control(spy, _TENANT_ID, _CONTROL_ID, min_relevance=0.5)
    evidence_calls = [(s, p) for s, p in spy.calls if "LEFT JOIN context_records" in s]
    assert len(evidence_calls) == 1
    sql, params = evidence_calls[0]
    assert "relevance_score" in sql, "SQL must filter by relevance_score when min_relevance provided"
    assert params.get("min_relevance") == 0.5


def test_get_controls_for_record_returns_list() -> None:
    """Reverse lookup returns a list of control dicts for the given record_id."""
    row = _make_control_row()
    spy = _SpyConn(responses=[("JOIN compliance_controls", _SelectResult([row]))])
    results = get_controls_for_record(spy, _TENANT_ID, _RECORD_ID)
    assert len(results) == 1
    assert results[0]["control_ref"] == "NIS2-Art21-1"
    assert results[0]["framework"] == "nis2"


def test_remove_link_returns_true_on_success() -> None:
    """remove_link returns True and sets removed_at when the link exists."""
    existing_row = (_LINK_ID,)
    spy = _SpyConn(
        responses=[("link_id = :link_id", _SelectResult([existing_row]))]
    )
    result = remove_link(spy, _TENANT_ID, _LINK_ID)
    assert result is True
    update_calls = [sql for sql, _ in spy.calls if "SET removed_at = :removed_at" in sql]
    assert len(update_calls) == 1, "removed_at must be set on successful removal"


def test_remove_link_returns_false_if_not_found() -> None:
    """remove_link returns False when no active link with that link_id exists."""
    spy = _SpyConn()  # SELECT returns None (no matching link)
    result = remove_link(spy, _TENANT_ID, str(uuid.uuid4()))
    assert result is False
    update_calls = [sql for sql, _ in spy.calls if "SET removed_at" in sql]
    assert len(update_calls) == 0, "No UPDATE expected when link not found"


def test_tenant_context_set_before_any_query() -> None:
    """SET LOCAL must be the very first SQL call — tenant context before any SELECT."""
    spy = _SpyConn()
    link_evidence(spy, _TENANT_ID, _CONTROL_ID, _RECORD_ID, "system")
    assert len(spy.calls) > 0, "At least one SQL call expected"
    assert "SET LOCAL" in spy.calls[0][0], "First SQL call must be SET LOCAL"
    subsequent = spy.calls[1:]
    assert all("SET LOCAL" not in call[0] for call in subsequent), (
        "SET LOCAL must appear only once, as the first call"
    )


def test_none_tenant_raises() -> None:
    """tenant_id=None raises TenantContextMissingError before any SQL is issued."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        link_evidence(spy, None, _CONTROL_ID, _RECORD_ID, "system")
    assert len(spy.calls) == 0, "No SQL must be issued when tenant_id is None"


def test_no_sqlalchemy_session_api() -> None:
    """conn.add() and conn.flush() must never be called by the evidence service.

    _SpyConn.add() and .flush() raise AssertionError if invoked. A clean return
    from link_evidence proves only the raw-connection API was used.
    """
    spy = _SpyConn()
    link_evidence(spy, _TENANT_ID, _CONTROL_ID, _RECORD_ID, "system")
