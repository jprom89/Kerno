"""Pydantic schemas for the AI-decision log query endpoint (KER-203).

Request filtering arrives as query parameters (validated in the router with
FastAPI Query types); these models define the response shape only: one
DecisionLogRecord per retained AI decision, wrapped in a DecisionLogResponse
with its count. No raw input snapshot ever appears here — only its SHA-256
fingerprint (GDPR alignment, KER-203 AC-6).

How:   exercised by tests/integration/test_ker203_ai_decision_log.py and the
       ai-decisions router.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class DecisionLogRecord(BaseModel):
    """One retained AI mapping decision as returned to the tenant.

    Field meanings mirror the ai_decision_log table: which control was mapped,
    the evidence refs the model cited, the fingerprint of what it saw, what it
    decided and how confidently, a short reasoning extract, the generating
    model version, and when the decision was recorded.
    """

    correlation_id: str
    control_id: str
    evidence_ids: list[str]
    input_snapshot_hash: str
    output_status: str
    confidence_score: float
    rationale_extract: str
    model_version: str
    created_at: datetime


class DecisionLogResponse(BaseModel):
    """The query result: matching decisions (newest first) and their count."""

    decisions: list[DecisionLogRecord]
    count: int
