"""Unit tests for src/services/recommendation_service.py.

Plain-English summary
---------------------
Fifteen tests verify the recommendation service without a live database.
A spy connection records every execute() call. get_evidence_for_control is
patched to return pre-built EvidenceResult lists, isolating the scoring and
persistence logic from the evidence retrieval layer. Tests cover: STATUS_MET /
PARTIAL / GAP outputs, confidence level assignment, requires_review flag,
superseding prior recommendations, input_snapshot persistence, rationale and
gaps content, tenant context ordering (SET LOCAL first), TenantContextMissingError
on None tenant, absence of SQLAlchemy Session API calls, and broken links
appearing in gaps text.

How to run
----------
    pytest tests/unit/services/test_recommendation_service.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from config.constants import MAX_RATIONALE_LENGTH
from src.exceptions import TenantContextMissingError
from src.models.recommendation import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    STATUS_GAP,
    STATUS_MET,
    STATUS_PARTIAL,
)
from src.services.evidence_service import LINK_STATUS_ACTIVE, LINK_STATUS_BROKEN, EvidenceResult
from src.services.recommendation_service import generate_recommendation

_TENANT_ID = "c0000000-0000-4000-c000-000000000077"
_CONTROL_ID = "NIS2-Art21-1"
_RECORD_ID = "rec-uuid-001"
_CONTROL_ROW = (_CONTROL_ID, "NIS2-Art21-1", "Risk Management Measures")
_NOW = datetime.now(timezone.utc)

_PATCH_TARGET = "src.services.recommendation_service.get_evidence_for_control"


# ── Test infrastructure ────────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT result — fetchone/fetchall return empty."""

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

    def execute(self, sql, params=None) -> object:
        """Record the call and return the first matching configured response."""
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in str(sql):
                return result
        return _NullResult()

    def add(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.add() called — recommendation_service must use conn.execute()")

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.flush() called — recommendation_service must use conn.execute()")


def _make_evidence(
    relevance_score: float | None = 0.9,
    link_status: str = LINK_STATUS_ACTIVE,
    title: str = "Security Patch",
    source_system: str | None = "jira",
    record_id: str = _RECORD_ID,
) -> EvidenceResult:
    """Return an EvidenceResult with configurable key fields."""
    return EvidenceResult(
        link_id="link-001",
        control_id=_CONTROL_ID,
        record_id=record_id,
        linked_by="system",
        linked_at=_NOW,
        relevance_score=relevance_score,
        note=None,
        link_status=link_status,
        source_system=source_system,
        external_id="JIRA-101",
        record_type="issue",
        title=title,
        body="Body text.",
        fetched_at=_NOW,
        content_hash="abc123",
    )


def _default_spy() -> _SpyConn:
    """Return a spy pre-configured to return control metadata for the test control."""
    return _SpyConn(
        responses=[("FROM compliance_controls", _SelectResult([_CONTROL_ROW]))]
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_generate_recommendation_met() -> None:
    """High-relevance evidence produces STATUS_MET and CONFIDENCE_HIGH."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_MET
    assert result.confidence_level == CONFIDENCE_HIGH


def test_generate_recommendation_partial() -> None:
    """Medium-relevance evidence produces STATUS_PARTIAL and CONFIDENCE_MEDIUM."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.55)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_PARTIAL
    assert result.confidence_level == CONFIDENCE_MEDIUM


def test_generate_recommendation_gap_no_evidence() -> None:
    """Zero active evidence produces STATUS_GAP."""
    spy = _default_spy()
    with patch(_PATCH_TARGET, return_value=[]):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_GAP


def test_generate_recommendation_gap_low_score() -> None:
    """Evidence present but score below MEDIUM threshold -> STATUS_GAP."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.2)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_GAP


def test_low_confidence_sets_requires_review() -> None:
    """CONFIDENCE_LOW output sets requires_review=True (AC-3)."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.1)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.confidence_level == CONFIDENCE_LOW
    assert result.requires_review is True


def test_high_confidence_clears_requires_review() -> None:
    """CONFIDENCE_HIGH output sets requires_review=False."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.confidence_level == CONFIDENCE_HIGH
    assert result.requires_review is False


def test_prior_recommendation_superseded() -> None:
    """Second generate call issues UPDATE to mark prior row is_superseded=True."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    supersede_calls = [
        sql for sql, _ in spy.calls
        if "is_superseded = TRUE" in str(sql)
    ]
    assert len(supersede_calls) >= 1, "UPDATE to set is_superseded=TRUE must be called"


def test_input_snapshot_persisted() -> None:
    """input_snapshot in INSERT params contains control_id, evidence_count, generated_at."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    insert_calls = [
        (sql, params) for sql, params in spy.calls
        if "INSERT INTO recommendations" in str(sql)
    ]
    assert len(insert_calls) == 1, "Exactly one INSERT expected"
    _, params = insert_calls[0]
    snapshot = params["input_snapshot"]
    assert snapshot["control_id"] == _CONTROL_ID
    assert snapshot["evidence_count"] == 1
    assert "generated_at" in snapshot


def test_rationale_non_empty_string() -> None:
    """rationale is a non-empty string under MAX_RATIONALE_LENGTH characters."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert isinstance(result.rationale, str)
    assert len(result.rationale) > 0
    assert len(result.rationale) <= MAX_RATIONALE_LENGTH


def test_gaps_none_when_met() -> None:
    """STATUS_MET recommendation has gaps=None (AC-2)."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_MET
    assert result.gaps is None


def test_gaps_present_when_partial() -> None:
    """STATUS_PARTIAL recommendation has a non-empty gaps string."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.55)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_PARTIAL
    assert isinstance(result.gaps, str)
    assert len(result.gaps) > 0


def test_tenant_context_set_before_query() -> None:
    """SET LOCAL must be the first SQL call — tenant context before any SELECT."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert len(spy.calls) > 0, "At least one SQL call expected"
    assert "SET LOCAL" in str(spy.calls[0][0]), "First SQL call must be SET LOCAL"


def test_none_tenant_raises() -> None:
    """tenant_id=None raises TenantContextMissingError before any SQL is issued."""
    spy = _default_spy()
    with pytest.raises(TenantContextMissingError):
        generate_recommendation(spy, None, _CONTROL_ID)
    sql_calls = [s for s, _ in spy.calls if "SET LOCAL" not in str(s)]
    assert len(sql_calls) == 0, "No query SQL must be issued when tenant_id is None"


def test_no_sqlalchemy_session_api() -> None:
    """conn.add() and conn.flush() must never be called by the recommendation service.

    _SpyConn.add() and .flush() raise AssertionError if invoked. A clean return
    from generate_recommendation proves only the raw-connection API was used.
    """
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)


def test_broken_links_noted_in_gaps() -> None:
    """A broken evidence link (LINK_STATUS_BROKEN) is noted in the gaps string."""
    spy = _default_spy()
    broken = _make_evidence(
        relevance_score=None,
        link_status=LINK_STATUS_BROKEN,
        source_system=None,
    )
    with patch(_PATCH_TARGET, return_value=[broken]):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.gaps is not None, "gaps must be set when a broken link is present"
    assert "broken" in result.gaps.lower(), "gaps must mention the broken link"
