"""Unit tests for the GET /api/v1/panel/controls/{control_id} side-panel endpoint (KER-108).
Service functions are mocked at the router level; no database is touched."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_CONTROL_ID = "NIS2-4.2"
_GENERATED_AT = datetime(2025, 6, 1, tzinfo=timezone.utc)

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _override_get_conn():
    yield MagicMock()


def _fake_recommendation() -> SimpleNamespace:
    return SimpleNamespace(
        recommendation_id="r0000000-0000-4000-a000-000000000001",
        tenant_id=_TENANT_ID,
        control_id=_CONTROL_ID,
        status="partial",
        confidence_level="medium",
        confidence_score=0.55,
        rationale="Found 2 active evidence record(s) for this control.",
        gaps="Score 0.55 is 0.2 below the high-confidence threshold of 0.75.",
        evidence_ids=["rec-001", "rec-002"],
        requires_review=False,
        input_snapshot={},
        generated_at=_GENERATED_AT,
        is_superseded=False,
    )


def _fake_evidence(link_status: str = "active") -> SimpleNamespace:
    return SimpleNamespace(
        link_id="l0000000-0000-4000-a000-000000000001",
        control_id=_CONTROL_ID,
        record_id="rec-001",
        linked_by="alice",
        linked_at=_GENERATED_AT,
        relevance_score=0.8,
        note=None,
        link_status=link_status,
        source_system="confluence",
        external_id="CONF-42",
        record_type="policy",
        title="IR Policy 2024",
        body="…",
        fetched_at=_GENERATED_AT,
        content_hash="abc",
    )


def _app_with_overrides():
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_panel_returns_recommendation_and_evidence():
    with patch("src.api.routers.panel.get_recommendation", return_value=_fake_recommendation()), \
         patch("src.api.routers.panel.get_evidence_for_control", return_value=[_fake_evidence()]):
        client = TestClient(_app_with_overrides())
        response = client.get(f"/api/v1/panel/controls/{_CONTROL_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["control_id"] == _CONTROL_ID
    assert body["recommendation"]["status"] == "partial"
    assert body["recommendation"]["confidence_level"] == "medium"
    assert body["recommendation"]["confidence_score"] == 0.55
    assert body["recommendation"]["rationale"].startswith("Found 2 active")
    assert len(body["evidence"]) == 1
    assert body["evidence"][0]["title"] == "IR Policy 2024"
    assert body["evidence"][0]["source_system"] == "confluence"


def test_panel_without_recommendation_returns_null():
    with patch("src.api.routers.panel.get_recommendation", return_value=None), \
         patch("src.api.routers.panel.get_evidence_for_control", return_value=[]):
        client = TestClient(_app_with_overrides())
        response = client.get(f"/api/v1/panel/controls/{_CONTROL_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["recommendation"] is None
    assert body["evidence"] == []


def test_panel_flags_broken_evidence_links():
    with patch("src.api.routers.panel.get_recommendation", return_value=_fake_recommendation()), \
         patch("src.api.routers.panel.get_evidence_for_control",
               return_value=[_fake_evidence(), _fake_evidence(link_status="broken")]):
        client = TestClient(_app_with_overrides())
        response = client.get(f"/api/v1/panel/controls/{_CONTROL_ID}")
    assert response.status_code == 200
    statuses = [item["link_status"] for item in response.json()["evidence"]]
    assert statuses == ["active", "broken"]


def test_panel_requires_authentication():
    _app = create_app()
    _app.dependency_overrides[get_conn] = _override_get_conn
    client = TestClient(_app)
    response = client.get(f"/api/v1/panel/controls/{_CONTROL_ID}")
    assert response.status_code == 401


def test_panel_services_receive_tenant_from_session_not_url():
    with patch("src.api.routers.panel.get_recommendation", return_value=None) as mock_rec, \
         patch("src.api.routers.panel.get_evidence_for_control", return_value=[]) as mock_ev:
        client = TestClient(_app_with_overrides())
        client.get(f"/api/v1/panel/controls/{_CONTROL_ID}")
    assert mock_rec.call_args[0][1] == _TENANT_ID
    assert mock_ev.call_args[0][1] == _TENANT_ID
