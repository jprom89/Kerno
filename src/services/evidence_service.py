"""evidence_service.py — Evidence linking and retrieval for compliance controls.

What:  Creates, updates, retrieves, and soft-deletes links between compliance
       controls and ingested context records (evidence). Activates the
       control_evidence_links table that Document 11 created as a schema stub.

Why:   KER-104 requires that a compliance control can be linked to one or more
       pieces of ingested evidence, that those links carry who/when metadata and
       a relevance score, and that broken links (where the source record is
       deleted) are flagged rather than silently dropped (AC-4).

How to run or test:
    pytest tests/unit/services/test_evidence_service.py -v
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timezone

from config.constants import RELEVANCE_SCORE_MAX, RELEVANCE_SCORE_MIN
from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError  # noqa: F401  re-exported for callers

# ---------------------------------------------------------------------------
# Link-status constants (AC-4 — broken links must be flagged, never dropped)
# ---------------------------------------------------------------------------
LINK_STATUS_ACTIVE: str = "active"
LINK_STATUS_BROKEN: str = "broken"

# ---------------------------------------------------------------------------
# EvidenceResult — the return type of get_evidence_for_control
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class EvidenceResult:
    """All fields from a control_evidence_links row plus the joined context_record.

    link_status is LINK_STATUS_ACTIVE when the source context_record exists and
    is not soft-deleted, and LINK_STATUS_BROKEN otherwise. Fields from the
    context_record side (source_system through content_hash) may be None when
    the record has been hard-deleted and the LEFT JOIN returns no match.
    """

    link_id: str
    control_id: str
    record_id: str
    linked_by: str
    linked_at: datetime
    relevance_score: float | None
    note: str | None
    link_status: str
    source_system: str | None
    external_id: str | None
    record_type: str | None
    title: str | None
    body: str | None
    fetched_at: datetime | None
    content_hash: str | None


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SELECT_EXISTING_LINK = """
SELECT link_id
FROM control_evidence_links
WHERE control_id = :control_id
AND record_id = :record_id
AND removed_at IS NULL
"""

_INSERT_LINK = """
INSERT INTO control_evidence_links
    (link_id, control_id, record_id, linked_by, linked_at, relevance_score, note)
VALUES
    (:link_id, :control_id, :record_id, :linked_by, :linked_at, :relevance_score, :note)
"""

_UPDATE_LINK = """
UPDATE control_evidence_links
SET linked_by = :linked_by,
    linked_at = :linked_at,
    relevance_score = :relevance_score,
    note = :note
WHERE link_id = :link_id
"""

_SELECT_LINK_BY_ID = """
SELECT link_id
FROM control_evidence_links
WHERE link_id = :link_id
AND removed_at IS NULL
"""

_UPDATE_REMOVED_AT = """
UPDATE control_evidence_links
SET removed_at = :removed_at
WHERE link_id = :link_id
"""

_SELECT_CONTROLS_FOR_RECORD = """
SELECT
    cc.control_id, cc.framework, cc.control_ref, cc.category,
    cc.title, cc.obligation_text, cc.entity_types, cc.is_active
FROM control_evidence_links cel
JOIN compliance_controls cc ON cc.control_id = cel.control_id
WHERE cel.record_id = :record_id
AND cel.removed_at IS NULL
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def link_evidence(
    conn,
    tenant_id,
    control_id: str,
    record_id: str,
    linked_by: str,
    relevance_score: float | None = None,
    note: str | None = None,
) -> str:
    """Create or update the link between a control and a context record.

    Validates relevance_score before any DB access. Sets tenant context, then
    checks whether a link already exists for (control_id, record_id). If yes,
    updates linked_by, linked_at, relevance_score, and note. If no, inserts a
    new row. Returns the link_id in both cases.
    Raises ValueError if relevance_score is outside [RELEVANCE_SCORE_MIN, RELEVANCE_SCORE_MAX].
    Raises TenantContextMissingError if tenant_id is None or empty.
    """
    _validate_relevance_score(relevance_score)
    set_tenant_context(conn, tenant_id)
    existing_id = _find_existing_link(conn, control_id, record_id)
    if existing_id is not None:
        _update_existing_link(conn, existing_id, linked_by, relevance_score, note)
        return existing_id
    return _insert_new_link(conn, control_id, record_id, linked_by, relevance_score, note)


def get_evidence_for_control(
    conn,
    tenant_id,
    control_id: str,
    source_system: str | None = None,
    record_type: str | None = None,
    min_relevance: float | None = None,
) -> list[EvidenceResult]:
    """Return all evidence linked to a control, including broken links (AC-4).

    Joins control_evidence_links with context_records (LEFT JOIN so missing
    records are still returned as broken links). Sets link_status to
    LINK_STATUS_BROKEN when the context_record is deleted or missing.
    Optional filters narrow results by source_system, record_type, and
    minimum relevance score. Results ordered by relevance_score DESC NULLS LAST,
    then linked_at DESC.
    """
    set_tenant_context(conn, tenant_id)
    sql, params = _build_evidence_query(control_id, source_system, record_type, min_relevance)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_evidence_result(row) for row in rows]


def get_controls_for_record(conn, tenant_id, record_id: str) -> list[dict]:
    """Return all compliance controls linked to a given context_record.

    Supports the reverse lookup — given a piece of evidence, find which controls
    it has been linked to. Only returns links where removed_at IS NULL.
    """
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(
        _SELECT_CONTROLS_FOR_RECORD,
        {"record_id": record_id},
    ).fetchall()
    return [_control_row_to_dict(row) for row in rows]


