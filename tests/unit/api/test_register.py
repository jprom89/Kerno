"""Nine unit tests for the /api/v1/register endpoints covering JWT auth, entry CRUD,
error mapping, and the unauthenticated /windows endpoint."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import jwt
from fastapi.testclient import TestClient

# Must be set before src.api.app is imported — load_dotenv() in app.py runs at import
# time and would otherwise install the real .env secret, breaking JWT signature checks.
_JWT_SECRET = "test-secret-for-unit-tests"
os.environ["KERNO_JWT_SECRET"] = _JWT_SECRET

from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id
from src.exceptions import EntryNotFoundError, TenantContextMissingError
from src.services.dora_roi_service import RegisterEntryOutput, ReportingWindowOutput

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_ENTRY_ID = "e0000000-0000-4000-e000-000000000001"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _token(tenant_id: str = _TENANT_ID, exp_offset: int = 3600) -> str:
    return jwt.encode(
        {"sub": "u1", "tenant_id": tenant_id, "exp": int(time.time()) + exp_offset},
        _JWT_SECRET,
        algorithm="HS256",
    )


def _make_entry_output() -> RegisterEntryOutput:
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return RegisterEntryOutput(
        register_entry_id=_ENTRY_ID,
        tenant_id=_TENANT_ID,
        provider_name="AWS",
        service_name="EC2",
        provider_type="cloud",
        criticality_level="critical",
        business_function="compute",
        data_types=["pii"],
        countries_supported=["DE"],
        contract_start_date=None,
        contract_end_date=None,
        exit_strategy_summary=None,
        is_active=True,
        source_record_id=None,
        created_at=now,
        updated_at=now,
    )


def _override_get_conn():
    yield MagicMock()


def _app_both_overrides():
    """App with get_conn and get_tenant_id both overridden — for endpoint logic tests."""
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


def _app_conn_override_only():
    """App with only get_conn overridden — lets get_tenant_id run for real for auth tests."""
    _app = create_app()
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


# ── Auth tests (get_tenant_id runs for real) ───────────────────────────────────


def test_valid_jwt_returns_200_for_list_entries():
    with patch("src.api.routers.register.list_active_register_entries", return_value=[]):
        client = TestClient(_app_conn_override_only())
        response = client.get(
            "/api/v1/register/entries",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 200


def test_valid_jwt_returns_200_for_get_entry():
    with patch("src.api.routers.register.get_register_entry", return_value=_make_entry_output()):
        client = TestClient(_app_conn_override_only())
        response = client.get(
            f"/api/v1/register/entries/{_ENTRY_ID}",
            headers={"Authorization": f"Bearer {_token()}"},
        )
    assert response.status_code == 200


def test_missing_jwt_returns_401():
    client = TestClient(_app_conn_override_only())
    response = client.get("/api/v1/register/entries")
    assert response.status_code == 401


def test_expired_jwt_returns_401():
    client = TestClient(_app_conn_override_only())
    response = client.get(
        "/api/v1/register/entries",
        headers={"Authorization": f"Bearer {_token(exp_offset=-3600)}"},
    )
    assert response.status_code == 401


def test_non_uuid_tenant_id_in_jwt_returns_401():
    client = TestClient(_app_conn_override_only())
    response = client.get(
        "/api/v1/register/entries",
        headers={"Authorization": f"Bearer {_token(tenant_id='not-a-uuid')}"},
    )
    assert response.status_code == 401


# ── Endpoint logic tests (both dependencies overridden) ────────────────────────


def test_tenant_context_missing_returns_403():
    with patch(
        "src.api.routers.register.list_active_register_entries",
        side_effect=TenantContextMissingError("test"),
    ):
        client = TestClient(_app_both_overrides())
        response = client.get("/api/v1/register/entries")
    assert response.status_code == 403
    assert response.json()["detail"] == "tenant context required"


def test_entry_not_found_returns_404():
    with patch("src.api.routers.register.get_register_entry", return_value=None):
        client = TestClient(_app_both_overrides())
        response = client.get(f"/api/v1/register/entries/{_ENTRY_ID}")
    assert response.status_code == 404
    assert response.json()["detail"] == "entry not found"


def test_create_entry_returns_201():
    with patch("src.api.routers.register.create_register_entry", return_value=_make_entry_output()):
        client = TestClient(_app_both_overrides())
        response = client.post(
            "/api/v1/register/entries",
            json={
                "provider_name": "AWS",
                "service_name": "EC2",
                "provider_type": "cloud",
                "criticality_level": "critical",
                "business_function": "compute",
                "data_types": ["pii"],
                "countries_supported": ["DE"],
            },
        )
    assert response.status_code == 201
    assert response.json()["register_entry_id"] == _ENTRY_ID


def test_list_reporting_windows_requires_no_auth():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    window = ReportingWindowOutput(
        reporting_window_id="w0000000-0000-4000-w000-000000000001",
        authority_code="MFSA",
        authority_name="Malta Financial Services Authority",
        member_state="MT",
        reporting_year=2025,
        submission_open_date=None,
        submission_close_date=None,
        notes=None,
        created_at=now,
    )
    with patch("src.api.routers.register.list_reporting_windows", return_value=[window]):
        # No get_tenant_id override and no Authorization header.
        _app = create_app()
        _app.dependency_overrides[get_conn] = _override_get_conn
        client = TestClient(_app)
        response = client.get("/api/v1/register/windows")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
