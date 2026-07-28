"""Pydantic schemas for the evidence intake endpoints (KER-406).

Request bodies for linking, and response shapes for upload, listing, and link
management. Note what is absent: no field ever carries the raw uploaded bytes
or a tenant_id. The tenant comes from the verified JWT on every call, and the
original file is not retained at all (§16 decision 2) — only its extracted
text, which lives in context_records.body and is never echoed back in these
shapes.

How:   pytest tests/unit/api/test_evidence.py -v
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from config.constants import RELEVANCE_SCORE_MAX, RELEVANCE_SCORE_MIN


class EvidenceUploadResponse(BaseModel):
    """One uploaded evidence document as stored.

    ``deduplicated`` is True when the upload matched an existing record's
    content hash and the existing record was returned instead of a twin being
    created — the caller can tell "already had this" from "newly stored".
    """

    record_id: str
    source_system: str
    external_id: str | None
    record_type: str
    title: str | None
    content_hash: str | None
    created_at: datetime
    deduplicated: bool


class EvidenceListItem(BaseModel):
    """One evidence record in the library, with how many controls it supports.

    ``link_count`` is what makes orphans visible: a webhook-ingested record
    that was never linked shows zero, and is exactly what ``?linked=false``
    filters to.
    """

    record_id: str
    source_system: str
    external_id: str | None
    record_type: str
    title: str | None
    created_at: datetime
    link_count: int


class EvidenceListResponse(BaseModel):
    """The tenant's evidence library, newest first, with its total count."""

    items: list[EvidenceListItem]
    total: int


class EvidenceLinkRequest(BaseModel):
    """Attach one evidence record to one control, with an optional human score.

    relevance_score is bounded here as well as in the service so a bad value is
    a 422 at the edge rather than a ValueError deeper in. It is optional: a
    reviewer may link first and score later, and an unscored link is treated as
    DEFAULT_RELEVANCE_SCORE by the engine.
    """

    control_id: str
    relevance_score: float | None = Field(
        default=None, ge=RELEVANCE_SCORE_MIN, le=RELEVANCE_SCORE_MAX
    )
    note: str | None = None


class EvidenceLinkResponse(BaseModel):
    """The created or updated link between a control and an evidence record."""

    link_id: str
    control_id: str
    record_id: str
    relevance_score: float | None
    linked_by: str
    linked_at: datetime
