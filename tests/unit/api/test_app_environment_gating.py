"""Tests for the environment-gated surfaces (§17 Ticket B, extended by KER-413).

What:  proves the legacy static dashboard is served only in development; that
       the interactive docs are served in development OR under
       KERNO_ENABLE_DOCS=1 and nowhere else; that the bare root URL redirects to
       the configured frontend without ever redirecting to itself or off to an
       unvalidated origin; and that a shipped .env.example CORS placeholder
       stops the app booting outside development.
Why:   before this, /openapi.json served the whole route inventory, every
       schema, and the endpoint docstrings describing our own auth design to
       anonymous callers, and / redirected into a localStorage-JWT dashboard.
       The docs and dashboard switches are now deliberately SEPARATE, and the
       test that pins that apart is the reason a future "simplify" cannot make
       wanting the schema on a host also remount the dashboard.
How:   pytest tests/unit/api/test_app_environment_gating.py -v

Every test pins KERNO_ENV explicitly. It cannot be left ambient: load_dotenv()
runs when src.api.app is imported and the local .env sets KERNO_ENV=development,
so a test that merely declines to set it passes locally for the wrong reason and
fails on a machine without a .env.
"""

from __future__ import annotations

import os
import pathlib

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")

from config.constants import (
    EXAMPLE_ALLOWED_ORIGIN,
    EXAMPLE_ALLOWED_ORIGINS,
    SUPERSEDED_EXAMPLE_ALLOWED_ORIGIN,
)
from src.api.app import _allowed_origins, create_app

DOC_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")
DASHBOARD_PATHS = ("/dashboard/login.html", "/dashboard/")


@pytest.fixture
def env(monkeypatch):
    """Clear every variable these tests depend on so each one states its own environment."""
    for name in ("KERNO_ENV", "FRONTEND_URL", "ALLOWED_ORIGINS", "KERNO_ENABLE_DOCS"):
        monkeypatch.delenv(name, raising=False)
    # The lifespan requires these before it reaches anything under test here, so
    # a startup test would otherwise fail for an unrelated reason.
    monkeypatch.setenv("KERNO_JWT_SECRET", "test-secret-for-unit-tests")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/for-startup-checks-only")
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


# ── The shipped CORS placeholder must fail closed ───────────────────────────
#
# Startup checks use `with TestClient(...)` on purpose: a bare TestClient never
# runs the lifespan, so the plain-request form used elsewhere in this file would
# pass whether or not the check exists.


def test_env_example_ships_an_unregistrable_origin():
    example = pathlib.Path(__file__).resolve().parents[3] / ".env.example"
    text = example.read_text(encoding="utf-8")
    origins_line = next(
        line for line in text.splitlines() if line.startswith("ALLOWED_ORIGINS=")
    )
    assert EXAMPLE_ALLOWED_ORIGIN in origins_line
    # The superseded placeholder is a real, registrable vercel.app subdomain. It
    # may still be named in a comment (the startup check keeps rejecting it), but
    # it must not be a value anyone would copy into a deployment.
    assert SUPERSEDED_EXAMPLE_ALLOWED_ORIGIN not in origins_line


def test_the_placeholder_is_never_the_first_origin():
    # GET / falls back to the first ALLOWED_ORIGINS entry when FRONTEND_URL is
    # unset, so a placeholder in front would redirect a browser to .invalid.
    example = pathlib.Path(__file__).resolve().parents[3] / ".env.example"
    origins_line = next(
        line
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.startswith("ALLOWED_ORIGINS=")
    )
    first = origins_line.split("=", 1)[1].split(",")[0].strip()
    assert first == "http://localhost:3000"
    assert first not in EXAMPLE_ALLOWED_ORIGINS


def test_startup_refuses_the_current_placeholder_outside_development(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("ALLOWED_ORIGINS", f"https://app.example,{EXAMPLE_ALLOWED_ORIGIN}")
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        with TestClient(create_app()):
            pass


def test_startup_refuses_the_superseded_placeholder_outside_development(env):
    # A .env copied before the placeholder changed must still fail closed.
    env.setenv("KERNO_ENV", "production")
    env.setenv(
        "ALLOWED_ORIGINS", f"http://localhost:3000,{SUPERSEDED_EXAMPLE_ALLOWED_ORIGIN}"
    )
    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        with TestClient(create_app()):
            pass


def test_development_still_boots_with_the_example_env(env):
    # `cp .env.example .env` is the documented local path; breaking it would
    # protect nothing and cost the setup instructions.
    env.setenv("KERNO_ENV", "development")
    env.setenv("ALLOWED_ORIGINS", f"http://localhost:3000,{EXAMPLE_ALLOWED_ORIGIN}")
    with TestClient(create_app()) as client:
        assert client.get("/openapi.json").status_code == 200


def test_a_real_origin_starts_and_is_the_cors_allow_list(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("ALLOWED_ORIGINS", "https://app.example")
    app = create_app()
    with TestClient(app) as client:
        response = client.get(
            "/", headers={"Origin": "https://app.example"}, follow_redirects=False
        )
    assert response.headers["access-control-allow-origin"] == "https://app.example"
    assert _allowed_origins() == ["https://app.example"]


def test_unset_origins_is_fail_closed_not_a_startup_error(env):
    # No allow-list means no cross-origin access. That is the safe state, not a
    # misconfiguration to refuse on.
    env.setenv("KERNO_ENV", "production")
    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 200
    assert _allowed_origins() == []


# ── KERNO_ENABLE_DOCS: docs without disarming the other controls ────────────


def test_enable_docs_serves_the_schema_outside_development(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("KERNO_ENABLE_DOCS", "1")
    client = TestClient(create_app())
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_enable_docs_does_not_remount_the_legacy_dashboard(env):
    # THE POINT OF THE WHOLE FLAG. If a future change merges these switches
    # again, asking for the schema on a deployed host also serves a dashboard
    # that keeps its JWT in localStorage. Pinned by name so that cannot happen
    # quietly.
    env.setenv("KERNO_ENV", "production")
    env.setenv("KERNO_ENABLE_DOCS", "1")
    client = TestClient(create_app())
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/dashboard/login.html").status_code == 404
    assert client.get("/dashboard/").status_code == 404


@pytest.mark.parametrize("value", ["true", "yes", "TRUE", "0", "", "on", "enabled"])
def test_only_the_exact_string_one_enables_docs(env, value):
    # Fails closed: a half-remembered value must not publish the schema.
    env.setenv("KERNO_ENV", "production")
    env.setenv("KERNO_ENABLE_DOCS", value)
    assert TestClient(create_app()).get("/openapi.json").status_code == 404


def test_enable_docs_is_read_at_call_time(env):
    # Same requirement as KERNO_ENV: read inside create_app, not at import, so
    # one process can build apps for either configuration.
    env.setenv("KERNO_ENV", "production")
    assert TestClient(create_app()).get("/openapi.json").status_code == 404
    env.setenv("KERNO_ENABLE_DOCS", "1")
    assert TestClient(create_app()).get("/openapi.json").status_code == 200
