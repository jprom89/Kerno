"""recommendation_service.py — Explainable recommendation engine for KER-105.

What:  Scores a compliance control's evidence coverage and persists a
       recommendation with status, confidence, rationale, cited evidence IDs,
       and a full input snapshot. Wires together evidence_service (Doc 12),
       control data, and the scoring rules from PROMPT_doc13_decision_layer.md.

Why:   KER-105 AC-1 through AC-4 require recommendations that are explainable
       (AC-2), marked for review when low-confidence (AC-3), and reproducible
       from a persisted snapshot without querying other tables (AC-4).

How to run or test:
    pytest tests/unit/services/test_recommendation_service.py -v
    pytest tests/unit/services/test_scoring.py -v
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

from config.constants import (
    DEFAULT_RELEVANCE_SCORE,
    HIGH_CONFIDENCE_THRESHOLD,
    MAX_RATIONALE_LENGTH,
    MEDIUM_CONFIDENCE_THRESHOLD,
)
from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError  # noqa: F401  re-exported
from src.models.recommendation import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    STATUS_GAP,
    STATUS_MET,
    STATUS_PARTIAL,
)
from src.services.evidence_service import (
    LINK_STATUS_ACTIVE,
    EvidenceResult,
    get_evidence_for_control,
)

# ---------------------------------------------------------------------------
# Internal dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ScoringResult:
    """Output of _score_evidence — the four derived scoring fields."""

    confidence_score: float
    status: str
    confidence_level: str
    requires_review: bool


@dataclasses.dataclass(frozen=True)
class OpenRecommendation:
    """One row of the review queue (KER-303) — a current recommendation with no
    later override, enriched with catalogue metadata for display and filtering."""

    recommendation_id: str
    control_id: str
    control_ref: str | None
    control_title: str | None
    category: str | None
    status: str
    confidence_level: str
    confidence_score: float
    rationale: str
    evidence_count: int
    generated_at: datetime


@dataclasses.dataclass(frozen=True)
class RecommendationOutput:
    """Return type for all public recommendation service functions."""

    recommendation_id: str
    tenant_id: str
    control_id: str
    status: str
    confidence_level: str
    confidence_score: float
    rationale: str
    gaps: str | None
    evidence_ids: list[str]
    requires_review: bool
    input_snapshot: dict
    generated_at: datetime
    is_superseded: bool


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SUPERSEDE_PRIOR = """
UPDATE recommendations
SET is_superseded = TRUE
WHERE tenant_id = :tenant_id
AND control_id = :control_id
AND is_superseded = FALSE
"""

_INSERT_RECOMMENDATION = """
INSERT INTO recommendations (
    recommendation_id, tenant_id, control_id, status, confidence_level,
    confidence_score, rationale, gaps, evidence_ids, requires_review,
    input_snapshot, generated_at, is_superseded
) VALUES (
    :recommendation_id, :tenant_id, :control_id, :status, :confidence_level,
    :confidence_score, :rationale, :gaps, :evidence_ids, :requires_review,
    :input_snapshot, :generated_at, FALSE
)
"""

_SELECT_CURRENT = """
SELECT recommendation_id, tenant_id, control_id, status, confidence_level,
       confidence_score, rationale, gaps, evidence_ids, requires_review,
       input_snapshot, generated_at, is_superseded
FROM recommendations
WHERE tenant_id = :tenant_id
AND control_id = :control_id
AND is_superseded = FALSE
ORDER BY generated_at DESC
LIMIT 1
"""

_SELECT_BY_ID = """
SELECT recommendation_id, tenant_id, control_id, status, confidence_level,
       confidence_score, rationale, gaps, evidence_ids, requires_review,
       input_snapshot, generated_at, is_superseded
FROM recommendations
WHERE recommendation_id = :recommendation_id
"""

_SELECT_CONTROL_META = """
SELECT control_id, control_ref, title
FROM compliance_controls
WHERE control_id = :control_id
"""

# "Open" predicate (KER-303, corrected 15 July 2026): a recommendation is open
# when it is current (not superseded) and no override for its control was
# recorded AFTER it was generated. Overrides link to controls via
# original_control_id — there is NO overrides.recommendation_id column. The
# created_at > generated_at guard is required: an override predating the
# recommendation does not close it. The explicit o.tenant_id filter is defence
# in depth on top of RLS, per the house pattern in coverage_service.
_OPEN_PREDICATE = """
r.tenant_id = :tenant_id
  AND r.is_superseded = FALSE
  AND NOT EXISTS (
      SELECT 1 FROM overrides o
      WHERE o.tenant_id = r.tenant_id
        AND o.original_control_id = r.control_id
        AND o.created_at > r.generated_at
  )