def remove_link(conn, tenant_id, link_id: str) -> bool:
    """Soft-delete a link by setting its removed_at timestamp.

    Returns True if the link was found and updated, False if no active link
    with that link_id exists. Does not hard-delete rows so audit history is
    preserved.
    """
    set_tenant_context(conn, tenant_id)
    row = conn.execute(_SELECT_LINK_BY_ID, {"link_id": link_id}).fetchone()
    if row is None:
        return False
    conn.execute(
        _UPDATE_REMOVED_AT,
        {"removed_at": datetime.now(timezone.utc), "link_id": link_id},
    )
    return True


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_relevance_score(score: float | None) -> None:
    """Raise ValueError if score is outside the [RELEVANCE_SCORE_MIN, RELEVANCE_SCORE_MAX] range.

    None is allowed — it means no score was assigned. Only non-None values
    are range-checked.
    """
    if score is not None and not (RELEVANCE_SCORE_MIN <= score <= RELEVANCE_SCORE_MAX):
        raise ValueError(
            f"relevance_score must be between {RELEVANCE_SCORE_MIN} and "
            f"{RELEVANCE_SCORE_MAX}, got {score}."
        )


def _find_existing_link(conn, control_id: str, record_id: str) -> str | None:
    """Return the existing link_id for (control_id, record_id), or None if absent."""
    row = conn.execute(
        _SELECT_EXISTING_LINK,
        {"control_id": control_id, "record_id": record_id},
    ).fetchone()
    return str(row[0]) if row is not None else None


def _insert_new_link(
    conn,
    control_id: str,
    record_id: str,
    linked_by: str,
    relevance_score: float | None,
    note: str | None,
) -> str:
    """Insert a new link row and return its Python-generated UUID string."""
    link_id = str(uuid.uuid4())
    conn.execute(
        _INSERT_LINK,
        {
            "link_id": link_id,
            "control_id": control_id,
            "record_id": record_id,
            "linked_by": linked_by,
            "linked_at": datetime.now(timezone.utc),
            "relevance_score": relevance_score,
            "note": note,
        },
    )
    return link_id


def _update_existing_link(
    conn,
    link_id: str,
    linked_by: str,
    relevance_score: float | None,
    note: str | None,
) -> None:
    """Update mutable fields on an existing link row."""
    conn.execute(
        _UPDATE_LINK,
        {
            "link_id": link_id,
            "linked_by": linked_by,
            "linked_at": datetime.now(timezone.utc),
            "relevance_score": relevance_score,
            "note": note,
        },
    )


def _build_evidence_query(
    control_id: str,
    source_system: str | None,
    record_type: str | None,
    min_relevance: float | None,
) -> tuple[str, dict]:
    """Build the LEFT JOIN evidence SELECT with any active filters applied.

    Returns (sql_string, params_dict). source_system and record_type filters
    include an OR cr.record_id IS NULL clause so hard-deleted records (where
    the LEFT JOIN returns no match) are still included as broken links (AC-4).
    """
    base = (
        "SELECT cel.link_id, cel.control_id, cel.record_id, cel.linked_by, "
        "cel.linked_at, cel.relevance_score, cel.note, "
        "cr.is_deleted, cr.source_system, cr.external_id, cr.record_type, "
        "cr.title, cr.body, cr.fetched_at, cr.content_hash "
        "FROM control_evidence_links cel "
        "LEFT JOIN context_records cr ON cr.record_id = cel.record_id "
        "WHERE cel.control_id = :control_id AND cel.removed_at IS NULL"
    )
    clauses: list[str] = []
    params: dict = {"control_id": control_id}

    if source_system is not None:
        clauses.append("(cr.source_system = :source_system OR cr.record_id IS NULL)")
        params["source_system"] = source_system
    if record_type is not None:
        clauses.append("(cr.record_type = :record_type OR cr.record_id IS NULL)")
        params["record_type"] = record_type
    if min_relevance is not None:
        clauses.append("cel.relevance_score >= :min_relevance")
        params["min_relevance"] = min_relevance

    filter_sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    order_sql = " ORDER BY cel.relevance_score DESC NULLS LAST, cel.linked_at DESC"
    return base + filter_sql + order_sql, params


def _row_to_evidence_result(row) -> EvidenceResult:
    """Map a LEFT JOIN result row (by position) to an EvidenceResult dataclass.

    row[7] is cr.is_deleted: None means the context_record was not found (hard-
    deleted), True means it is soft-deleted. Both are LINK_STATUS_BROKEN.
    """
    is_deleted = row[7]
    link_status = LINK_STATUS_BROKEN if (is_deleted is None or is_deleted) else LINK_STATUS_ACTIVE
    return EvidenceResult(
        link_id=str(row[0]),
        control_id=str(row[1]),
        record_id=str(row[2]),
        linked_by=row[3],
        linked_at=row[4],
        relevance_score=row[5],
        note=row[6],
        link_status=link_status,
        source_system=row[8],
        external_id=row[9],
        record_type=row[10],
        title=row[11],
        body=row[12],
        fetched_at=row[13],
        content_hash=row[14],
    )


def _control_row_to_dict(row) -> dict:
    """Map a compliance_controls JOIN result row (by position) to a plain dict."""
    return {
        "control_id": str(row[0]),
        "framework": row[1],
        "control_ref": row[2],
        "category": row[3],
        "title": row[4],
        "obligation_text": row[5],
        "entity_types": row[6],
        "is_active": row[7],
    }
