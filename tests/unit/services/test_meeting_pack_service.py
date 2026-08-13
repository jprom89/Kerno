"""Unit tests for the monthly meeting-pack assembler.

What:  gap/partial rows become decisions, met rows are skipped, non-NIS2
       catalogue rows are dropped, and the markdown contains the ask the
       chair should read. Spy-level mocks; no database.
How:   pytest tests/unit/services/test_meeting_pack_service.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.services.coverage_service import CoverageControl
from src.services.evidence_service import EvidenceResult
from src.services.meeting_pack_service import (
    MEETING_PACK_PREAMBLE,
    build_meeting_pack,
    render_meeting_notes,
)
from src.services.recommendation_service import OpenRecommendation

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _coverage(
    control_id: str,
    control_ref: str,
    title: str,
    status: str,
    framework: str = "nis2",
    evidence_count: int = 0,
) -> CoverageControl:
    """Return a coverage row with the fields the pack actually reads."""
    return CoverageControl(
        control_id=control_id,
        control_ref=control_ref,
        title=title,
        category="operational_resilience",
        framework=framework,
        status=status,
        status_source="none",
        human_confirmed=False,
        confidence_level=None,
        confidence_score=None,
        evidence_count=evidence_count,
    )


def _evidence(title: str) -> EvidenceResult:
    """Return a linked evidence row whose title should appear in the notes."""
    return EvidenceResult(
        link_id="l1",
        control_id="c-gap",
        record_id="r1",
        linked_by="user",
        linked_at=_NOW,
        relevance_score=None,
        note=None,
        link_status="active",
        source_system="upload",
        external_id="backup.pdf",
        record_type="policy",
        title=title,
        body=None,
        fetched_at=_NOW,
        content_hash=None,
    )


def test_gap_with_no_evidence_is_a_decision() -> None:
    """A red control with nothing linked is the first thing the chair asks."""
    gap = _coverage("c-gap", "NIS2-Art21-2-c", "Backup, Recovery and Crisis Management", "gap")
    with patch(
        "src.services.meeting_pack_service.get_coverage_controls", return_value=[gap]
    ), patch(
        "src.services.meeting_pack_service.list_open_recommendations",
        return_value=([], 0),
    ), patch(
        "src.services.meeting_pack_service.get_evidence_for_control", return_value=[]
    ):
        pack = build_meeting_pack(MagicMock(), _TENANT_ID)

    assert pack.gap == 1
    assert pack.met == 0
    assert len(pack.decisions_needed) == 1
    item = pack.decisions_needed[0]
    assert "Nothing is linked" in item.ask_in_the_meeting
    assert "restore" in item.what_this_means.lower()
    assert "Nothing is linked" in pack.notes_markdown
    assert MEETING_PACK_PREAMBLE in pack.notes_markdown


def test_met_control_is_skipped_unless_asked() -> None:
    """Greens are listed at the bottom, not in the decision block."""
    met = _coverage(
        "c-met", "NIS2-Art21-2-a", "Policies", "met", evidence_count=1
    )
    with patch(
        "src.services.meeting_pack_service.get_coverage_controls", return_value=[met]
    ), patch(
        "src.services.meeting_pack_service.list_open_recommendations",
        return_value=([], 0),
    ), patch(
        "src.services.meeting_pack_service.get_evidence_for_control",
        return_value=[_evidence("IS policy")],
    ):
        pack = build_meeting_pack(MagicMock(), _TENANT_ID)

    assert pack.decisions_needed == []
    assert pack.skip_unless_asked[0].control_ref == "NIS2-Art21-2-a"
    assert "Skip unless someone asks" in pack.notes_markdown


def test_non_nis2_controls_are_excluded() -> None:
    """DORA catalogue rows are not this offer's meeting."""
    dora = _coverage("c-dora", "DORA-Art9", "DORA ICT", "gap", framework="dora")
    nis2 = _coverage("c-nis2", "NIS2-Art21-2-c", "Backup", "partial", evidence_count=2)
    with patch(
        "src.services.meeting_pack_service.get_coverage_controls",
        return_value=[dora, nis2],
    ), patch(
        "src.services.meeting_pack_service.list_open_recommendations",
        return_value=([], 0),
    ), patch(
        "src.services.meeting_pack_service.get_evidence_for_control",
        return_value=[_evidence("restore-log.txt")],
    ):
        pack = build_meeting_pack(MagicMock(), _TENANT_ID)

    assert pack.total_controls == 1
    assert pack.decisions_needed[0].control_ref == "NIS2-Art21-2-c"
    assert "What would make this met" in pack.decisions_needed[0].ask_in_the_meeting
    assert "restore-log.txt" in pack.notes_markdown


def test_open_recommendation_rationale_is_excerpted() -> None:
    """The chair sees Kerno's gist, not an unbounded LLM dump."""
    gap = _coverage("c-gap", "NIS2-Art23-1", "Incident notification", "gap")
    rec = OpenRecommendation(
        recommendation_id="rec-1",
        control_id="c-gap",
        control_ref="NIS2-Art23-1",
        control_title="Incident notification",
        category="incident_handling",
        status="gap",
        confidence_level="low",
        confidence_score=0.2,
        rationale="No playbook is linked. " * 80,
        evidence_count=0,
        generated_at=_NOW,
    )
    with patch(
        "src.services.meeting_pack_service.get_coverage_controls", return_value=[gap]
    ), patch(
        "src.services.meeting_pack_service.list_open_recommendations",
        return_value=([rec], 1),
    ), patch(
        "src.services.meeting_pack_service.get_evidence_for_control", return_value=[]
    ):
        pack = build_meeting_pack(MagicMock(), _TENANT_ID)

    rationale = pack.decisions_needed[0].open_recommendation_rationale
    assert rationale is not None
    assert rationale.endswith("…")
    assert "Kerno said:" in pack.notes_markdown


def test_empty_nis2_set_still_renders() -> None:
    """A tenant with no NIS2 rows gets a pack that says there is nothing to decide."""
    from src.services.meeting_pack_service import MeetingPack
    from config.constants import MEETING_REVIEW_MINUTES

    notes = render_meeting_notes(
        MeetingPack(
            generated_at=_NOW,
            met=0,
            partial=0,
            gap=0,
            total_controls=0,
            decisions_needed=[],
            skip_unless_asked=[],
            notes_markdown="",
            preamble=MEETING_PACK_PREAMBLE,
            review_minutes=MEETING_REVIEW_MINUTES,
        )
    )
    assert "No gaps or partials" in notes
