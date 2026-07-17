"""Unit tests for the /api/v1/auth endpoints (login and me).

What:  Login tests verify successful issuance, uniform 401s, and 422 on missing
       fields; /me tests (KER-301) verify a valid JWT yields email + role and
       every failure mode is a uniform 401.
Why:   The login endpoint is the entry point for all dashboard sessions, and
       /me is the per-page-load session check — both must fail closed without
       leaking which part was wrong.
How:   pytest tests/unit/api/test_auth.py -v
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import jwt
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")

_PATCH_TARGET = "src.api.routers.auth.authenticate_and_issue_token"
_FAKE_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.fake.token"
_ONE_HOUR_SECONDS = 3600


def _signed_token(email="lead@kerno.local", role="compliance_lead", *,
                  expires_in=_ONE_HOUR_SECONDS, **claim_overrides) -> str:
    """Mint a genuinely signed JWT with the KER-202 claim shape for /me tests.

    The signing secret is read at CALL time, not import time: other test
    modules overwrite KERNO_JWT_SECRET during collection, and the endpoint
    decodes with whatever the env holds when the request runs.
    """
    payload = {
        "sub": "d0000000-0000-4000-d000-000000000004",
        "email": email,
        "role": role,
        "tenant_id": "a0000000-0000-4000-a000-000000000001",
        "exp": int(time.time()) + expires_in,
    }
    payload.update(claim_overrides)
    return jwt.encode(payload, os.environ["KERNO_JWT_SECRET"], algorithm="HS256")


def _override_get_conn():
    yield MagicMock()


def _app():
    app = create_app()
    app.dependency_overrides[get_conn] = _override_get_conn
    return app


def test_valid_credentials_return_200_with_token():
    with patch(_PATCH_TARGET, return_value=_FAKE_TOKEN):
        client = TestClient(_app())
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "correct"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] == _FAKE_TOKEN
    assert body["token_type"] == "bearer"


def test_wrong_password_returns_401():
    with patch(_PATCH_TARGET, return_value=None):
        client = TestClient(_app())
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "wrong"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


def test_unknown_email_returns_401():
    with patch(_PATCH_TARGET, return_value=None):
        client = TestClient(_app())
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@nowhere.com", "password": "irrelevant"},
        )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid credentials"


def test_missing_password_returns_422():
    client = TestClient(_app())
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com"},
    )
    assert response.status_code == 422


# ── GET /api/v1/auth/me (KER-301) ────────────────────────────────────────────


def _get_me(token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(_app()).get("/api/v1/auth/me", headers=headers)


def test_me_returns_email_and_role_for_valid_token():
    response = _get_me(_signed_token(email="vciso@kerno.local", role="vciso"))
    assert response.status_code == 200
    assert response.json() == {"email": "vciso@kerno.local", "role": "vciso"}


def test_me_never_returns_tenant_or_user_ids():
    response = _get_me(_signed_token())
    assert set(response.json()) == {"email", "role"}


def test_me_without_token_returns_401():
    assert _get_me(None).status_code == 401


def test_me_with_garbage_token_returns_401():
    assert _get_me("not-a-jwt").status_code == 401


def test_me_with_expired_token_returns_401():
    expired = _signed_token(expires_in=-_ONE_HOUR_SECONDS)
    assert _get_me(expired).status_code == 401


def test_me_with_wrong_signature_returns_401():
    forged = jwt.encode(
        {"email": "x@x.io", "role": "vciso", "exp": int(time.time()) + _ONE_HOUR_SECONDS},
        "some-other-secret",
        algorithm="HS256",
    )
    assert _get_me(forged).status_code == 401


def test_me_with_missing_identity_claims_returns_401():
    no_role = _signed_token(role=None)
    assert _get_me(no_role).status_code == 401