"""

# The catalogue join enriches each row with ref/title/category for display and
# the client-side category filter; LEFT JOIN because a recommendation may
# reference a control ref that is not in the platform catalogue.
_SELECT_OPEN_PAGE = f"""
SELECT r.recommendation_id, r.control_id, cc.control_ref, cc.title, cc.category,
       r.status, r.confidence_level, r.confidence_score, r.rationale,
       r.evidence_ids, r.generated_at
FROM recommendations r
LEFT JOIN compliance_controls cc ON r.control_id = cc.control_id::text
WHERE {_OPEN_PREDICATE}
ORDER BY r.generated_at DESC
LIMIT :page_size OFFSET :page_offset
"""

_COUNT_OPEN = f"""
SELECT count(*)
FROM recommendations r
WHERE {_OPEN_PREDICATE}
"""

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_recommendation(conn, tenant_id, control_id: str) -> RecommendationOutput:
    """Score a control's evidence and persist a new recommendation.

    Sets tenant context, fetches all evidence for the control (including broken
    links), runs the deterministic scoring rules, marks any prior recommendations
    as superseded, then inserts and returns the new recommendation.
    Raises TenantContextMissingError if tenant_id is None or empty.
    """
    set_tenant_context(conn, tenant_id)
    control_meta = _fetch_control_meta(conn, control_id)
    evidence = get_evidence_for_control(conn, tenant_id, control_id)
    scoring = _score_evidence(evidence)
    active_evidence = [e for e in evidence if e.link_status == LINK_STATUS_ACTIVE]
    rationale = _build_rationale(active_evidence, scoring)
    gaps = _build_gaps(evidence, active_evidence, scoring)
    now = datetime.now(timezone.utc)
    snapshot = _build_snapshot(control_id, control_meta, evidence, now)
    rec_id = str(uuid.uuid4())
    _supersede_prior(conn, tenant_id, control_id)
    params = _build_insert_params(
        rec_id, tenant_id, control_id, scoring, rationale, gaps,
        active_evidence, snapshot, now,
    )
    conn.execute(_INSERT_RECOMMENDATION, params)
    return _row_to_output(
        (rec_id, tenant_id, control_id, scoring.status, scoring.confidence_level,
         scoring.confidence_score, rationale, gaps,
         [e.record_id for e in active_evidence],
         scoring.requires_review, snapshot, now, False)
    )


def get_recommendation(conn, tenant_id, control_id: str) -> RecommendationOutput | None:
    """Return the current (is_superseded=False) recommendation, or None.

    Sets tenant context before querying. Returns None if no recommendation
    has ever been generated for this (tenant, control) pair.
    """
    set_tenant_context(conn, tenant_id)
    row = conn.execute(
        _SELECT_CURRENT, {"tenant_id": tenant_id, "control_id": control_id}
    ).fetchone()
    return _row_to_output(row) if row is not None else None


def list_open_recommendations(
    conn, tenant_id, page: int, page_size: int
) -> tuple[list[OpenRecommendation], int]:
    """Return one page of the tenant's open recommendations plus the total count.

    "Open" uses the exact corrected KER-303 predicate (_OPEN_PREDICATE above):
    current rows with no override recorded after generation. Newest first;
    page is 1-based. Sets tenant context before querying and raises
    TenantContextMissingError on a missing or invalid tenant. Read-only.
    """
    set_tenant_context(conn, tenant_id)
    params = {
        "tenant_id": str(tenant_id),
        "page_size": page_size,
        "page_offset": (page - 1) * page_size,
    }
    rows = conn.execute(_SELECT_OPEN_PAGE, params).fetchall()
    count_row = conn.execute(_COUNT_OPEN, {"tenant_id": str(tenant_id)}).fetchone()
    total = int(count_row[0]) if count_row is not None else 0
    return [_row_to_open_recommendation(row) for row in rows], total


def _row_to_open_recommendation(row) -> OpenRecommendation:
    """Map one _SELECT_OPEN_PAGE row (by position) to an OpenRecommendation."""
    evidence_ids = list(row[9]) if row[9] is not None else []
    return OpenRecommendation(
        recommendation_id=str(row[0]),
        control_id=str(row[1]),
        control_ref=row[2],
        control_title=row[3],
        category=row[4],
        status=row[5],
        confidence_level=row[6],
        confidence_score=float(row[7]),
        rationale=row[8],
        evidence_count=len(evidence_ids),
        generated_at=row[10],
    )


def get_recommendation_by_id(
    conn, tenant_id, recommendation_id: str
) -> RecommendationOutput | None:
    """Return a specific recommendation by ID for audit reproduction.

    Sets tenant context before querying. Returns None if the ID does not exist
    within the current tenant's scope.
    """
    set_tenant_context(conn, tenant_id)
    row = conn.execute(
        _SELECT_BY_ID, {"recommendation_id": recommendation_id}
    ).fetchone()
    return _row_to_output(row) if row is not None else None


# ---------------------------------------------------------------------------
# Scoring logic (independently testable)
# ---------------------------------------------------------------------------


def _score_evidence(evidence: list[EvidenceResult]) -> ScoringResult:
    """Compute confidence_score, status, confidence_level, and requires_review.

    Only LINK_STATUS_ACTIVE records contribute to the score. Records with no
    relevance_score use DEFAULT_RELEVANCE_SCORE. The raw weighted sum is
    normalised by the active evidence count and capped at 1.0.
    """
    active = [e for e in evidence if e.link_status == LINK_STATUS_ACTIVE]
    evidence_count = len(active)
    if evidence_count == 0:
        return ScoringResult(
            confidence_score=0.0,
            status=STATUS_GAP,
            confidence_level=CONFIDENCE_LOW,
            requires_review=True,
        )
    weighted_sum = sum(
        e.relevance_score if e.relevance_score is not None else DEFAULT_RELEVANCE_SCORE
        for e in active
    )
    raw_score = weighted_sum / evidence_count
    confidence_score = min(raw_score, 1.0)
    status = _derive_status(evidence_count, confidence_score)
    confidence_level = _derive_confidence_level(confidence_score)
    return ScoringResult(
        confidence_score=confidence_score,
        status=status,
        confidence_level=confidence_level,
        requires_review=(confidence_level == CONFIDENCE_LOW),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _derive_status(evidence_count: int, confidence_score: float) -> str:
    """Map evidence count and confidence score to a STATUS_ constant."""
    if evidence_count == 0:
        return STATUS_GAP
    if confidence_score >= HIGH_CONFIDENCE_THRESHOLD:
        return STATUS_MET
    if confidence_score >= MEDIUM_CONFIDENCE_THRESHOLD:
        return STATUS_PARTIAL
    return STATUS_GAP


def _derive_confidence_level(confidence_score: float) -> str:
    """Map a raw confidence score to a CONFIDENCE_ constant."""
    if confidence_score >= HIGH_CONFIDENCE_THRESHOLD:
        return CONFIDENCE_HIGH
    if confidence_score >= MEDIUM_CONFIDENCE_THRESHOLD:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


def _build_rationale(
    active_evidence: list[EvidenceResult], scoring: ScoringResult
) -> str:
    """Build a plain-language rationale string capped at MAX_RATIONALE_LENGTH.

    Names the active evidence count, the highest-relevance source and title
    (if any), the resulting status, and why it was assigned.
    """
    count = len(active_evidence)
    best = _best_evidence(active_evidence)
    best_desc = (
        f" The highest-relevance record is '{best.title}' from {best.source_system}."
        if best and best.title
        else ""
    )
    text = (
        f"Found {count} active evidence record(s) for this control.{best_desc} "
        f"Confidence score: {scoring.confidence_score:.2f}. "
        f"Status set to '{scoring.status}': "
        + _status_reason(scoring.status, scoring.confidence_score)
    )
    return text[:MAX_RATIONALE_LENGTH]


def _build_gaps(
    all_evidence: list[EvidenceResult],
    active_evidence: list[EvidenceResult],
    scoring: ScoringResult,
) -> str | None:
    """Build a plain-language gaps string, or None when status is STATUS_MET.

    Names broken link count, zero-evidence condition, and whether the score
    fell below thresholds and by how much.
    """
    if scoring.status == STATUS_MET:
        return None
    broken_count = len(all_evidence) - len(active_evidence)
    parts: list[str] = []
    if len(active_evidence) == 0:
        parts.append("No active evidence records were found for this control.")
    if broken_count > 0:
        parts.append(f"{broken_count} linked record(s) are broken (source deleted).")
    if scoring.confidence_score < HIGH_CONFIDENCE_THRESHOLD:
        gap = round(HIGH_CONFIDENCE_THRESHOLD - scoring.confidence_score, 2)
        parts.append(
            f"Score {scoring.confidence_score:.2f} is {gap} below the high-confidence "
            f"threshold of {HIGH_CONFIDENCE_THRESHOLD}."
        )
    text = " ".join(parts) if parts else "Evidence coverage is insufficient."
    return text[:MAX_RATIONALE_LENGTH]


def _best_evidence(active: list[EvidenceResult]) -> EvidenceResult | None:
    """Return the active evidence record with the highest relevance_score, or None."""
    if not active:
        return None
    return max(
        active,
        key=lambda e: e.relevance_score if e.relevance_score is not None else DEFAULT_RELEVANCE_SCORE,
    )


def _status_reason(status: str, score: float) -> str:
    """Return a one-sentence explanation of why status was assigned."""
    if status == STATUS_MET:
        return f"score meets the high-confidence threshold of {HIGH_CONFIDENCE_THRESHOLD}."
    if status == STATUS_PARTIAL:
        return (
            f"score meets the medium-confidence threshold of {MEDIUM_CONFIDENCE_THRESHOLD} "
            f"but is below the high threshold of {HIGH_CONFIDENCE_THRESHOLD}."
        )
    return (
        f"score {score:.2f} is below the medium-confidence threshold of "
        f"{MEDIUM_CONFIDENCE_THRESHOLD} or no evidence was found."
    )


def _fetch_control_meta(conn, control_id: str) -> tuple:
    """Fetch (control_id, control_ref, title) from compliance_controls."""
    row = conn.execute(
        _SELECT_CONTROL_META, {"control_id": control_id}
    ).fetchone()
    return row if row is not None else (control_id, "", "")


def _build_snapshot(
    control_id: str,
    control_meta: tuple,
    evidence: list[EvidenceResult],
    now: datetime,
) -> dict:
    """Build the input_snapshot dict required by AC-4."""
    active = [e for e in evidence if e.link_status == LINK_STATUS_ACTIVE]
    return {
        "control_id": control_id,
        "control_ref": control_meta[1] if control_meta else "",
        "control_title": control_meta[2] if control_meta else "",
        "evidence_count": len(active),
        "evidence_records": [
            {
                "record_id": e.record_id,
                "source_system": e.source_system,
                "external_id": e.external_id,
                "title": e.title,
                "relevance_score": e.relevance_score,
            }
            for e in active
        ],
        "bias_vector_present": False,
        "generated_at": now.isoformat(),
    }


def _supersede_prior(conn, tenant_id, control_id: str) -> None:
    """Mark all prior non-superseded recommendations for this pair as superseded."""
    conn.execute(
        _SUPERSEDE_PRIOR,
        {"tenant_id": tenant_id, "control_id": control_id},
    )


def _build_insert_params(
    rec_id: str,
    tenant_id,
    control_id: str,
    scoring: ScoringResult,
    rationale: str,
    gaps: str | None,
    active_evidence: list[EvidenceResult],
    snapshot: dict,
    now: datetime,
) -> dict:
    """Assemble the parameter dict for the INSERT statement."""
    return {
        "recommendation_id": rec_id,
        "tenant_id": str(tenant_id),
        "control_id": control_id,
        "status": scoring.status,
        "confidence_level": scoring.confidence_level,
        "confidence_score": scoring.confidence_score,
        "rationale": rationale,
        "gaps": gaps,
        "evidence_ids": [e.record_id for e in active_evidence],
        "requires_review": scoring.requires_review,
        "input_snapshot": snapshot,
        "generated_at": now,
    }


def _row_to_output(row) -> RecommendationOutput:
    """Map a SELECT result row (by position) to a RecommendationOutput."""
    return RecommendationOutput(
        recommendation_id=str(row[0]),
        tenant_id=str(row[1]),
        control_id=str(row[2]),
        status=row[3],
        confidence_level=row[4],
        confidence_score=row[5],
        rationale=row[6],
        gaps=row[7],
        evidence_ids=list(row[8]) if row[8] is not None else [],
        requires_review=row[9],
        input_snapshot=row[10],
        generated_at=row[11],
        is_superseded=row[12],
    )
