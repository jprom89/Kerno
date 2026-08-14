"""Unit tests for GET /api/v1/meetings/pack.

The service is mocked at the router; no database is touched.
How: pytest tests/unit/api/test_meetings.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from config.constants import MEETING_REVIEW_MINUTES
from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id
from src.services.control_inspection import InspectionItem
from src.services.meeting_pack_service import MeetingControlItem, MeetingPack

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")


def _override_get_conn():
    yield MagicMock()


def _fake_pack() -> MeetingPack:
    item = MeetingControlItem(
        control_id="c-gap",
        control_ref="NIS2-Art21-2-c",
        title="Backup, Recovery and Crisis Management",
        category="operational_resilience",
        what_this_means="Are there backups, and when was the last restore test?",
        status="gap",
        evidence_count=0,
        evidence_titles=[],
        open_recommendation_rationale=None,
        ask_in_the_meeting="Nothing is linked. Who will provide evidence?",
        approve_only_when="Approve as met only if linked: Backup exists.",
        inspection_items=(
            InspectionItem(
                label="Backup exists",
                pass_if="A console screenshot showing backups.",
                fail_if="No screenshot.",
                required_for_met=True,
            ),
        ),
        skip_unless_asked=False,
    )
    return MeetingPack(
        generated_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
        met=0,
        partial=0,
        gap=1,
        total_controls=1,
        decisions_needed=[item],
        skip_unless_asked=[],
        notes_markdown="# NIS2 exception review\n\nNothing is linked.\n",
        preamble="A NIS2 control is one legal requirement.",
        review_minutes=MEETING_REVIEW_MINUTES,
    )


def _app_with_overrides():
    app = create_app()
    app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    app.dependency_overrides[get_conn] = _override_get_conn
    return app


def test_pack_returns_agenda_and_markdown() -> None:
    """The chair receives counts, the ask, and copy-paste notes."""
    with patch(
        "src.api.routers.meetings.build_meeting_pack", return_value=_fake_pack()
    ):
        client = TestClient(_app_with_overrides())
        response = client.get("/api/v1/meetings/pack")
    assert response.status_code == 200
    body = response.json()
    assert body["gap"] == 1
    assert body["decisions_needed"][0]["control_ref"] == "NIS2-Art21-2-c"
    assert body["decisions_needed"][0]["inspection_items"][0]["label"] == "Backup exists"
    assert body["decisions_needed"][0]["approve_only_when"].startswith("Approve as met")
    assert "Nothing is linked" in body["notes_markdown"]
    assert body["review_minutes"] == MEETING_REVIEW_MINUTES
