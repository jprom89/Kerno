"""FastAPI router for evidence intake, mounted at /api/v1/evidence (KER-406).

What:  upload a document, list the tenant's evidence library (including the
       orphans nothing was ever linked to), and link or unlink a record to a
       control.
Why:   before this, evidence could only arrive by webhook and could not be
       linked at all — so a customer could ingest perfectly and still score
       every control "gap, no evidence". This is the surface that makes the
       evidence base usable by a human.
How:   thin translation only. Text extraction lives in evidence_intake, the
       link upsert in evidence_service; the tenant and the acting user always
       come from the verified JWT, never the request.
       pytest tests/unit/api/test_evidence.py -v
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from config.constants import UPLOAD_SOURCE_SYSTEM
from src.api.dependencies import get_conn, get_tenant_id, require_role
# get_reviewer_id is the existing verified-JWT user identity (KER-202); reused
# so linked_by names the same verified actor that override attribution does.
from src.api.routers.overrides import get_reviewer_id
from src.api.schemas.evidence import (
    EvidenceLinkRequest,
    EvidenceLinkResponse,
    EvidenceListItem,
    EvidenceListResponse,
    EvidenceUploadResponse,
)
from src.db.rls import set_tenant_context
from src.exceptions import EvidenceExtractionError, UnsupportedFileTypeError
from src.services.audit_log import append_audit_entry
from src.services.evidence_intake import (
    EVIDENCE_CAPABLE_ROLES,
    content_fingerprint,
    external_id_for,
    extract_text,
)
from src.services.evidence_service import link_evidence

router = APIRouter()

_SELECT_BY_CONTENT_HASH = """
SELECT record_id, source_system, external_id, record_type, title, content_hash, created_at
FROM context_records
WHERE tenant_id = :tenant_id AND content_hash = :content_hash AND is_deleted = FALSE
LIMIT 1
"""

_INSERT_RECORD = """
INSERT INTO context_records
    (record_id, tenant_id, source_system, external_id, record_type, title, body, content_hash)
VALUES
    (:record_id, :tenant_id, :source_system, :external_id, :record_type, :title, :body, :content_hash)
"""

_SELECT_RECORD_CREATED_AT = """
SELECT created_at FROM context_records WHERE record_id = :record_id
"""

# link_count drives the ?linked= filter and is what makes orphans visible.
_LIST_RECORDS = """
SELECT r.record_id, r.source_system, r.external_id, r.record_type, r.title,
       r.created_at,
       (SELECT count(*) FROM control_evidence_links cel
        WHERE cel.record_id = r.record_id AND cel.removed_at IS NULL) AS link_count
FROM context_records r
WHERE r.tenant_id = :tenant_id AND r.is_deleted = FALSE
"""

_LINKED_FILTER = """
  AND EXISTS (SELECT 1 FROM control_evidence_links cel
              WHERE cel.record_id = r.record_id AND cel.removed_at IS NULL)
"""

_UNLINKED_FILTER = """
  AND NOT EXISTS (SELECT 1 FROM control_evidence_links cel
                  WHERE cel.record_id = r.record_id AND cel.removed_at IS NULL)
