"""AI control mapping engine — sends a structured prompt to an EU-native LLM (Mistral) and persists the result.

Call map_control() with a ControlInput and evidence list; raises MappingError on any
LLM or validation failure, TenantContextMissingError when tenant context is missing.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from mistralai.client.errors import MistralError

from config.constants import (
    HIGH_CONFIDENCE_THRESHOLD,
    LOW_CONFIDENCE_THRESHOLD,
    MAX_REASONING_WORDS,
    MEDIUM_CONFIDENCE_THRESHOLD,
)
from src.db.rls import set_tenant_context
from src.exceptions import MappingError, TenantContextMissingError  # noqa: F401
from src.models.recommendation import CONFIDENCE_HIGH, CONFIDENCE_LOW, CONFIDENCE_MEDIUM
from src.services.audit_log import write_audit_event
from src.services.llm_client import get_llm_client

logger = logging.getLogger(__name__)

_VALID_STATUSES = frozenset({"met", "partial", "gap"})

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

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

_SUPERSEDE_PRIOR = """
UPDATE recommendations
SET is_superseded = TRUE
WHERE tenant_id = :tenant_id
AND control_id = :control_id
AND is_superseded = FALSE
"""

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ControlInput:
    """Compliance control passed into the mapping engine."""

    control_id: str
    framework: str
    control_ref: str
    title: str
    description: str


@dataclasses.dataclass(frozen=True)
class EvidenceInput:
    """Single evidence record retrieved from context_records and passed to the LLM."""

    record_id: str
    title: str
    body: str
    source_system: str


@dataclasses.dataclass(frozen=True)
class MappingRecommendation:
    """Immutable return type for map_control — the LLM's mapping decision plus derived fields."""

    recommendation_id: str
    control_id: str
    status: str
    confidence: float
    confidence_level: str
    evidence_ids: list[str]
    reasoning: str
    gaps: list[str]
    requires_human_review: bool
    generated_at: datetime


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def map_control(
    conn,
    tenant_id,
    control: ControlInput,
    evidence: list[EvidenceInput],
) -> MappingRecommendation:
    """Map a compliance control to its evidence via LLM and persist the result.

    Sets tenant context as the first DB call, then calls the LLM, validates
    the JSON response, marks prior recommendations superseded, inserts a new
    row, and emits an audit event. Raises MappingError on any LLM or parse
    failure, TenantContextMissingError if tenant_id is None or not a valid UUIDv4.
    """
    set_tenant_context(conn, tenant_id)
    model_id = _get_model_id()
    now = datetime.now(timezone.utc)
    messages = _build_prompt(control, evidence)
    raw_response = _call_llm(messages, model_id)
    parsed = _parse_llm_response(raw_response)
    confidence_level = _derive_confidence_level(parsed["confidence"])
    requires_human_review = parsed["confidence"] < LOW_CONFIDENCE_THRESHOLD
    snapshot = _build_input_snapshot(control, evidence, model_id, now)
    rec_id = str(uuid.uuid4())
    _supersede_prior(conn, tenant_id, control.control_id)
    _persist_recommendation(
        conn, rec_id, tenant_id, control.control_id,
        parsed, confidence_level, requires_human_review, snapshot, now,
    )
    write_audit_event(
        conn, str(tenant_id), "recommendation_generated",
        {"recommendation_id": rec_id, "control_id": control.control_id, "status": parsed["status"]},
    )
    return _assemble_result(rec_id, control.control_id, parsed, confidence_level, requires_human_review, now)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _build_prompt(control: ControlInput, evidence: list[EvidenceInput]) -> list[dict]:
    system_message = (
        "You are a compliance expert mapping technical security controls to regulatory "
        "frameworks. Respond with valid JSON only, no additional text or explanation."
    )
    user_message = (
        f"Framework: {control.framework}\n"
        f"Control reference: {control.control_ref}\n"
        f"Control title: {control.title}\n"
        f"Control description: {control.description}\n\n"
        f"Evidence records:\n{_format_evidence(evidence)}\n\n"
        "Analyse whether the evidence covers this control. "
        "Respond with a JSON object containing exactly these fields:\n"
        '- "status": one of "met", "partial", or "gap"\n'
        '- "confidence": a float between 0.0 and 1.0\n'
        '- "evidence_ids": list of record_id strings from the evidence that support this mapping\n'
        f'- "reasoning": explanation in {MAX_REASONING_WORDS} words or fewer\n'
        '- "gaps": list of strings describing missing evidence (empty list when status is "met")'
    )
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


