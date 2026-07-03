"""Unit tests for the /api/v1/coverage endpoints (KER-109).
Coverage service functions are mocked at the router level; no database is touched."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id
from src.services.coverage_service import CategoryCoverage, CoverageControl, CoverageSummary

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _override_get_conn():
    yield MagicMock()


def _fake_summary() -> CoverageSummary:
    return CoverageSummary(
        total_controls=4,
        met=1,
        partial=1,
        gap=2,
        categories=[
            CategoryCoverage(category="governance", met=1, partial=0, gap=1, total=2),
            CategoryCoverage(category="risk_management", met=0, partial=1, gap=1, total=2),
        ],
    )


def _fake_control(status: str = "met", human_confirmed: bool = True) -> CoverageControl:
    return CoverageControl(
        control_id="c0000000-0000-4000-a000-000000000009",
        control_ref="NIS2-1.1",
        title="Governance policy",
        category="governance",
        framework="nis2",
        status=status,
        status_source="override" if human_confirmed else "recommendation",
        human_confirmed=human_confirmed,
        confidence_level="high",
        confidence_score=0.9,
        evidence_count=3,
    )


def _app_with_overrides():
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_summary_returns_counts_and_categories():
    with patch("src.api.routers.coverage.get_coverage_summary", return_value=_fake_summary()):
        client = TestClient(_app_with_overrides())
        response = client.get("/api/v1/coverage/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["total_controls"] == 4
    assert (body["met"], body["partial"], body["gap"]) == (1, 1, 2)
    assert len(body["categories"]) == 2
    assert body["categories"][0]["category"] == "governance"
    assert body["categories"][0]["total"] == 2


def test_controls_expose_system_of_record_resolution():
    controls = [_fake_control("met", human_confirmed=True), _fake_control("gap", human_confirmed=False)]
    with patch("src.api.routers.coverage.get_coverage_controls", return_value=controls):
        client = TestClient(_app_with_overrides())
        response = client.get("/api/v1/coverage/controls")
    assert response.status_code == 200
    body = response.json()
    assert body[0]["status"] == "met"
    assert body[0]["human_confirmed"] is True
    assert body[0]["status_source"] == "override"
    assert body[1]["human_confirmed"] is False
    assert body[1]["status_source"] == "recommendation"
    assert body[0]["evidence_count"] == 3


def test_controls_category_filter_forwarded_to_service():
    with patch("src.api.routers.coverage.get_coverage_controls", return_value=[]) as mock_controls:
        client = TestClient(_app_with_overrides())
        response = client.get("/api/v1/coverage/controls?category=governance")
    assert response.status_code == 200
    assert mock_controls.call_args.kwargs["category"] == "governance"


def test_coverage_requires_authentication():
    _app = create_app()
    _app.dependency_overrides[get_conn] = _override_get_conn
    client = TestClient(_app)
    assert client.get("/api/v1/coverage/summary").status_code == 401
    assert client.get("/api/v1/coverage/controls").status_code == 401


def test_tenant_comes_from_session_not_url():
    with patch("src.api.routers.coverage.get_coverage_summary", return_value=_fake_summary()) as mock_summary, \
         patch("src.api.routers.coverage.get_coverage_controls", return_value=[]) as mock_controls:
        client = TestClient(_app_with_overrides())
        client.get("/api/v1/coverage/summary")
        client.get("/api/v1/coverage/controls?category=governance&tenant_id=b0000000-0000-4000-b000-000000000002")
    assert mock_summary.call_args[0][1] == _TENANT_ID
    # A tenant_id smuggled into the query string must be ignored: the service
    # receives the session tenant.
    assert mock_controls.call_args[0][1] == _TENANT_ID
