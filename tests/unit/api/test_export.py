"""Unit tests for the GET /api/v1/export/evidence-pack endpoint (KER-111).
The export service is mocked at the router level; serialisation runs for real."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id
from src.api.schemas.export import EvidencePack, PackMetadata

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _override_get_conn():
    yield MagicMock()


def _fake_pack() -> EvidencePack:
    return EvidencePack(
        metadata=PackMetadata(
            tenant_id=_TENANT_ID,
            control_family="governance",
            generated_at=datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc),
            export_id="x0000000-0000-4000-a000-000000000001",
            kerno_version="dev",
        ),
        controls=[],
    )


def _app_with_overrides():
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_export_returns_json_attachment_with_filename():
    with patch("src.api.routers.export.build_evidence_pack", return_value=_fake_pack()):
        client = TestClient(_app_with_overrides())
        response = client.get("/api/v1/export/evidence-pack?control_family=governance")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"] == (
        'attachment; filename="kerno-evidence-pack-governance-2026-07-03.json"'
    )
    body = response.json()
    assert body["metadata"]["control_family"] == "governance"
    assert body["controls"] == []


def test_export_sanitises_filename_from_family_value():
    with patch("src.api.routers.export.build_evidence_pack", return_value=_fake_pack()):
        client = TestClient(_app_with_overrides())
        response = client.get(
            '/api/v1/export/evidence-pack?control_family=gov"; rm -rf'
        )
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert '"; rm' not in disposition
    assert "kerno-evidence-pack-gov___rm_-rf-" in disposition


def test_export_missing_family_param_returns_422():
    client = TestClient(_app_with_overrides())
    response = client.get("/api/v1/export/evidence-pack")
    assert response.status_code == 422


def test_export_unknown_family_returns_422():
    with patch(
        "src.api.routers.export.build_evidence_pack",
        side_effect=ValueError("No active controls found for control family 'unknown'."),
    ):
        client = TestClient(_app_with_overrides())
        response = client.get("/api/v1/export/evidence-pack?control_family=unknown")
    assert response.status_code == 422
    assert "No active controls" in response.json()["detail"]


def test_export_requires_authentication():
    _app = create_app()
    _app.dependency_overrides[get_conn] = _override_get_conn
    client = TestClient(_app)
    response = client.get("/api/v1/export/evidence-pack?control_family=governance")
    assert response.status_code == 401
