"""
meeting_pack_service.py

What: Builds the monthly exception-review agenda from live coverage, linked
 evidence, and open recommendations, and renders it as meeting notes.
Why: The founder delivering the NIS2 operating cycle is not a GRC expert.
 The pack tells them what each control is, what Kerno scored, what evidence
 exists, and the exact question to ask — so they chair from the screen.
How: Call build_meeting_pack(conn, tenant_id). Tests:
 pytest tests/unit/services/test_meeting_pack_service.py -v
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from config.constants import (
    MEETING_PACK_RATIONALE_EXCERPT_CHARS,
    MEETING_REVIEW_MINUTES,
    RECOMMENDATIONS_MAX_PAGE_SIZE,
)
from src.models.compliance_control import FRAMEWORK_NIS2
from src.models.recommendation import STATUS_GAP, STATUS_MET, STATUS_PARTIAL
from src.services.control_inspection import InspectionItem, approve_rule_for, inspection_items_for
from src.services.coverage_service import CoverageControl, get_coverage_controls
from src.services.evidence_service import get_evidence_for_control
from src.services.recommendation_service import OpenRecommendation, list_open_recommendations

# Read aloud at the start of every review. Not a compliance certificate.
MEETING_PACK_PREAMBLE = (
    "A NIS2 control is one legal requirement for this service (for example: "
    "can you restore it, and have you tested that?). Green means linked "
    "evidence currently supports it. Amber means some evidence, not enough. "
    "Red means nothing useful is linked, or a human already marked a gap. "
    "This file is not a determination that NIS2 applies and does not "
    "establish compliance with §30 or §38 BSIG."
)

# What to say in the room when the catalogue title is not enough.
# Fallback for any other ref is the control title.
_CONTROL_IN_PLAIN_ENGLISH: dict[str, str] = {
    "NIS2-Art20-1": "Who in management is accountable, and is that written down?",
    "NIS2-Art20-2": "Have the directors had cyber training? Attendance, or a gap.",
    "NIS2-Art21-1": "Is there a list of what could go wrong for this service?",
    "NIS2-Art21-2-a": "Is there an information-security or IT policy on file?",
    "NIS2-Art23-1": "Is there a written playbook for notifying a CSIRT / BSI?",
    "NIS2-Art23-4": "Does that playbook include the 24h / 72h notification times?",
    "NIS2-Art21-2-d": "Which vendors does this service depend on?",
    "NIS2-Art22-1": (
        "Are they in an ENISA/Cooperation Group assessment, or do they follow "
        "BSI/ENISA advisories? A vendor list is not this article."
    ),
    "NIS2-Art21-2-e": "How do you patch, and is there a recent test or pentest?",
    "NIS2-Art21-2-j": "Where does production run, and is admin access using MFA?",
    "NIS2-Art21-2-b": "Who is on-call, and is there an incident channel?",
    "NIS2-Art21-2-c": "Are there backups, and when was the last restore test?",
}


@dataclasses.dataclass(frozen=True)
class MeetingControlItem:
    """One NIS2 control as it should appear on the meeting agenda."""

    control_id: str
    control_ref: str
    title: str
    category: str
    what_this_means: str
    status: str
    evidence_count: int
    evidence_titles: list[str]
    open_recommendation_rationale: str | None
    ask_in_the_meeting: str
    approve_only_when: str
    inspection_items: tuple[InspectionItem, ...]
    skip_unless_asked: bool


@dataclasses.dataclass(frozen=True)
class MeetingPack:
    """The full agenda plus copy-paste notes for one monthly review."""

    generated_at: datetime
    met: int
    partial: int
    gap: int
    total_controls: int
    decisions_needed: list[MeetingControlItem]
    skip_unless_asked: list[MeetingControlItem]
    notes_markdown: str
    preamble: str
    review_minutes: int


def build_meeting_pack(conn, tenant_id) -> MeetingPack:
    """Assemble the NIS2 exception-review pack for this tenant.

    Reuses coverage, evidence, and open-recommendation reads (no new tables).
    Non-NIS2 catalogue rows are dropped. Raises TenantContextMissingError if
    tenant_id is missing. notes_markdown is filled after the lists are built.
    """
    nis2_controls = _nis2_coverage_rows(conn, tenant_id)
    rec_by_control = _open_recommendations_by_control(conn, tenant_id)
    items = [
        _item_for_control(conn, tenant_id, control, rec_by_control.get(control.control_id))
        for control in nis2_controls
    ]
    pack = MeetingPack(
        generated_at=datetime.now(timezone.utc),
        met=sum(1 for item in items if item.status == STATUS_MET),
        partial=sum(1 for item in items if item.status == STATUS_PARTIAL),
        gap=sum(1 for item in items if item.status == STATUS_GAP),
        total_controls=len(items),
        decisions_needed=[item for item in items if not item.skip_unless_asked],
        skip_unless_asked=[item for item in items if item.skip_unless_asked],
        notes_markdown="",
        preamble=MEETING_PACK_PREAMBLE,
        review_minutes=MEETING_REVIEW_MINUTES,
    )
    return dataclasses.replace(pack, notes_markdown=render_meeting_notes(pack))


def render_meeting_notes(pack: MeetingPack) -> str:
    """Return markdown the chair can paste into Google Docs or read aloud."""
    lines = [
        f"# NIS2 exception review ({pack.review_minutes} minutes)",
        "",
        pack.preamble,
        "",
        (
            f"Coverage: {pack.met} met / {pack.partial} partial / {pack.gap} gap "
            f"(of {pack.total_controls} NIS2 controls)."
        ),
        "",
        "## Decisions needed today",
        "",
    ]
    if not pack.decisions_needed:
        lines.append("No gaps or partials. Confirm nothing slipped, then stop.")
        lines.append("")
    for item in pack.decisions_needed:
        lines.extend(_render_decision_item(item))
    lines.extend(["## Skip unless someone asks", ""])
    if not pack.skip_unless_asked:
        lines.append("None.")
    for item in pack.skip_unless_asked:
        lines.append(
            f"- {item.control_ref} — {item.title} (met, {item.evidence_count} evidence)"
        )
    return "\n".join(lines) + "\n"


def _nis2_coverage_rows(conn, tenant_id) -> list[CoverageControl]:
    """Return active coverage rows whose catalogue framework is NIS2."""
    return [
        control
        for control in get_coverage_controls(conn, tenant_id)
        if control.framework == FRAMEWORK_NIS2
    ]


def _open_recommendations_by_control(conn, tenant_id) -> dict[str, OpenRecommendation]:
    """Index this tenant's open recommendations by control_id."""
    rows, _total = list_open_recommendations(
        conn, tenant_id, page=1, page_size=RECOMMENDATIONS_MAX_PAGE_SIZE
    )
    return {row.control_id: row for row in rows}


