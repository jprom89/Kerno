"""Unit tests for the POST /api/v1/scheduler/run-recalculation endpoint (KER-114).
The scheduler stub is mocked at the router level; no database is touched."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_tenant_id
from src.scheduler.nightly_bias_recalculation import RecalculationStubResult

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")


def _override_get_conn():
    yield MagicMock()


def _fake_result() -> RecalculationStubResult:
    return RecalculationStubResult(
        tenant_id=_TENANT_ID, override_count=3, duration_ms=12, status="stub"
    )


def _app_with_overrides():
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


def test_run_recalculation_returns_stub_result():
    with patch(
        "src.api.routers.scheduler.run_recalculation_stub", return_value=_fake_result()
    ) as mock_run:
        client = TestClient(_app_with_overrides())
        response = client.post("/api/v1/scheduler/run-recalculation")
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": _TENANT_ID,
        "override_count": 3,
        "duration_ms": 12,
        "status": "stub",
    }
    # The stub receives the session tenant, never caller-supplied identity.
    session_arg = mock_run.call_args[0][1]
    assert session_arg.resolve_tenant_id() == _TENANT_ID


def test_run_recalculation_requires_authentication():
    _app = create_app()
    _app.dependency_overrides[get_conn] = _override_get_conn
    client = TestClient(_app)
    response = client.post("/api/v1/scheduler/run-recalculation")
    assert response.status_code == 401