"""

_LIST_ORDER = " ORDER BY r.created_at DESC"

# Pre-validation reads (KER-406 AC-4). Both run before link_evidence so an
# unknown id is an honest 404 rather than a driver exception reinterpreted as
# one — see the §16 design decision 8 note on why the two ids fail differently.
_SELECT_RECORD_FOR_LINK = """
SELECT record_id FROM context_records
WHERE record_id = :record_id AND tenant_id = :tenant_id AND is_deleted = FALSE
"""

_SELECT_CONTROL_FOR_LINK = """
SELECT control_id FROM compliance_controls
WHERE control_id = :control_id AND is_active = TRUE
"""

_SELECT_LINK = """
SELECT link_id, relevance_score, linked_by, linked_at
FROM control_evidence_links
WHERE control_id = :control_id AND record_id = :record_id
"""

_SOFT_DELETE_LINK = """
UPDATE control_evidence_links
SET removed_at = now()
WHERE control_id = :control_id AND record_id = :record_id AND removed_at IS NULL
RETURNING link_id
"""

_NOT_FOUND_DETAIL = "evidence record or control not found"


@router.post("", status_code=201)
def upload_evidence(
    file: UploadFile = File(...),
    record_type: str = Form(...),
    title: str | None = Form(default=None),
    tenant_id: str = Depends(get_tenant_id),
    user_id: str = Depends(get_reviewer_id),
    rbac_role: str = Depends(require_role(*EVIDENCE_CAPABLE_ROLES)),
    conn=Depends(get_conn),
) -> EvidenceUploadResponse:
    """Store one uploaded document as an evidence record for this tenant.

    Extracts text (the original bytes are not retained — §16 decision 2),
    fingerprints it, and returns the EXISTING record when the same content was
    already uploaded rather than creating a duplicate. Unsupported file type →
    422; unreadable content → 422; oversize → 413.
    """
    content = file.file.read()
    text = _extract_or_reject(file.filename or "", content)
    content_hash = content_fingerprint(text)
    set_tenant_context(conn, tenant_id)

    existing = conn.execute(
        _SELECT_BY_CONTENT_HASH, {"tenant_id": tenant_id, "content_hash": content_hash}
    ).fetchone()
    if existing is not None:
        return _upload_response(existing, deduplicated=True)

    record_id = str(uuid.uuid4())
    conn.execute(
        _INSERT_RECORD,
        {
            "record_id": record_id,
            "tenant_id": tenant_id,
            "source_system": UPLOAD_SOURCE_SYSTEM,
            "external_id": external_id_for(file.filename or "upload"),
            "record_type": record_type,
            "title": title or external_id_for(file.filename or "upload"),
            "body": text,
            "content_hash": content_hash,
        },
    )
    created_at = conn.execute(
        _SELECT_RECORD_CREATED_AT, {"record_id": record_id}
    ).fetchone()[0]
    _record_evidence_audit(
        conn, tenant_id, user_id, rbac_role, "evidence_uploaded", record_id,
        {"external_id": external_id_for(file.filename or ""), "record_type": record_type},
    )
    return EvidenceUploadResponse(
        record_id=record_id,
        source_system=UPLOAD_SOURCE_SYSTEM,
        external_id=external_id_for(file.filename or "upload"),
        record_type=record_type,
        title=title or external_id_for(file.filename or "upload"),
        content_hash=content_hash,
        created_at=created_at,
        deduplicated=False,
    )


@router.get("")
def list_evidence(
    linked: bool | None = Query(default=None),
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> EvidenceListResponse:
    """List the tenant's evidence, newest first, optionally by link status.

    ``?linked=false`` surfaces orphans — records nothing has ever been linked
    to, including webhook deliveries that arrived without a control_ref. Not
    gated by EVIDENCE_CAPABLE_ROLES: auditors are read-only but must be able
    to see the evidence base.
    """
    set_tenant_context(conn, tenant_id)
    sql = _LIST_RECORDS
    if linked is True:
        sql += _LINKED_FILTER
    elif linked is False:
        sql += _UNLINKED_FILTER
    rows = conn.execute(sql + _LIST_ORDER, {"tenant_id": tenant_id}).fetchall()
    items = [
        EvidenceListItem(
            record_id=str(row[0]), source_system=row[1], external_id=row[2],
            record_type=row[3], title=row[4], created_at=row[5], link_count=int(row[6]),
        )
        for row in rows
    ]
    return EvidenceListResponse(items=items, total=len(items))


@router.post("/{record_id}/links", status_code=201)
def create_link(
    record_id: uuid.UUID,
    body: EvidenceLinkRequest,
    tenant_id: str = Depends(get_tenant_id),
    user_id: str = Depends(get_reviewer_id),
    rbac_role: str = Depends(require_role(*EVIDENCE_CAPABLE_ROLES)),
    conn=Depends(get_conn),
) -> EvidenceLinkResponse:
    """Link one evidence record to one control, with an optional relevance score.

    Both ids are pre-validated (AC-4): either miss returns an IDENTICAL 404, so
    a record belonging to another tenant is indistinguishable from one that
    does not exist. Re-linking the same pair updates it rather than
    duplicating — the (control_id, record_id) uniqueness is DB-enforced.
    linked_by records the verified JWT user, never a free string.
    """
    _require_linkable(conn, tenant_id, str(record_id), body.control_id)
    link_id = link_evidence(
        conn,
        tenant_id,
        control_id=body.control_id,
        record_id=str(record_id),
        linked_by=user_id,
        relevance_score=body.relevance_score,
        note=body.note,
    )
    row = conn.execute(
        _SELECT_LINK, {"control_id": body.control_id, "record_id": str(record_id)}
    ).fetchone()
    _record_evidence_audit(
        conn, tenant_id, user_id, rbac_role, "evidence_linked", str(record_id),
        {"control_id": body.control_id, "relevance_score": body.relevance_score},
    )
    return EvidenceLinkResponse(
        link_id=str(link_id), control_id=body.control_id, record_id=str(record_id),
        relevance_score=row[1], linked_by=row[2], linked_at=row[3],
    )


@router.delete("/{record_id}/links/{control_id}", status_code=204)
def remove_link(
    record_id: uuid.UUID,
    control_id: uuid.UUID,
    tenant_id: str = Depends(get_tenant_id),
    user_id: str = Depends(get_reviewer_id),
    rbac_role: str = Depends(require_role(*EVIDENCE_CAPABLE_ROLES)),
    conn=Depends(get_conn),
) -> None:
    """Detach an evidence record from a control by setting removed_at.

    A soft delete: the row survives so the history of what was once linked —
    and by whom — is never erased. An already-removed or nonexistent link is a
    404. Returns 204 with no body.
    """
    set_tenant_context(conn, tenant_id)
    row = conn.execute(
        _SOFT_DELETE_LINK,
        {"control_id": str(control_id), "record_id": str(record_id)},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)
    _record_evidence_audit(
        conn, tenant_id, user_id, rbac_role, "evidence_unlinked", str(record_id),
        {"control_id": str(control_id)},
    )


def _extract_or_reject(filename: str, content: bytes) -> str:
    """Return the document's text, translating extraction failures to HTTP codes.

    Unsupported type and unreadable content are both the caller's problem
    (422); an oversize upload is 413, which is the honest status for a payload
    the server refuses on size alone.
    """
    try:
        return extract_text(filename, content)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except EvidenceExtractionError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc))


def _require_linkable(conn, tenant_id: str, record_id: str, control_id: str) -> None:
    """Pre-validate both ids before any write; raise an identical 404 on either miss.

    The record read is tenant-scoped (RLS plus an explicit filter), so another
    tenant's record is "not found" exactly like a nonexistent one — preserving
    the no-existence-oracle property the database already enforces. Deliberately
    NOT implemented by catching driver exceptions: a nonexistent control raises
    ForeignKeyViolation while a nonexistent record raises InsufficientPrivilege,
    and remapping either to 404 would hide genuine faults (§16 decision 8).
    """
    set_tenant_context(conn, tenant_id)
    record = conn.execute(
        _SELECT_RECORD_FOR_LINK, {"record_id": record_id, "tenant_id": tenant_id}
    ).fetchone()
    control = conn.execute(
        _SELECT_CONTROL_FOR_LINK, {"control_id": control_id}
    ).fetchone()
    if record is None or control is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)


def _upload_response(row, deduplicated: bool) -> EvidenceUploadResponse:
    """Build the upload response from an existing context_records row."""
    return EvidenceUploadResponse(
        record_id=str(row[0]), source_system=row[1], external_id=row[2],
        record_type=row[3], title=row[4], content_hash=row[5], created_at=row[6],
        deduplicated=deduplicated,
    )


def _record_evidence_audit(
    conn, tenant_id: str, user_id: str, rbac_role: str,
    action_type: str, record_id: str, after_state: dict,
) -> None:
    """Append the KER-107 ledger entry for one evidence write (AC-7).

    Runs on the caller's transaction so the ledger entry and the write it
    records commit or roll back together, attributing the verified JWT user.
    """
    append_audit_entry(
        conn,
        tenant_id,
        actor_id=user_id,
        actor_role=rbac_role,
        action_type=action_type,
        object_type="context_record",
        object_id=record_id,
        control_id=None,
        after_state=after_state,
    )
