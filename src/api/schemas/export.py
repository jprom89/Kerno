"""Evidence pack schema (KER-111) — the documented, validating structure of an exported pack.

These pydantic models ARE the pack's schema: serialise_pack() output round-trips through
EvidencePack.model_validate_json(), and EvidencePack.model_json_schema() emits the formal
JSON Schema for external auditors. A pack is self-contained — every entry carries enough
context to verify coverage without querying the live system.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PackMetadata(BaseModel):
    """Identifies one export generation. export_id is unique per generation."""

    tenant_id: str
    control_family: str
    generated_at: datetime
    export_id: str
    kerno_version: str


class EvidenceEntry(BaseModel):
    """One evidence reference linked to the control, with provenance."""

    model_config = ConfigDict(from_attributes=True)

    evidence_id: str
    source_system: str | None
    external_ref: str | None
    artifact_type: str | None
    relevance_score: float | None
    linked_at: datetime
    linked_by: str


class DecisionEntry(BaseModel):
    """One human decision (KER-106 override). justification_text is stored anonymised."""

    override_id: str
    action_type: str
    reviewer_role: str
    created_at: datetime
    justification_text: str | None


class AuditExtract(BaseModel):
    """One KER-107 ledger entry relevant to the control. entry_hash lets an
    auditor cross-check the extract against the tamper-evident chain."""

    entry_id: str
    actor_id: str | None
    action_type: str
    object_id: str | None
    created_at: datetime
    entry_hash: str


class ControlEntry(BaseModel):
    """One control with its system-of-record status and all supporting records.

    decided_by is 'human_confirmed' when a KER-106 override is the system of
    record, 'ai_unconfirmed' otherwise. Empty evidence/decisions/audit lists
    mean none exist — controls are never silently dropped from a pack.
    """

    control_id: str
    control_ref: str
    title: str
    category: str
    system_of_record_status: str
    confidence_level: str | None
    rationale: str | None
    gaps: str | None
    decided_by: str
    decided_at: datetime | None
    evidence: list[EvidenceEntry]
    decisions: list[DecisionEntry]
    audit_extract: list[AuditExtract]


class EvidencePack(BaseModel):
    """The complete export: metadata plus every control in the family."""

    metadata: PackMetadata
    controls: list[ControlEntry]
