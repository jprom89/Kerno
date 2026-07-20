"""Unit tests for GET /api/v1/recommendations (KER-303).

What:  the list endpoint returns the paginated open-review queue, enforces
       auth, validates pagination bounds, and forwards the session tenant —
       never anything caller-supplied — to the service.
Why:   this is the read side of the human-in-the-loop surface.
How:   pytest tests/unit/api/test_recommendations.py -v
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from config.constants import RECOMMENDATIONS_DEFAULT_PAGE_SIZE
from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id
from src.services.recommendation_service import OpenRecommendation

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_PATCH_TARGET = "src.api.routers.recommendations.list_open_recommendations"


def _item(ref: str = "NIS2-21.2a") -> OpenRecommendation:
    return OpenRecommendation(
        recommendation_id="e0000000-0000-4000-e000-000000000001",
        control_id="c0000000-0000-4000-c000-000000000001",
        control_ref=ref,
        control_title="Risk analysis policy",
        category="governance",
        status="partial",
        confidence_level="medium",
        confidence_score=0.66,
        rationale="Partial coverage found.",
        evidence_count=2,
        generated_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
    )


def _override_get_conn():
    yield MagicMock()


def _app():
    app = create_app()
    app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    app.dependency_overrides[get_conn] = _override_get_conn
    return app


def test_list_returns_paginated_items():
    with patch(_PATCH_TARGET, return_value=([_item()], 1)) as mock_list:
        response = TestClient(_app()).get("/api/v1/recommendations")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == RECOMMENDATIONS_DEFAULT_PAGE_SIZE
    item = body["items"][0]
    assert item["control_ref"] == "NIS2-21.2a"
    assert item["confidence_level"] == "medium"
    assert item["evidence_count"] == 2
    # The service receives the SESSION tenant and the validated pagination.
    args = mock_list.call_args[0]
    assert args[1] == _TENANT_ID
    assert args[2] == 1 and args[3] == RECOMMENDATIONS_DEFAULT_PAGE_SIZE


def test_pagination_params_forwarded_and_bounded():
    with patch(_PATCH_TARGET, return_value=([], 0)) as mock_list:
        client = TestClient(_app())
        assert client.get("/api/v1/recommendations?page=3&page_size=50").status_code == 200
        assert mock_list.call_args[0][2:] == (3, 50)
        assert client.get("/api/v1/recommendations?page=0").status_code == 422
        assert client.get("/api/v1/recommendations?page_size=101").status_code == 422


def test_unauthenticated_request_returns_401():
    app = create_app()
    app.dependency_overrides[get_conn] = _override_get_conn
    assert TestClient(app).get("/api/v1/recommendations").status_code == 401


# ── POST /api/v1/recommendations/generate (KER-401) ───────────────────────────

_GENERATE_PATCH = "src.api.routers.recommendations.generate_recommendation"
_USER_ID = "d0000000-0000-4000-d000-000000000004"


def _generated_output():
    from src.services.recommendation_service import RecommendationOutput

    return RecommendationOutput(
        recommendation_id="f0000000-0000-4000-f000-000000000001",
        tenant_id=_TENANT_ID,
        control_id="c0000000-0000-4000-c000-000000000001",
        status="met",
        confidence_level="high",
        confidence_score=0.9,
        rationale="The evidence demonstrates full coverage.",
        gaps=None,
        evidence_ids=["rec-1"],
        requires_review=False,
        input_snapshot={"rationale_source": "llm", "llm_opinion": {"status": "met", "confidence": 0.8}},
        generated_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
        is_superseded=False,
    )


def _generate_app(role: str = "compliance_lead"):
    from src.api.dependencies import get_role
    from src.api.routers.overrides import get_reviewer_id

    app = create_app()
    app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    app.dependency_overrides[get_reviewer_id] = lambda: _USER_ID
    app.dependency_overrides[get_role] = lambda: role
    app.dependency_overrides[get_conn] = _override_get_conn
    return app


def test_generate_returns_201_with_scorer_verdict_and_rationale_source():
    with patch(_GENERATE_PATCH, return_value=_generated_output()) as mock_generate:
        response = TestClient(_generate_app()).post(
            "/api/v1/recommendations/generate",
            json={"control_id": "c0000000-0000-4000-c000-000000000001"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "met"
    assert body["confidence_level"] == "high"
    assert body["rationale_source"] == "llm"
    assert body["evidence_ids"] == ["rec-1"]
    # The service receives the SESSION identity, never anything body-supplied.
    kwargs = mock_generate.call_args.kwargs
    assert kwargs["triggered_by_user_id"] == _USER_ID
    assert kwargs["triggered_by_role"] == "compliance_lead"


def test_generate_unknown_control_returns_404():
    from src.exceptions import EntryNotFoundError

    with patch(_GENERATE_PATCH, side_effect=EntryNotFoundError("no such control")):
        response = TestClient(_generate_app()).post(
            "/api/v1/recommendations/generate", json={"control_id": "ghost"}
        )
    assert response.status_code == 404


def test_generate_role_gate():
    for role, expected in (
        ("compliance_lead", 201), ("vciso", 201), ("security_engineer", 201),
        ("platform_engineer", 403), ("end_customer_admin", 403), ("auditor", 403),
    ):
        with patch(_GENERATE_PATCH, return_value=_generated_output()):
            response = TestClient(_generate_app(role)).post(
                "/api/v1/recommendations/generate",
                json={"control_id": "c0000000-0000-4000-c000-000000000001"},
            )
        assert response.status_code == expected, f"{role} expected {expected}"


def test_generate_unauthenticated_returns_401():
    app = create_app()
    app.dependency_overrides[get_conn] = _override_get_conn
    response = TestClient(app).post(
        "/api/v1/recommendations/generate", json={"control_id": "x"}
    )
    assert response.status_code == 401
