"""Evidence pack export service (KER-111) — assembles a deterministic, auditor-facing pack.

build_evidence_pack() gathers every control in a family (KER-109 system-of-record resolution)
with its evidence, human decisions, and KER-107 audit extract, in fully deterministic order;
serialise_pack() renders stable UTF-8 JSON bytes. The generation itself is recorded in the
audit ledger with control_id NULL, so repeated exports never change any control's extract.
Run tests with: pytest tests/unit/services/test_export_service.py -v
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

from src.api.schemas.export import (
    AuditExtract,
    ControlEntry,
    DecisionEntry,
    EvidenceEntry,
    EvidencePack,
    PackMetadata,
)
from src.services.audit_log import append_audit_entry, get_entries_by_control
from src.services.coverage_service import CoverageControl, get_coverage_controls
from src.services.evidence_service import get_evidence_for_control
from src.services.recommendation_service import get_recommendation
from src.services.tenant_context import resolve_and_set_tenant_context

DECIDED_BY_HUMAN: str = "human_confirmed"
DECIDED_BY_AI: str = "ai_unconfirmed"

_SELECT_DECISIONS = """
SELECT override_id, action_type, reviewer_role, created_at, justification_text
FROM overrides
WHERE tenant_id = :tenant_id
AND original_control_id = :control_id
ORDER BY created_at ASC, override_id ASC
"""


def build_evidence_pack(conn, session, control_family: str) -> EvidencePack:
    """Assemble the complete evidence pack for one control family (category).

    Resolves the tenant from the authenticated session and every control in the
    family via the KER-109 coverage pass, so statuses match the dashboard
    exactly. Controls are sorted by control_ref, evidence by linked_at,
    decisions and audit entries by created_at — all ascending with stable
    tiebreaks, so the pack content is deterministic. Records an
    export_generated ledger entry after assembly. Raises ValueError when the
    family contains no active controls, TenantContextMissingError on a bad session.
    """
    tenant_id = resolve_and_set_tenant_context(session, conn)
    controls = get_coverage_controls(conn, tenant_id, category=control_family)
    if not controls:
        raise ValueError(f"No active controls found for control family {control_family!r}.")
    ordered_controls = sorted(controls, key=lambda c: (c.control_ref, c.control_id))
    entries = [_build_control_entry(conn, tenant_id, control) for control in ordered_controls]
    export_id = str(uuid.uuid4())
    metadata = PackMetadata(
        tenant_id=str(tenant_id),
        control_family=control_family,
        generated_at=datetime.now(timezone.utc),
        export_id=export_id,
        kerno_version=os.environ.get("KERNO_VERSION", "dev"),
    )
    _record_export_audit_entry(conn, tenant_id, control_family, export_id, len(entries))
    return EvidencePack(metadata=metadata, controls=entries)


def serialise_pack(pack: EvidencePack) -> bytes:
    """Render the pack as deterministic UTF-8 JSON bytes.

    Sorted keys and compact separators guarantee that the same EvidencePack
    always produces byte-identical output, so an auditor can hash a pack and
    reproduce it. mode='json' renders datetimes as ISO-8601 strings.
    """
    return json.dumps(
        pack.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _build_control_entry(conn, tenant_id, control: CoverageControl) -> ControlEntry:
    recommendation = get_recommendation(conn, str(tenant_id), control.control_id)
    evidence = _collect_evidence(conn, tenant_id, control.control_id)
    decisions = _collect_decisions(conn, tenant_id, control.control_id)
    audit_extract = _collect_audit_extract(conn, tenant_id, control.control_id)
    return ControlEntry(
        control_id=control.control_id,
        control_ref=control.control_ref,
        title=control.title,
        category=control.category,
        system_of_record_status=control.status,
        confidence_level=control.confidence_level,
        rationale=recommendation.rationale if recommendation else None,
        gaps=recommendation.gaps if recommendation else None,
        decided_by=DECIDED_BY_HUMAN if control.human_confirmed else DECIDED_BY_AI,
        decided_at=_resolve_decided_at(control.human_confirmed, decisions, recommendation),
        evidence=evidence,
        decisions=decisions,
        audit_extract=audit_extract,
    )


def _resolve_decided_at(human_confirmed: bool, decisions: list[DecisionEntry], recommendation):
    """The moment the current status was decided: the latest human decision when
    one is the system of record, else the recommendation's generation time."""
    if human_confirmed and decisions:
        return decisions[-1].created_at
    if recommendation is not None:
        return recommendation.generated_at
    return None


def _collect_evidence(conn, tenant_id, control_id: str) -> list[EvidenceEntry]:
    # The evidence service orders by relevance for display; the pack re-sorts by
    # linked_at (with record_id tiebreak) for deterministic output.
    results = get_evidence_for_control(conn, tenant_id, control_id)
    ordered = sorted(results, key=lambda e: (e.linked_at, e.record_id))
    return [
        EvidenceEntry(
            evidence_id=item.record_id,
            source_system=item.source_system,
            external_ref=item.external_id,
            artifact_type=item.record_type,
            relevance_score=item.relevance_score,
            linked_at=item.linked_at,
            linked_by=item.linked_by,
        )
        for item in ordered
    ]


def _collect_decisions(conn, tenant_id, control_id: str) -> list[DecisionEntry]:
    rows = conn.execute(
        _SELECT_DECISIONS,
        {"tenant_id": str(tenant_id), "control_id": control_id},
    ).fetchall()
    return [
        DecisionEntry(
            override_id=str(row[0]),
            action_type=row[1],
            reviewer_role=row[2],
            created_at=row[3],
            justification_text=row[4],
        )
        for row in rows
    ]


def _collect_audit_extract(conn, tenant_id, control_id: str) -> list[AuditExtract]:
    # The ledger walks by sequence_number; the pack sorts by created_at with an
    # entry-id tiebreak, per the export contract.
    entries = get_entries_by_control(conn, tenant_id, control_id)
    ordered = sorted(entries, key=lambda e: (e.created_at, e.id))
    return [
        AuditExtract(
            entry_id=entry.id,
            actor_id=entry.actor_id,
            action_type=entry.action_type,
            object_id=entry.object_id,
            created_at=entry.created_at,
            entry_hash=entry.entry_hash,
        )
        for entry in ordered
    ]


def _record_export_audit_entry(
    conn, tenant_id, control_family: str, export_id: str, control_count: int
) -> None:
    append_audit_entry(
        conn,
        tenant_id,
        # actor_id pending per-user JWT claims (CLAUDE.md §8, KER-108 note).
        actor_id=None,
        actor_role="compliance_engineer",
        action_type="export_generated",
        object_type="evidence_pack",
        object_id=export_id,
        # control_id stays NULL: the generation entry must never appear in any
        # control's audit extract, or repeated exports would change pack content.
        control_id=None,
        after_state={
            "control_family": control_family,
            "control_count": control_count,
            "export_id": export_id,
        },
    )
