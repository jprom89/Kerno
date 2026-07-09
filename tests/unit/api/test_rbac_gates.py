"""RBAC gate tests for the override endpoint using real per-user JWTs (KER-202).

Seven tests drive POST /api/v1/overrides with genuinely signed tokens carrying
different RBAC roles, exercising the full get_tenant_id / get_reviewer_id /
require_role decode path. capture_override is mocked and the DB connection is a
MagicMock, so only the auth + RBAC behaviour is under test:
  - override-capable roles (vciso, compliance_lead, platform_engineer) -> 201
  - auditor (read-only) -> 403
  - an unknown role -> 403
  - a token missing the role claim -> 401
  - no token at all -> 401
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Must be set before src.api.app is imported: load_dotenv() runs at import time
# and would otherwise install the real .env secret, breaking these signatures.
_JWT_SECRET = "test-secret-for-unit-tests"
os.environ["KERNO_JWT_SECRET"] = _JWT_SECRET

import jwt
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_USER_ID = "d0000000-0000-4000-d000-000000000004"
_BODY = {"action_type": "approve", "original_control_id": "ctrl-001"}


def _token(role: str | None, *, with_role: bool = True) -> str:
    payload = {
        "sub": _USER_ID,
        "user_id": _USER_ID,
        "email": "u@x.io",
        "tenant_id": _TENANT_ID,
        "exp": int(time.time()) + 3600,
    }
    if with_role:
        payload["role"] = role
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


def _override_get_conn():
    yield MagicMock()


def _fake_override() -> SimpleNamespace:
    return SimpleNamespace(
        override_id=uuid.UUID("e0000000-0000-4000-e000-000000000001"),
        action_type="approve",
        original_control_id="ctrl-001",
        corrected_control_id=None,
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )


def _app():
    app = create_app()
    app.dependency_overrides[get_conn] = _override_get_conn
    return app


def _post_override(headers: dict) -> int:
    with patch("src.api.routers.overrides.capture_override", return_value=_fake_override()):
        client = TestClient(_app())
        return client.post("/api/v1/overrides", json=_BODY, headers=headers).status_code


def _bearer(role: str) -> dict:
    return {"Authorization": f"Bearer {_token(role)}"}


# ── Allowed roles ───────────────────────────────────────────────────────────────


def test_vciso_may_override():
    assert _post_override(_bearer("vciso")) == 201


def test_compliance_lead_may_override():
    assert _post_override(_bearer("compliance_lead")) == 201


def test_platform_engineer_may_override():
    # platform_engineer maps to INTERNAL_ADMIN (junior weight) — permitted, not None.
    assert _post_override(_bearer("platform_engineer")) == 201


# ── Denied / unauthenticated ────────────────────────────────────────────────────


def test_auditor_is_forbidden():
    assert _post_override(_bearer("auditor")) == 403


def test_unknown_role_is_forbidden():
    assert _post_override(_bearer("intruder")) == 403


def test_missing_role_claim_is_unauthorized():
    headers = {"Authorization": f"Bearer {_token(None, with_role=False)}"}
    assert _post_override(headers) == 401


def test_no_token_is_unauthorized():
    assert _post_override({}) == 401
