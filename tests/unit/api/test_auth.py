"""Unit tests for the POST /api/v1/auth/login endpoint.

What:  Four tests verify authentication behaviour — successful login returns a token,
       invalid credentials return 401, and a missing required field returns 422.
Why:   The login endpoint is the entry point for all dashboard sessions; its error
       paths must return stable, non-leaky responses.
How:   pytest tests/unit/api/test_auth.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")

_PATCH_TARGET = "src.api.routers.auth.authenticate_and_issue_token"
_FAKE_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.fake.token"


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
