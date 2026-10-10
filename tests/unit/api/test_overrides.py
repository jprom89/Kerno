"""Unit tests for the POST /api/v1/overrides endpoint covering approve, edit, and validation failures.

capture_override is mocked at the router level; no database is touched. Auth is supplied per-user
(KER-202): the reviewer's tenant, id, and role come from dependencies overridden here (get_role
stands in for the verified JWT role claim); RBAC gating itself is exercised in test_rbac_gates.py.
Every decision names the recommendation the reviewer saw (SEC-REMED-005); the tests below pin how
the router validates that id and maps the service's refusals to 404, 409 and 422.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
from src.api.routers.overrides import get_reviewer_id
from src.exceptions import EntryNotFoundError, StaleRecommendationError

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_REVIEWER_ID = "d0000000-0000-4000-d000-000000000004"
_OVERRIDE_ID = "e0000000-0000-4000-e000-000000000001"
_RECOMMENDATION_ID = "f0000000-0000-4000-f000-000000000005"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _override_get_conn():
    yield MagicMock()


def _fake_override(action_type: str, corrected_control_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        override_id=uuid.UUID(_OVERRIDE_ID),
        action_type=action_type,
        original_control_id="ctrl-001",
        recommendation_id=uuid.UUID(_RECOMMENDATION_ID),
        corrected_control_id=corrected_control_id,
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )


def _app_with_overrides(role: str = "vciso"):
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_reviewer_id] = lambda: _REVIEWER_ID
    _app.dependency_overrides[get_role] = lambda: role
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


def _invalid_fields(response) -> list:
    """Return the last loc element of each Pydantic validation error in a 422 body."""
    return [error["loc"][-1] for error in response.json()["detail"]]


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_approve_action_returns_201():
    override = _fake_override("approve", None)
    with patch("src.api.routers.overrides.capture_override", return_value=override):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "approve",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["override_id"] == _OVERRIDE_ID
    assert body["action_type"] == "approve"
    assert body["recommendation_id"] == _RECOMMENDATION_ID
    assert body["corrected_control_id"] is None


def test_reviewer_role_derived_from_jwt_not_body():
    # The reviewer_role passed to capture_override must come from the JWT role
    # (vciso -> ReviewerRole.VCISO), never from any value in the request body.
    override = _fake_override("approve", None)
    with patch("src.api.routers.overrides.capture_override", return_value=override) as mock_capture:
        client = TestClient(_app_with_overrides(role="vciso"))
        response = client.post(
            "/api/v1/overrides",
            json={"action_type": "approve", "original_control_id": "ctrl-001",
                  "recommendation_id": _RECOMMENDATION_ID,
                  "reviewer_role": "internal_admin"},  # ignored — not in the schema
        )
    assert response.status_code == 201
    override_input = mock_capture.call_args[0][2]
    assert override_input.reviewer_role == "vciso"


def test_edit_action_with_corrected_control_id_returns_201():
    override = _fake_override("edit", "ctrl-002")
    with patch("src.api.routers.overrides.capture_override", return_value=override):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "edit",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
                "corrected_control_id": "ctrl-002",
                "justification_text": "Control 002 is the accurate mapping for this evidence.",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["action_type"] == "edit"
    assert body["corrected_control_id"] == "ctrl-002"


# The four service-validation tests below send a body the schema accepts, so the
# 422 can only come from the router mapping capture_override's ValueError; the
# called-once assertion and the string detail both pin that.


def test_edit_without_corrected_control_id_returns_422():
    error = ValueError("corrected_control_id is required when action_type is 'edit'.")
    with patch("src.api.routers.overrides.capture_override", side_effect=error) as mock_capture:
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "edit",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
            },
        )
    assert response.status_code == 422
    mock_capture.assert_called_once()
    assert response.json()["detail"] == str(error)


def test_reject_without_corrected_control_id_returns_422():
    error = ValueError("corrected_control_id is required when action_type is 'reject'.")
    with patch("src.api.routers.overrides.capture_override", side_effect=error) as mock_capture:
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "reject",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
            },
        )
    assert response.status_code == 422
    mock_capture.assert_called_once()
    assert response.json()["detail"] == str(error)


def test_edit_without_justification_returns_422():
    error = ValueError("justification_text is required when action_type is 'edit'.")
    with patch("src.api.routers.overrides.capture_override", side_effect=error) as mock_capture:
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "edit",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
                "corrected_control_id": "ctrl-002",
            },
        )
    assert response.status_code == 422
    mock_capture.assert_called_once()
    assert response.json()["detail"] == str(error)


def test_invalid_action_type_returns_422():
    error = ValueError("action_type must be one of ['approve', 'edit', 'reject']; received 'approve_all'.")
    with patch("src.api.routers.overrides.capture_override", side_effect=error) as mock_capture:
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "approve_all",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
            },
        )
    assert response.status_code == 422
    mock_capture.assert_called_once()
    assert response.json()["detail"] == str(error)


# ── SEC-REMED-005: the reviewed recommendation ─────────────────────────────────


def test_missing_recommendation_id_returns_422_before_capture():
    with patch("src.api.routers.overrides.capture_override") as mock_capture:
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={"action_type": "approve", "original_control_id": "ctrl-001"},
        )
    assert response.status_code == 422
    assert _invalid_fields(response) == ["recommendation_id"]
    mock_capture.assert_not_called()


def test_malformed_recommendation_id_returns_422_before_capture():
    with patch("src.api.routers.overrides.capture_override") as mock_capture:
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "approve",
                "original_control_id": "ctrl-001",
                "recommendation_id": "not-a-uuid",
            },
        )
    assert response.status_code == 422
    assert _invalid_fields(response) == ["recommendation_id"]
    mock_capture.assert_not_called()


def test_override_input_carries_the_canonical_recommendation_id():
    override = _fake_override("approve", None)
    with patch("src.api.routers.overrides.capture_override", return_value=override) as mock_capture:
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "approve",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID.upper(),
            },
        )
    assert response.status_code == 201
    override_input = mock_capture.call_args[0][2]
    assert override_input.recommendation_id == _RECOMMENDATION_ID


def test_stale_recommendation_returns_409_with_its_message():
    error = StaleRecommendationError(
        f"recommendation {_RECOMMENDATION_ID} has been replaced by a newer recommendation "
        "for this control; review the newer recommendation. This decision was not recorded."
    )
    with patch("src.api.routers.overrides.capture_override", side_effect=error):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "approve",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
            },
        )
    assert response.status_code == 409
    assert response.json()["detail"] == str(error)


def test_recommendation_outside_the_tenant_returns_the_generic_404():
    error = EntryNotFoundError(f"recommendation {_RECOMMENDATION_ID} is not in this tenant")
    with patch("src.api.routers.overrides.capture_override", side_effect=error):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "approve",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
            },
        )
    assert response.status_code == 404
    assert response.json() == {"detail": "entry not found"}


def test_recommendation_for_another_control_returns_422():
    error = ValueError(
        f"recommendation {_RECOMMENDATION_ID} does not belong to control 'ctrl-001'."
    )
    with patch("src.api.routers.overrides.capture_override", side_effect=error):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "action_type": "approve",
                "original_control_id": "ctrl-001",
                "recommendation_id": _RECOMMENDATION_ID,
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"] == str(error)
