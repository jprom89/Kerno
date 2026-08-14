"""Tests for the surfaces that exist only in development (§17 Ticket B).

What:  proves the legacy static dashboard and FastAPI's interactive docs are
       served when KERNO_ENV=development and are absent otherwise, and that the
       bare root URL redirects to the configured frontend without ever
       redirecting to itself or off to an unvalidated origin.
Why:   before this, /openapi.json served the whole route inventory, every
       schema, and the endpoint docstrings describing our own auth design to
       anonymous callers, and / redirected into a localStorage-JWT dashboard.
       None of it had any test coverage at all.
How:   pytest tests/unit/api/test_app_environment_gating.py -v

Every test pins KERNO_ENV explicitly. It cannot be left ambient: load_dotenv()
runs when src.api.app is imported and the local .env sets KERNO_ENV=development,
so a test that merely declines to set it passes locally for the wrong reason and
fails on a machine without a .env.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")

from src.api.app import create_app

DOC_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")
DASHBOARD_PATHS = ("/dashboard/login.html", "/dashboard/")


@pytest.fixture
def env(monkeypatch):
    """Clear every variable these tests depend on so each one states its own environment."""
    for name in ("KERNO_ENV", "FRONTEND_URL", "ALLOWED_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ── Documentation surface ───────────────────────────────────────────────────


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_are_absent_outside_development(env, path):
    env.setenv("KERNO_ENV", "production")
    assert TestClient(create_app()).get(path).status_code == 404


@pytest.mark.parametrize("path", DOC_PATHS)
def test_docs_are_served_in_development(env, path):
    env.setenv("KERNO_ENV", "development")
    assert TestClient(create_app()).get(path).status_code == 200


def test_schema_is_gone_not_merely_the_viewers(env):
    # The regression this file exists for: docs_url=None and redoc_url=None
    # remove the two HTML viewers and leave /openapi.json serving everything.
    # Anyone can point their own Swagger UI at a raw schema.
    env.setenv("KERNO_ENV", "production")
    assert TestClient(create_app()).get("/openapi.json").status_code == 404


def test_unset_environment_is_treated_as_not_development(env):
    # Fails closed: a missing or misspelled KERNO_ENV must lock down, not open up.
    client = TestClient(create_app())
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/dashboard/login.html").status_code == 404


# ── Legacy dashboard ────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", DASHBOARD_PATHS)
def test_legacy_dashboard_is_absent_outside_development(env, path):
    env.setenv("KERNO_ENV", "production")
    assert TestClient(create_app()).get(path).status_code == 404


def test_legacy_dashboard_is_served_in_development(env):
    env.setenv("KERNO_ENV", "development")
    response = TestClient(create_app()).get("/dashboard/login.html")
    assert response.status_code == 200


# ── Root redirect ───────────────────────────────────────────────────────────


def test_root_redirects_to_the_configured_frontend(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("FRONTEND_URL", "https://app.kerno.io")
    response = TestClient(create_app()).get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://app.kerno.io"


def test_root_never_redirects_to_the_legacy_dashboard(env):
    # The behaviour being replaced. Worth asserting by name so a revert is loud.
    env.setenv("KERNO_ENV", "development")
    env.setenv("FRONTEND_URL", "https://app.kerno.io")
    response = TestClient(create_app()).get("/", follow_redirects=False)
    assert "/dashboard/login.html" not in response.headers.get("location", "")


def test_root_falls_back_to_the_first_allowed_origin(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("ALLOWED_ORIGINS", "https://first.example,https://second.example")
    response = TestClient(create_app()).get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://first.example"


def test_frontend_url_wins_over_allowed_origins(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("FRONTEND_URL", "https://app.kerno.io")
    env.setenv("ALLOWED_ORIGINS", "https://ignored.example")
    response = TestClient(create_app()).get("/", follow_redirects=False)
    assert response.headers["location"] == "https://app.kerno.io"


def test_root_trailing_slash_is_normalised(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("FRONTEND_URL", "https://app.kerno.io/")
    response = TestClient(create_app()).get("/", follow_redirects=False)
    assert response.headers["location"] == "https://app.kerno.io"


def test_root_identifies_itself_when_no_frontend_is_configured(env):
    # Not a redirect. RedirectResponse(url="") emits an empty Location header,
    # which resolves to the request URI — an infinite loop on the front door.
    env.setenv("KERNO_ENV", "production")
    response = TestClient(create_app()).get("/", follow_redirects=False)
    assert response.status_code == 200
    assert response.json() == {"service": "kerno-api", "status": "ok"}


def test_root_descriptor_leaks_nothing(env):
    # The descriptor must not hand back what disabling /openapi.json removed.
    env.setenv("KERNO_ENV", "production")
    body = TestClient(create_app()).get("/", follow_redirects=False).json()
    assert set(body) == {"service", "status"}
    assert "production" not in str(body).lower()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "//evil.example",  # protocol-relative: looks relative, redirects off-origin
        "app.kerno.io",  # scheme-less: resolves under this host and 404s
        "javascript:alert(1)",
    ],
)
def test_root_refuses_a_target_that_is_not_an_absolute_http_url(env, value):
    env.setenv("KERNO_ENV", "production")
    env.setenv("FRONTEND_URL", value)
    response = TestClient(create_app()).get("/", follow_redirects=False)
    assert response.status_code == 200, f"{value!r} should not produce a redirect"
    assert "location" not in response.headers


def test_blank_frontend_url_falls_through_to_allowed_origins(env):
    # An operator writing a bare "FRONTEND_URL=" line yields "" from dotenv.
    env.setenv("KERNO_ENV", "production")
    env.setenv("FRONTEND_URL", "")
    env.setenv("ALLOWED_ORIGINS", "https://fallback.example")
    response = TestClient(create_app()).get("/", follow_redirects=False)
    assert response.headers["location"] == "https://fallback.example"
