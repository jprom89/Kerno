"""Unit tests for the POST /api/v1/overrides endpoint covering approve, edit, and validation failures.
capture_override is mocked at the router level; no database is touched."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id
from src.api.routers.overrides import get_reviewer_id

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_REVIEWER_ID = "a0000000-0000-4000-a000-000000000001"
_OVERRIDE_ID = "e0000000-0000-4000-e000-000000000001"

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _override_get_conn():
    yield MagicMock()


def _fake_override(action_type: str, corrected_control_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        override_id=uuid.UUID(_OVERRIDE_ID),
        action_type=action_type,
        original_control_id="ctrl-001",
        corrected_control_id=corrected_control_id,
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )


def _app_with_overrides():
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_reviewer_id] = lambda: _REVIEWER_ID
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_approve_action_returns_201():
    override = _fake_override("approve", None)
    with patch("src.api.routers.overrides.capture_override", return_value=override):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "reviewer_role": "vciso",
                "action_type": "approve",
                "original_control_id": "ctrl-001",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["override_id"] == _OVERRIDE_ID
    assert body["action_type"] == "approve"
    assert body["corrected_control_id"] is None


def test_edit_action_with_corrected_control_id_returns_201():
    override = _fake_override("edit", "ctrl-002")
    with patch("src.api.routers.overrides.capture_override", return_value=override):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "reviewer_role": "vciso",
                "action_type": "edit",
                "original_control_id": "ctrl-001",
                "corrected_control_id": "ctrl-002",
            },
        )
    assert response.status_code == 201
    body = response.json()
    assert body["action_type"] == "edit"
    assert body["corrected_control_id"] == "ctrl-002"


def test_edit_without_corrected_control_id_returns_422():
    error = ValueError("corrected_control_id is required when action_type is 'edit'.")
    with patch("src.api.routers.overrides.capture_override", side_effect=error):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "reviewer_role": "vciso",
                "action_type": "edit",
                "original_control_id": "ctrl-001",
            },
        )
    assert response.status_code == 422
    assert "corrected_control_id" in response.json()["detail"]


def test_reject_without_corrected_control_id_returns_422():
    error = ValueError("corrected_control_id is required when action_type is 'reject'.")
    with patch("src.api.routers.overrides.capture_override", side_effect=error):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "reviewer_role": "vciso",
                "action_type": "reject",
                "original_control_id": "ctrl-001",
            },
        )
    assert response.status_code == 422
    assert "corrected_control_id" in response.json()["detail"]


def test_invalid_action_type_returns_422():
    error = ValueError("action_type must be one of ['approve', 'edit', 'reject']; received 'approve_all'.")
    with patch("src.api.routers.overrides.capture_override", side_effect=error):
        client = TestClient(_app_with_overrides())
        response = client.post(
            "/api/v1/overrides",
            json={
                "reviewer_role": "vciso",
                "action_type": "approve_all",
                "original_control_id": "ctrl-001",
            },
        )
    assert response.status_code == 422
    assert "action_type" in response.json()["detail"]
