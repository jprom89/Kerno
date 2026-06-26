"""Unit tests for the _score_evidence helper in recommendation_service.py.

Plain-English summary
---------------------
Seven tests verify the private scoring function directly. The function is
imported by name and called with lists of EvidenceResult objects constructed
in-process — no database, no connection spy needed. Tests cover: empty input
returning STATUS_GAP, high relevance producing STATUS_MET, mixed relevance
producing STATUS_PARTIAL, None relevance scores using DEFAULT_RELEVANCE_SCORE,
the 1.0 cap on confidence_score, exact threshold boundary behaviour, and the
rule that only CONFIDENCE_LOW sets requires_review=True.

How to run
----------
    pytest tests/unit/services/test_scoring.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

from config.constants import (
    DEFAULT_RELEVANCE_SCORE,
    HIGH_CONFIDENCE_THRESHOLD,
    MEDIUM_CONFIDENCE_THRESHOLD,
)
from src.models.recommendation import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    STATUS_GAP,
    STATUS_MET,
    STATUS_PARTIAL,
)
from src.services.evidence_service import LINK_STATUS_ACTIVE, LINK_STATUS_BROKEN, EvidenceResult
from src.services.recommendation_service import _score_evidence

_NOW = datetime.now(timezone.utc)
_CONTROL_ID = "ctrl-001"
_RECORD_ID = "rec-001"


def _make_evidence(relevance_score=None, link_status=LINK_STATUS_ACTIVE) -> EvidenceResult:
    """Return a minimal EvidenceResult with the given relevance_score and link_status."""
    return EvidenceResult(
        link_id="link-001",
        control_id=_CONTROL_ID,
        record_id=_RECORD_ID,
        linked_by="system",
        linked_at=_NOW,
        relevance_score=relevance_score,
        note=None,
        link_status=link_status,
        source_system="jira",
        external_id="JIRA-1",
        record_type="issue",
        title="Test record",
        body="Body text.",
        fetched_at=_NOW,
        content_hash="abc123",
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_score_empty_list_returns_gap() -> None:
    """Empty evidence list -> STATUS_GAP with confidence_score of 0.0."""
    result = _score_evidence([])
    assert result.status == STATUS_GAP
    assert result.confidence_score == 0.0
    assert result.confidence_level == CONFIDENCE_LOW
    assert result.requires_review is True


def test_score_high_relevance_returns_met() -> None:
    """All evidence scores >= HIGH_CONFIDENCE_THRESHOLD -> STATUS_MET, CONFIDENCE_HIGH."""
    evidence = [
        _make_evidence(relevance_score=0.9),
        _make_evidence(relevance_score=0.85),
    ]
    result = _score_evidence(evidence)
    assert result.status == STATUS_MET
    assert result.confidence_level == CONFIDENCE_HIGH
    assert result.confidence_score >= HIGH_CONFIDENCE_THRESHOLD


def test_score_mixed_relevance_returns_partial() -> None:
    """Mixed evidence scores averaging between thresholds -> STATUS_PARTIAL, CONFIDENCE_MEDIUM."""
    evidence = [
        _make_evidence(relevance_score=0.9),
        _make_evidence(relevance_score=0.1),
    ]
    result = _score_evidence(evidence)
    assert result.status == STATUS_PARTIAL
    assert result.confidence_level == CONFIDENCE_MEDIUM
    assert MEDIUM_CONFIDENCE_THRESHOLD <= result.confidence_score < HIGH_CONFIDENCE_THRESHOLD


def test_score_none_relevance_uses_default() -> None:
    """Records with relevance_score=None contribute DEFAULT_RELEVANCE_SCORE to the sum."""
    evidence = [_make_evidence(relevance_score=None)]
    result = _score_evidence(evidence)
    assert result.confidence_score == DEFAULT_RELEVANCE_SCORE
    assert result.status == STATUS_PARTIAL


def test_score_capped_at_1_0() -> None:
    """confidence_score is never greater than 1.0 regardless of input scores."""
    evidence = [
        _make_evidence(relevance_score=1.0),
        _make_evidence(relevance_score=1.0),
        _make_evidence(relevance_score=1.0),
    ]
    result = _score_evidence(evidence)
    assert result.confidence_score <= 1.0
    assert result.confidence_score == 1.0


def test_confidence_level_boundaries() -> None:
    """Score at exact threshold values maps to the correct confidence level."""
    at_high = [_make_evidence(relevance_score=HIGH_CONFIDENCE_THRESHOLD)]
    result_high = _score_evidence(at_high)
    assert result_high.confidence_level == CONFIDENCE_HIGH
    assert result_high.status == STATUS_MET

    at_medium = [_make_evidence(relevance_score=MEDIUM_CONFIDENCE_THRESHOLD)]
    result_medium = _score_evidence(at_medium)
    assert result_medium.confidence_level == CONFIDENCE_MEDIUM
    assert result_medium.status == STATUS_PARTIAL

    below_medium = [_make_evidence(relevance_score=MEDIUM_CONFIDENCE_THRESHOLD - 0.01)]
    result_low = _score_evidence(below_medium)
    assert result_low.confidence_level == CONFIDENCE_LOW
    assert result_low.status == STATUS_GAP


def test_requires_review_only_on_low() -> None:
    """requires_review is True only when confidence_level is CONFIDENCE_LOW."""
    low_evidence = [_make_evidence(relevance_score=0.1)]
    result_low = _score_evidence(low_evidence)
    assert result_low.requires_review is True

    medium_evidence = [_make_evidence(relevance_score=MEDIUM_CONFIDENCE_THRESHOLD)]
    result_medium = _score_evidence(medium_evidence)
    assert result_medium.requires_review is False

    high_evidence = [_make_evidence(relevance_score=HIGH_CONFIDENCE_THRESHOLD)]
    result_high = _score_evidence(high_evidence)
    assert result_high.requires_review is False