def _format_evidence(evidence: list[EvidenceInput]) -> str:
    if not evidence:
        return "(no evidence records provided)"
    lines = []
    for idx, rec in enumerate(evidence, start=1):
        lines.append(
            f"{idx}. [record_id: {rec.record_id}] "
            f"[source: {rec.source_system}] "
            f"{rec.title}: {rec.body}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _get_model_id() -> str:
    model_id = os.environ.get("KERNO_LLM_MODEL")
    if not model_id:
        raise MappingError("KERNO_LLM_MODEL environment variable is not set")
    return model_id


def _call_llm(messages: list[dict], model_id: str) -> str:
    """Call the Mistral chat completion endpoint and return raw response content.

    Obtains the client from the llm_client factory so provider configuration lives in
    one place; a fresh client per call keeps tests able to patch it without module-level
    state. Raises MappingError on any LLM API error.
    """
    try:
        client = get_llm_client()
        response = client.chat.complete(
            model=model_id,
            messages=messages,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content
    except (MistralError, httpx.HTTPError) as exc:
        raise MappingError(f"LLM API call failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Response parsing and validation
# ---------------------------------------------------------------------------


def _parse_llm_response(raw_json: str) -> dict:
    """Parse and validate the LLM JSON response; raise MappingError on any problem.

    Returns the validated dict with confidence normalised to float.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise MappingError(f"LLM returned invalid JSON: {exc}") from exc
    _validate_status(data)
    _validate_confidence(data)
    _validate_evidence_ids(data)
    _validate_reasoning(data)
    _validate_gaps(data)
    return {**data, "confidence": float(data["confidence"])}


def _validate_status(data: dict) -> None:
    status = data.get("status")
    if status not in _VALID_STATUSES:
        raise MappingError(
            f"Invalid status value: {status!r}. Must be one of {sorted(_VALID_STATUSES)}"
        )


def _validate_confidence(data: dict) -> None:
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise MappingError(f"confidence must be a number, got {type(confidence).__name__}")
    if not 0.0 <= float(confidence) <= 1.0:
        raise MappingError(f"confidence must be between 0.0 and 1.0, got {confidence}")


def _validate_evidence_ids(data: dict) -> None:
    ids = data.get("evidence_ids")
    if not isinstance(ids, list):
        raise MappingError(f"evidence_ids must be a list, got {type(ids).__name__}")
    for item in ids:
        if not isinstance(item, str):
            raise MappingError(f"each evidence_id must be a string, got {type(item).__name__}")


def _validate_reasoning(data: dict) -> None:
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise MappingError("reasoning must be a non-empty string")


def _validate_gaps(data: dict) -> None:
    gaps = data.get("gaps")
    if not isinstance(gaps, list):
        raise MappingError(f"gaps must be a list, got {type(gaps).__name__}")
    for item in gaps:
        if not isinstance(item, str):
            raise MappingError(f"each gap must be a string, got {type(item).__name__}")


# ---------------------------------------------------------------------------
# Confidence level derivation
# ---------------------------------------------------------------------------


def _derive_confidence_level(confidence: float) -> str:
    if confidence >= HIGH_CONFIDENCE_THRESHOLD:
        return CONFIDENCE_HIGH
    if confidence >= MEDIUM_CONFIDENCE_THRESHOLD:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _supersede_prior(conn, tenant_id, control_id: str) -> None:
    conn.execute(
        _SUPERSEDE_PRIOR,
        {"tenant_id": str(tenant_id), "control_id": control_id},
    )


def _persist_recommendation(
    conn,
    rec_id: str,
    tenant_id,
    control_id: str,
    parsed: dict,
    confidence_level: str,
    requires_human_review: bool,
    snapshot: dict,
    now: datetime,
) -> None:
    """Insert the recommendation row, mapping LLM fields to the existing table schema.

    The existing table uses `rationale` (not `reasoning`) and `gaps TEXT` (not JSONB),
    so gaps list is serialised to JSON string and reasoning is stored as rationale.
    """
    gaps_text = json.dumps(parsed["gaps"]) if parsed["gaps"] else None
    conn.execute(
        _INSERT_RECOMMENDATION,
        {
            "recommendation_id": rec_id,
            "tenant_id": str(tenant_id),
            "control_id": control_id,
            "status": parsed["status"],
            "confidence_level": confidence_level,
            "confidence_score": parsed["confidence"],
            "rationale": parsed["reasoning"],
            "gaps": gaps_text,
            "evidence_ids": parsed["evidence_ids"],
            "requires_review": requires_human_review,
            "input_snapshot": json.dumps(snapshot),
            "generated_at": now,
        },
    )


def _build_input_snapshot(
    control: ControlInput,
    evidence: list[EvidenceInput],
    model_id: str,
    now: datetime,
) -> dict:
    # model_id is stored here because the recommendations table has no model_id column;
    # the snapshot is the canonical source for audit reproducibility (AC-4).
    return {
        "model_id": model_id,
        "control_id": control.control_id,
        "framework": control.framework,
        "control_ref": control.control_ref,
        "control_title": control.title,
        "evidence_count": len(evidence),
        "evidence_records": [
            {
                "record_id": e.record_id,
                "title": e.title,
                "source_system": e.source_system,
            }
            for e in evidence
        ],
        "generated_at": now.isoformat(),
    }


def _assemble_result(
    rec_id: str,
    control_id: str,
    parsed: dict,
    confidence_level: str,
    requires_human_review: bool,
    now: datetime,
) -> MappingRecommendation:
    return MappingRecommendation(
        recommendation_id=rec_id,
        control_id=control_id,
        status=parsed["status"],
        confidence=parsed["confidence"],
        confidence_level=confidence_level,
        evidence_ids=parsed["evidence_ids"],
        reasoning=parsed["reasoning"],
        gaps=parsed["gaps"],
        requires_human_review=requires_human_review,
        generated_at=now,
    )
