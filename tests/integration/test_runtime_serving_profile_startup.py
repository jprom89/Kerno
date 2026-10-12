"""test_runtime_serving_profile_startup.py — the real app lifespan's profile check, under the full grants.

What:  Builds apps with create_app(serving_profile=...) on the runtime pool and
       enters them with TestClient, so the real lifespan reads the pooled
       connection's identity and its assigned grant profile from kerno_test as
       kerno_test_app: a full app starts (and serves a full-only route), a
       pilot app refuses to start in production and in development alike,
       naming the database's 'full' assignment against its own 'pilot', and a
       pilot app on the owner login refuses even in development. No refusal
       carries a URL or a password.
Why:   PILOT-READY-003 (issue #20, gate G10). The app's serving profile and the
       runtime's grant profile are configured separately; only this startup
       comparison ties them together, so it is proved against the real
       database rather than only with fakes
       (tests/unit/api/test_runtime_identity_gate.py). The helpers here are
       shared with tests/integration/test_pilot_runtime_application.py.
How:   Runs in the full-profile run (the default --runtime-profile full) and in
       the restoration check after a pilot run:
       python -m pytest tests/integration/test_runtime_serving_profile_startup.py --require-live-database --require-runtime-identity
       Without runtime settings the runtime tests skip by name.
"""

from __future__ import annotations

import urllib.parse

import psycopg2.pool
import pytest
from fastapi.testclient import TestClient

from src.api import dependencies, runtime_identity
from src.api.app import create_app
from tests import _database_safety as test_database_safety

RUNTIME_ROLE = test_database_safety.TEST_RUNTIME_ROLE_NAME
_STARTUP_SETTINGS_TO_CLEAR = ("KERNO_ENABLE_DOCS", "ALLOWED_ORIGINS", "FRONTEND_URL")
_REFUSED_FULL_ASSIGNMENT = "the database assigns this login the 'full' grant profile but this process serves 'pilot'"


def pin_startup_environment(monkeypatch, environment: str = "production") -> None:
    """Make the lifespan's settings explicit: KERNO_ENV as given, no docs switch, no CORS list, no owner URL.

    The test process pins KERNO_MIGRATION_DATABASE_URL to the owner target,
    which the gate rightly refuses, so it is removed for these tests only.
    """
    monkeypatch.setenv("KERNO_ENV", environment)
    for name in _STARTUP_SETTINGS_TO_CLEAR + (runtime_identity.MIGRATION_DATABASE_URL_VARIABLE,):
        monkeypatch.delenv(name, raising=False)


def record_startup_reads(monkeypatch) -> dict[str, list]:
    """Wrap the gate's two real database reads and return what the lifespan read: session users and profiles."""
    reads = {"identity": [], "profile": []}
    read_identity = runtime_identity.read_connection_identity
    read_profile = runtime_identity.read_assigned_profile

    def recorded_identity():
        """Read the identity through the real helper and record its session_user."""
        identity = read_identity()
        reads["identity"].append(identity.session_user)
        return identity

    def recorded_profile():
        """Read the assignment through the real helper and record the answer."""
        assigned = read_profile()
        reads["profile"].append(assigned)
        return assigned

    monkeypatch.setattr(runtime_identity, "read_connection_identity", recorded_identity)
    monkeypatch.setattr(runtime_identity, "read_assigned_profile", recorded_profile)
    return reads


def start_and_capture_refusal(app) -> str:
    """Enter the app's lifespan, which must refuse; return the refusal's message."""
    with pytest.raises(RuntimeError) as refused:
        with TestClient(app):
            pytest.fail("the app started; its startup check should have refused")
    return str(refused.value)


def assert_no_connection_secret(message: str) -> None:
    """Assert a refusal names no database URL and neither test login's password."""
    state = test_database_safety.current_state()
    for target in (state.target, state.runtime_target):
        if target is None:
            continue
        assert target.url not in message
        password = urllib.parse.urlsplit(target.url).password
        assert not password or password not in message
    assert "postgresql://" not in message and "postgres://" not in message


def test_a_full_app_starts_on_the_runtime_pool_after_reading_its_identity_and_full_assignment(runtime_pool, monkeypatch):
    pin_startup_environment(monkeypatch)
    reads = record_startup_reads(monkeypatch)
    app = create_app(serving_profile="full")
    with TestClient(app) as client:
        reporting_windows = client.get("/api/v1/register/windows")
        submission_windows = client.get("/api/v1/submissions/windows")
    assert app.state.serving_profile == "full"
    assert reads == {"identity": [RUNTIME_ROLE], "profile": ["full"]}
    assert (reporting_windows.status_code, submission_windows.status_code) == (200, 200)


@pytest.mark.parametrize("environment", ["production", "development"])
def test_a_pilot_app_refuses_to_start_under_the_full_grants_in_every_environment(runtime_pool, monkeypatch, environment):
    pin_startup_environment(monkeypatch, environment)
    reads = record_startup_reads(monkeypatch)
    message = start_and_capture_refusal(create_app(serving_profile="pilot"))
    assert _REFUSED_FULL_ASSIGNMENT in message
    assert reads == {"identity": [RUNTIME_ROLE], "profile": ["full"]}
    assert_no_connection_secret(message)


@pytest.mark.integration
def test_a_pilot_app_on_the_owner_login_refuses_even_in_development(db_connection, monkeypatch):
    owner_pool = psycopg2.pool.ThreadedConnectionPool(1, 1, dsn=test_database_safety.current_state().target.url)

    def owner_pool_only():
        """Return the owner pool in place of the application's."""
        return owner_pool

    monkeypatch.setattr(dependencies, "_get_pool", owner_pool_only)
    pin_startup_environment(monkeypatch, "development")
    try:
        message = start_and_capture_refusal(create_app(serving_profile="pilot"))
    finally:
        owner_pool.closeall()
    assert "the login owns the database" in message
    assert "no grant profile is assigned to this login" in message
    assert_no_connection_secret(message)