def _item_for_control(
    conn,
    tenant_id,
    control: CoverageControl,
    recommendation: OpenRecommendation | None,
) -> MeetingControlItem:
    """Build one agenda row, including live evidence titles for this control."""
    evidence = get_evidence_for_control(conn, tenant_id, control.control_id)
    titles = [row.title or row.external_id or "(untitled)" for row in evidence]
    rationale = None
    if recommendation is not None:
        rationale = _excerpt(recommendation.rationale)
    return MeetingControlItem(
        control_id=control.control_id,
        control_ref=control.control_ref,
        title=control.title,
        category=control.category,
        what_this_means=_CONTROL_IN_PLAIN_ENGLISH.get(control.control_ref, control.title),
        status=control.status,
        evidence_count=control.evidence_count,
        evidence_titles=titles,
        open_recommendation_rationale=rationale,
        ask_in_the_meeting=_ask_in_the_meeting(control.status, control.evidence_count),
        approve_only_when=approve_rule_for(control.control_ref),
        inspection_items=inspection_items_for(control.control_ref),
        skip_unless_asked=control.status == STATUS_MET,
    )


def _ask_in_the_meeting(status: str, evidence_count: int) -> str:
    """Return the one question the chair should ask for this status."""
    if status == STATUS_GAP and evidence_count == 0:
        return (
            "Nothing is linked. Who will provide evidence, or set a treatment "
            "date and an owner?"
        )
    if status == STATUS_GAP:
        return (
            "Status is still gap even with linked files. What is missing, "
            "who owns it, by when?"
        )
    if status == STATUS_PARTIAL:
        return "Partial. What would make this met, who owns that, by when?"
    return "Met — skip unless someone wants to challenge it."


def _excerpt(text: str) -> str:
    """Trim rationale prose to the meeting-notes character cap."""
    stripped = text.strip()
    if len(stripped) <= MEETING_PACK_RATIONALE_EXCERPT_CHARS:
        return stripped
    return stripped[:MEETING_PACK_RATIONALE_EXCERPT_CHARS].rstrip() + "…"


def _render_decision_item(item: MeetingControlItem) -> list[str]:
    """Return markdown lines for one gap or partial control."""
    evidence = ", ".join(item.evidence_titles) if item.evidence_titles else "none linked"
    lines = [
        f"### {item.control_ref} — {item.title}",
        f"What this is: {item.what_this_means}",
        f"Status: {item.status} | Evidence: {evidence}",
        f"Ask: {item.ask_in_the_meeting}",
        f"Approve only when: {item.approve_only_when}",
    ]
    for artefact in item.inspection_items:
        flag = "required" if artefact.required_for_met else "else partial"
        lines.append(f"- [{flag}] {artefact.label} — pass if: {artefact.pass_if}")
    if item.open_recommendation_rationale:
        lines.append(f"Kerno said: {item.open_recommendation_rationale}")
    lines.append("")
    return lines
