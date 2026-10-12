"""Offline tests for the full and pilot serving profiles (src/api/serving_profile.py, src/api/app.py).

What:  Checks that KERNO_SERVING_PROFILE parses to exactly "full" or "pilot"
       (blank means "full" only in development; anything else refuses
       without echoing the value); that each app keeps the profile it was
       built with; that the pilot registers exactly the supported workflow
       while every excluded operation answers the router's own 404, for
       anonymous and authenticated callers, trailing-slash and other-method
       variants and after a CORS preflight, without leasing a connection,
       entering a business service, calling Jira or the LLM, or writing the
       ledger; that the docs and legacy dashboard flags cannot reopen
       anything in the pilot; that the retained routes answer the same in
       both profiles; and that the real lifespan starts only when the
       database assigns the login the app's own profile.
Why:   PILOT-READY-003 (issue #20, gate G10 of docs/pilot_readiness_kickoff.md
       §4.1–§4.3). Exclusion is enforced by not registering a route, so the
       proof is the HTTP answer and the absence of every side effect, not a
       hidden button. Every database, external and ledger path here is a
       recorder or a refusal; nothing connects to a database.
How:   pytest tests/unit/api/test_serving_profile.py -v
"""

from __future__ import annotations

import dataclasses
import importlib
import logging
import os
import time
from unittest.mock import MagicMock

_JWT_SECRET = "test-secret-for-unit-tests"
os.environ.setdefault("KERNO_JWT_SECRET", _JWT_SECRET)

import jwt
import psycopg2
import pytest
from fastapi.testclient import TestClient

from src.api import dependencies, runtime_identity
from src.api.app import create_app
from src.api.dependencies import get_conn
from src.api.routers import register as register_router
from src.api.routers import submissions as submissions_router
from src.api.serving_profile import (
    FULL_PROFILE,
    PILOT_PROFILE,
    SERVING_PROFILE_VARIABLE,
    ServingProfileError,
    resolve_serving_profile,
)

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_USER_ID = "d0000000-0000-4000-d000-000000000004"
_SOME_ID = "b0000000-0000-4000-b000-000000000009"
_RECOMMENDATION_ID = "f0000000-0000-4000-f000-000000000005"
_ALLOWED_ORIGIN = "https://app.example"
_GATE_LOGGER = "src.api.runtime_identity"
_TOKEN_LIFETIME_SECONDS = 3600

# The router's own answer for an unregistered path; an application 404
# ("entry not found", "not found") would mean business code ran.
_ROUTER_NOT_FOUND = {"detail": "Not Found"}

# A distinctive unrecognised value: a refusal must never repeat it.
_UNRECOGNISED = "staging-zz-profile"

_REGISTER_ENTRY = {
    "provider_name": "Acme Cloud",
    "service_name": "Hosting",
    "provider_type": "cloud",
    "criticality_level": "critical",
    "business_function": "Platform",
    "data_types": ["operational"],
    "countries_supported": ["DE"],
}


@dataclasses.dataclass(frozen=True)
class Operation:
    """One concrete request against a registered operation, with the path template it is registered under."""

    method: str
    path: str
    template: str
    # A role the full profile admits to this operation; None where it has no role gate.
    role: str | None = None
    json: dict | None = None
    headers: dict = dataclasses.field(default_factory=dict)
    # What an anonymous caller gets in either profile, for a retained operation.
    anonymous_status: int = 401
    # The first guarded path a permitted caller reaches in the full profile, for an excluded operation.
    full_reaches: str = "pool lease"

    def __str__(self) -> str:
        """The method and concrete path, used as the test id."""
        return f"{self.method} {self.path}"


def _operation(method: str, path: str, template: str | None = None, **details) -> Operation:
    """An Operation whose template defaults to its concrete path."""
    return Operation(method, path, template or path, **details)


# Kickoff §4.3: every operation the pilot must not register, read from the routers.
EXCLUDED_OPERATIONS = [
    _operation("POST", "/api/v1/remediation/trigger", role="platform_engineer", json={"control_id": "ctrl-001"}),
    _operation(
        "POST", "/api/v1/remediation/close-callback", role="platform_engineer",
        json={"jira_issue_key": "KERNO-1", "control_id": "ctrl-001"},
    ),
    _operation("POST", "/api/v1/scheduler/run-recalculation", role="compliance_lead", json={}),
    _operation("GET", "/api/v1/panel/controls/ctrl-001", "/api/v1/panel/controls/{control_id}", role="compliance_lead"),
    _operation("GET", "/api/v1/ai-decisions", role="compliance_lead"),
    _operation("GET", "/api/v1/register/windows"),
    _operation("GET", "/trust-center/acme/status", "/trust-center/{tenant_slug}/status"),
    _operation("PUT", "/api/v1/trust-center/visibility", role="compliance_lead", json={"public": True}),
    _operation("POST", "/api/v1/webhooks", role="platform_engineer", json={"source_system": "jira"}),
    _operation("GET", f"/api/v1/webhooks/{_SOME_ID}", "/api/v1/webhooks/{registration_id}", role="platform_engineer"),
    _operation(
        "POST", f"/api/v1/webhooks/{_SOME_ID}/rotate", "/api/v1/webhooks/{registration_id}/rotate",
        role="platform_engineer", json={},
    ),
    _operation(
        "POST", "/api/v1/webhooks/ingest",
        json={"source_system": "jira", "event_type": "jira.issue.updated", "external_ref": "K-1", "payload": {}},
        headers={"X-Kerno-Webhook-Id": _SOME_ID, "X-Kerno-Signature": "sha256=" + "0" * 64},
        # Ingest leases nothing until the signature's form is checked.
        full_reaches="webhook service",
    ),
]

# Kickoff §4.1: the supported pilot workflow, which both profiles register.
# Logout is the frontend's own route clearing its cookie; the backend has none.
RETAINED_OPERATIONS = [
    _operation("POST", "/api/v1/auth/login", json={}, anonymous_status=422),
    _operation("GET", "/api/v1/auth/me"),
    _operation("POST", "/api/v1/register/entries", json=_REGISTER_ENTRY),
    _operation("GET", "/api/v1/register/entries"),
    _operation("GET", f"/api/v1/register/entries/{_SOME_ID}", "/api/v1/register/entries/{entry_id}"),
    _operation(
        "PATCH", f"/api/v1/register/entries/{_SOME_ID}", "/api/v1/register/entries/{entry_id}", json=_REGISTER_ENTRY
    ),
    _operation("POST", "/api/v1/submissions/runs", json={"submission_window_id": _SOME_ID}),
    _operation("GET", "/api/v1/submissions/runs"),
    _operation("GET", f"/api/v1/submissions/runs/{_SOME_ID}", "/api/v1/submissions/runs/{run_id}"),
    _operation("GET", f"/api/v1/submissions/runs/{_SOME_ID}/package", "/api/v1/submissions/runs/{run_id}/package"),
    _operation("GET", "/api/v1/submissions/windows", anonymous_status=200),
    _operation(
        "POST", "/api/v1/overrides",
        json={"action_type": "approve", "original_control_id": "ctrl-001", "recommendation_id": _RECOMMENDATION_ID},
    ),
    _operation("GET", "/api/v1/coverage/summary"),
    _operation("GET", "/api/v1/coverage/controls"),
    _operation("GET", "/api/v1/export/evidence-pack?control_family=access-control", "/api/v1/export/evidence-pack"),
    _operation("GET", "/api/v1/recommendations"),
    _operation("POST", "/api/v1/recommendations/generate", json={"control_id": "ctrl-001"}),
    _operation("POST", "/api/v1/evidence"),
    _operation("GET", "/api/v1/evidence"),
    _operation(
        "POST", f"/api/v1/evidence/{_SOME_ID}/links", "/api/v1/evidence/{record_id}/links",
        json={"control_id": "ctrl-001", "relevance_score": 0.8},
    ),
    _operation(
        "DELETE", f"/api/v1/evidence/{_SOME_ID}/links/ctrl-001", "/api/v1/evidence/{record_id}/links/{control_id}"
    ),
]

EXCLUDED_REGISTRATIONS = {(operation.method, operation.template) for operation in EXCLUDED_OPERATIONS}
RETAINED_REGISTRATIONS = {(operation.method, operation.template) for operation in RETAINED_OPERATIONS}

EXCLUDED_PATHS = sorted({operation.path for operation in EXCLUDED_OPERATIONS})
ALL_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

DOC_PATHS = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")
DASHBOARD_PATHS = ("/dashboard/", "/dashboard/login.html")

RESTRICTED = runtime_identity.ConnectionIdentity(
    session_user="kerno_test_app", superuser=False, bypass_rls=False, create_role=False,
    create_database=False, replication=False, memberships=0, owns_database=False, owned_objects=0,
)

# Every assignment other than the app's own, and the phrase its refusal names.
# The lookup answers only for session_user, so an assignment recorded for
# another role reads exactly like no assignment at all: None.
_MISSING = pytest.param(None, "no grant profile is assigned to this login", id="missing_or_another_role")
_UNKNOWN = pytest.param("bogus", "not a recognised profile", id="unknown")
MISMATCHES_FOR_PILOT = [
    _MISSING,
    _UNKNOWN,
    pytest.param("full", "assigns this login the 'full' grant profile but this process serves 'pilot'", id="opposite"),
]
MISMATCHES_FOR_FULL = [
    _MISSING,
    _UNKNOWN,
    pytest.param("pilot", "assigns this login the 'pilot' grant profile but this process serves 'full'", id="opposite"),
]

# Every path an excluded request could take into a database, an external
# system or the ledger: (module, attribute, what reaching it means).
GUARDED_PATHS = [
    ("src.api.dependencies", "_get_pool", "pool lease"),
    ("src.services.jira_client", "JiraClient", "Jira client"),
    ("src.services.remediation_service", "JiraClient", "Jira client"),
    ("src.services.llm_client", "get_llm_client", "LLM client"),
    ("src.services.recommendation_service", "get_llm_client", "LLM client"),
    ("src.services.audit_log", "append_audit_entry", "ledger write"),
    ("src.api.trust_center", "append_audit_entry", "ledger write"),
    ("src.api.webhooks", "append_audit_entry", "ledger write"),
    ("src.services.remediation_service", "append_audit_entry", "ledger write"),
    ("src.scheduler.nightly_bias_recalculation", "append_audit_entry", "ledger write"),
    ("src.api.routers.remediation", "trigger_remediation", "remediation service"),
    ("src.api.routers.remediation", "flag_for_rereview", "remediation service"),
    ("src.api.routers.scheduler", "run_tenant_recalculation", "recalculation service"),
    ("src.api.routers.panel", "get_recommendation", "panel service"),
    ("src.api.routers.panel", "get_evidence_for_control", "panel service"),
    ("src.api.routers.ai_decisions", "query_decision_logs", "decision log service"),
    ("src.api.routers.register", "list_reporting_windows", "reporting window service"),
    ("src.api.trust_center", "_get_or_build_snapshot", "trust center service"),
    ("src.api.webhooks", "register_webhook", "webhook service"),
    ("src.api.webhooks", "rotate_secret", "webhook service"),
    ("src.api.webhooks", "parse_webhook_credentials", "webhook service"),
    ("src.api.webhooks", "_ingest_in_one_transaction", "webhook service"),
]


class _SideEffectReached(RuntimeError):
    """A guarded database, external, ledger or business path was entered."""


class _SideEffects:
    """Records every guarded path a request reached; each one raises so nothing real runs."""

    def __init__(self) -> None:
        """Start with nothing reached."""
        self.reached: list[str] = []

    def guard(self, what: str):
        """A stand-in that records what was reached and stops the request there."""
        def guarded(*args, **kwargs):
            """Record, then refuse."""
            self.reached.append(what)
            raise _SideEffectReached(what)

        return guarded

    def returning(self, what: str, value):
        """A stand-in that records what was reached and returns value."""
        def stand_in(*args, **kwargs):
            """Record, then return the scripted value."""
            self.reached.append(what)
            return value

        return stand_in


def _no_real_pool():
    """Stands in for the application pool: no test here may reach a real one."""
    raise AssertionError("a serving-profile test reached the application pool")


def _mock_connection():
    """Stands in for get_conn: a connection with nothing real behind it."""
    yield MagicMock()


@pytest.fixture
def env(monkeypatch):
    """Clear every variable app construction and startup read, and refuse the real pool."""
    for name in ("KERNO_ENV", "FRONTEND_URL", "ALLOWED_ORIGINS", "KERNO_ENABLE_DOCS", "KERNO_MIGRATION_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KERNO_JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/for-startup-checks-only")
    monkeypatch.setenv(SERVING_PROFILE_VARIABLE, FULL_PROFILE)
    monkeypatch.setattr(dependencies, "_get_pool", _no_real_pool)
    return monkeypatch


@pytest.fixture
def side_effects(env):
    """Replace every guarded path with a recorder that stops the request."""
    recorder = _SideEffects()
    for module_name, attribute, what in GUARDED_PATHS:
        env.setattr(importlib.import_module(module_name), attribute, recorder.guard(what))
    return recorder


@pytest.fixture
def windows(env, side_effects):
    """Both window reads answer an empty list through a recorder, so a 200 shows the route ran."""
    env.setattr(register_router, "list_reporting_windows", side_effects.returning("reporting windows read", []))
    env.setattr(submissions_router, "list_open_windows", side_effects.returning("submission windows read", []))
    return side_effects


def _token(role: str) -> str:
    """A signed per-user JWT for role, carrying the claims the login endpoint issues (auth_service.py)."""
    now = int(time.time())
    payload = {
        "sub": _USER_ID,
        "user_id": _USER_ID,
        "email": "u@x.io",
        "role": role,
        "tenant_id": _TENANT_ID,
        "iat": now,
        "exp": now + _TOKEN_LIFETIME_SECONDS,
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm="HS256")


def _bearer(role: str) -> dict:
    """The Authorization header for role."""
    return {"Authorization": f"Bearer {_token(role)}"}


def _app(serving_profile: str):
    """A freshly built app of serving_profile whose get_conn yields a mock connection."""
    app = create_app(serving_profile)
    app.dependency_overrides[get_conn] = _mock_connection
    return app


def _client(serving_profile: str, real_connections: bool = False) -> TestClient:
    """A client for a fresh app; server errors come back as 500 responses rather than raising."""
    app = create_app(serving_profile) if real_connections else _app(serving_profile)
    return TestClient(app, raise_server_exceptions=False)


def _send(client: TestClient, operation: Operation, headers: dict | None = None, path: str | None = None):
    """Send operation, optionally with extra headers or to another path; never follows a redirect."""
    return client.request(
        operation.method,
        path or operation.path,
        json=operation.json,
        headers={**operation.headers, **(headers or {})},
        follow_redirects=False,
    )


def _assert_router_not_found(response) -> None:
    """The router's own 404: the request matched no registered route."""
    assert response.status_code == 404
    if response.request.method != "HEAD":
        assert response.json() == _ROUTER_NOT_FOUND


def _registrations(serving_profile: str) -> set[tuple[str, str]]:
    """Every (METHOD, path template) the app's own schema generator lists, whether or not the schema is served."""
    paths = create_app(serving_profile).openapi()["paths"]
    return {(method.upper(), path) for path, item in paths.items() for method in item}


# ── Parsing ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_a_blank_profile_is_full_in_development(value):
    assert resolve_serving_profile(value, development=True) == FULL_PROFILE


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_a_blank_profile_is_refused_outside_development(value):
    with pytest.raises(ServingProfileError, match="must be set to 'full' or 'pilot' outside development"):
        resolve_serving_profile(value, development=False)


@pytest.mark.parametrize("development", [False, True])
@pytest.mark.parametrize(
    "value, profile",
    [("full", FULL_PROFILE), (" full ", FULL_PROFILE), ("pilot", PILOT_PROFILE), ("\tpilot\n", PILOT_PROFILE)],
)
def test_the_two_profiles_are_accepted_with_surrounding_whitespace(value, profile, development):
    assert resolve_serving_profile(value, development=development) == profile


@pytest.mark.parametrize("development", [False, True])
@pytest.mark.parametrize("value", ["Pilot", "FULL", "staging", "full,pilot", "1", "pi lot", _UNRECOGNISED])
def test_an_unrecognised_profile_is_refused_without_echoing_it(value, development):
    with pytest.raises(ServingProfileError) as refused:
        resolve_serving_profile(value, development=development)
    assert "the configured value is not recognised" in str(refused.value)
    assert repr(value) not in str(refused.value)
    assert _UNRECOGNISED not in str(refused.value)


def test_a_profile_error_stops_startup_like_any_runtime_error():
    assert issubclass(ServingProfileError, RuntimeError)


# ── create_app resolves the profile once ────────────────────────────────────


@pytest.mark.parametrize("value", ["Pilot", "staging", "full,pilot", _UNRECOGNISED])
def test_create_app_refuses_an_unrecognised_explicit_profile(env, value):
    with pytest.raises(ServingProfileError):
        create_app(value)


@pytest.mark.parametrize("kerno_env", ["production", "development"])
def test_create_app_refuses_an_unrecognised_profile_in_the_environment(env, kerno_env):
    env.setenv("KERNO_ENV", kerno_env)
    env.setenv(SERVING_PROFILE_VARIABLE, _UNRECOGNISED)
    with pytest.raises(ServingProfileError) as refused:
        create_app()
    assert _UNRECOGNISED not in str(refused.value)


def test_create_app_refuses_a_missing_profile_outside_development(env):
    env.setenv("KERNO_ENV", "production")
    env.delenv(SERVING_PROFILE_VARIABLE)
    with pytest.raises(ServingProfileError, match="outside development"):
        create_app()


def test_create_app_refuses_a_blank_explicit_profile_outside_development(env):
    with pytest.raises(ServingProfileError, match="outside development"):
        create_app("")


def test_create_app_defaults_to_full_in_development(env):
    env.setenv("KERNO_ENV", "development")
    env.delenv(SERVING_PROFILE_VARIABLE)
    assert create_app().state.serving_profile == FULL_PROFILE


@pytest.mark.parametrize("environment_profile, explicit_profile", [("full", "pilot"), ("pilot", "full")])
def test_an_explicit_profile_wins_over_the_environment(env, environment_profile, explicit_profile):
    env.setenv(SERVING_PROFILE_VARIABLE, environment_profile)
    assert create_app(explicit_profile).state.serving_profile == explicit_profile


@pytest.mark.parametrize("profile", [FULL_PROFILE, PILOT_PROFILE])
def test_create_app_reads_the_environment_when_no_profile_is_given(env, profile):
    env.setenv(SERVING_PROFILE_VARIABLE, f" {profile} ")
    assert create_app().state.serving_profile == profile


# ── Per-app isolation ───────────────────────────────────────────────────────


def test_two_apps_in_one_process_keep_their_own_profiles(windows):
    full_client = TestClient(_app(FULL_PROFILE))
    pilot_client = TestClient(_app(PILOT_PROFILE))
    assert pilot_client.get("/api/v1/register/windows").status_code == 404
    assert full_client.get("/api/v1/register/windows").status_code == 200
    assert pilot_client.get("/api/v1/register/windows").status_code == 404
    assert (full_client.app.state.serving_profile, pilot_client.app.state.serving_profile) == (
        FULL_PROFILE,
        PILOT_PROFILE,
    )
    assert windows.reached == ["reporting windows read"]


@pytest.mark.parametrize("built_with, changed_to", [("pilot", "full"), ("full", "pilot")])
def test_changing_the_environment_after_construction_does_not_change_the_app(env, windows, built_with, changed_to):
    gate_calls: list[dict] = []

    def record_gate(**arguments):
        """Record the arguments the lifespan passed to the startup gate."""
        gate_calls.append(arguments)

    env.setenv("KERNO_ENV", "production")
    env.setenv(SERVING_PROFILE_VARIABLE, built_with)
    app = create_app()
    app.dependency_overrides[get_conn] = _mock_connection
    env.setenv(SERVING_PROFILE_VARIABLE, changed_to)
    env.setattr(runtime_identity, "enforce_runtime_identity", record_gate)
    with TestClient(app) as client:
        status = client.get("/api/v1/register/windows").status_code
    assert gate_calls == [{"development": False, "serving_profile": built_with}]
    assert app.state.serving_profile == built_with
    assert status == (404 if built_with == PILOT_PROFILE else 200)


# ── The route table ─────────────────────────────────────────────────────────


def test_the_pilot_registers_exactly_the_supported_workflow(env):
    assert _registrations(PILOT_PROFILE) == RETAINED_REGISTRATIONS


def test_the_full_profile_registers_the_workflow_and_every_excluded_operation(env):
    assert _registrations(FULL_PROFILE) == RETAINED_REGISTRATIONS | EXCLUDED_REGISTRATIONS


def test_the_exclusions_are_exactly_what_the_full_profile_adds(env):
    assert _registrations(FULL_PROFILE) - _registrations(PILOT_PROFILE) == EXCLUDED_REGISTRATIONS


def test_the_register_keeps_its_entries_and_loses_only_its_reporting_windows(env):
    pilot_register = {entry for entry in _registrations(PILOT_PROFILE) if entry[1].startswith("/api/v1/register/")}
    full_register = {entry for entry in _registrations(FULL_PROFILE) if entry[1].startswith("/api/v1/register/")}
    assert full_register - pilot_register == {("GET", "/api/v1/register/windows")}
    assert {path for _, path in pilot_register} == {"/api/v1/register/entries", "/api/v1/register/entries/{entry_id}"}


# ── Excluded operations: the router's 404 and no side effect ────────────────


@pytest.mark.parametrize("operation", EXCLUDED_OPERATIONS, ids=str)
def test_the_full_profile_serves_each_excluded_operation(side_effects, operation):
    headers = _bearer(operation.role or "compliance_lead")
    response = _send(_client(FULL_PROFILE, real_connections=True), operation, headers)
    # The recorders do catch these requests: in the full profile each one runs
    # until its first guarded path, which is what makes their silence in the
    # pilot evidence rather than an untested fake.
    assert response.status_code == 500
    assert side_effects.reached == [operation.full_reaches]


@pytest.mark.parametrize("operation", EXCLUDED_OPERATIONS, ids=str)
def test_an_anonymous_excluded_request_is_the_routers_404_with_no_side_effect(side_effects, operation):
    _assert_router_not_found(_send(_client(PILOT_PROFILE, real_connections=True), operation))
    assert side_effects.reached == []


@pytest.mark.parametrize("operation", EXCLUDED_OPERATIONS, ids=str)
def test_an_authenticated_excluded_request_is_the_routers_404_with_no_side_effect(side_effects, operation):
    headers = _bearer(operation.role or "compliance_lead")
    _assert_router_not_found(_send(_client(PILOT_PROFILE, real_connections=True), operation, headers))
    assert side_effects.reached == []


@pytest.mark.parametrize("operation", EXCLUDED_OPERATIONS, ids=str)
def test_the_trailing_slash_alias_of_an_excluded_operation_is_absent(side_effects, operation):
    pilot = _send(_client(PILOT_PROFILE, real_connections=True), operation, path=operation.path + "/")
    _assert_router_not_found(pilot)
    assert side_effects.reached == []
    # The full profile answers the same alias by redirecting to the canonical route.
    full = _send(_client(FULL_PROFILE, real_connections=True), operation, path=operation.path + "/")
    assert full.status_code == 307
    assert full.headers["location"] == f"http://testserver{operation.path}"


@pytest.mark.parametrize("method", ALL_METHODS)
@pytest.mark.parametrize("path", EXCLUDED_PATHS)
def test_every_method_on_an_excluded_path_is_absent(side_effects, path, method):
    pilot = _client(PILOT_PROFILE, real_connections=True)
    response = pilot.request(method, path, headers=_bearer("platform_engineer"), follow_redirects=False)
    _assert_router_not_found(response)
    assert side_effects.reached == []


@pytest.mark.parametrize("method", ["PATCH", "DELETE", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("path", EXCLUDED_PATHS)
def test_the_full_profile_answers_an_unregistered_method_on_those_paths_with_405(side_effects, path, method):
    response = _client(FULL_PROFILE, real_connections=True).request(method, path, follow_redirects=False)
    assert response.status_code == 405


# ── CORS: a preflight answer is not an available route ─────────────────────


@pytest.mark.parametrize("operation", EXCLUDED_OPERATIONS, ids=str)
def test_a_cors_preflight_is_answered_but_the_excluded_route_stays_absent(env, side_effects, operation):
    env.setenv("ALLOWED_ORIGINS", _ALLOWED_ORIGIN)
    pilot = _client(PILOT_PROFILE, real_connections=True)
    preflight = pilot.options(
        operation.path,
        headers={"Origin": _ALLOWED_ORIGIN, "Access-Control-Request-Method": operation.method},
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == _ALLOWED_ORIGIN
    actual = _send(pilot, operation, {"Origin": _ALLOWED_ORIGIN, **_bearer(operation.role or "compliance_lead")})
    _assert_router_not_found(actual)
    assert actual.headers["access-control-allow-origin"] == _ALLOWED_ORIGIN
    assert side_effects.reached == []


@pytest.mark.parametrize("path", EXCLUDED_PATHS)
def test_a_plain_options_request_on_an_excluded_path_is_the_routers_404(env, side_effects, path):
    env.setenv("ALLOWED_ORIGINS", _ALLOWED_ORIGIN)
    _assert_router_not_found(_client(PILOT_PROFILE, real_connections=True).options(path))
    assert side_effects.reached == []


# ── Docs and the legacy dashboard cannot be reopened in the pilot ──────────


@pytest.mark.parametrize("kerno_env", ["development", "production"])
@pytest.mark.parametrize("path", DOC_PATHS + DASHBOARD_PATHS)
def test_the_convenience_flags_do_not_reopen_docs_or_dashboard_in_the_pilot(env, kerno_env, path):
    env.setenv("KERNO_ENV", kerno_env)
    env.setenv("KERNO_ENABLE_DOCS", "1")
    _assert_router_not_found(TestClient(create_app(PILOT_PROFILE)).get(path))


def test_a_pilot_app_is_built_without_any_documentation_url(env):
    env.setenv("KERNO_ENV", "development")
    env.setenv("KERNO_ENABLE_DOCS", "1")
    app = create_app(PILOT_PROFILE)
    assert (app.openapi_url, app.docs_url, app.redoc_url) == (None, None, None)


@pytest.mark.parametrize("path", DOC_PATHS + DASHBOARD_PATHS)
def test_the_same_flags_still_serve_docs_and_dashboard_in_full_development(env, path):
    env.setenv("KERNO_ENV", "development")
    env.setenv("KERNO_ENABLE_DOCS", "1")
    assert TestClient(create_app(FULL_PROFILE)).get(path).status_code == 200


def test_enable_docs_still_serves_the_full_schema_outside_development_without_the_dashboard(env):
    env.setenv("KERNO_ENV", "production")
    env.setenv("KERNO_ENABLE_DOCS", "1")
    client = TestClient(create_app(FULL_PROFILE))
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/v1/register/windows" in schema.json()["paths"]
    assert client.get("/dashboard/login.html").status_code == 404


# ── Retained workflow: registered and answering identically ─────────────────


@pytest.mark.parametrize("operation", RETAINED_OPERATIONS, ids=str)
def test_a_retained_operation_answers_the_same_in_both_profiles(windows, operation):
    pilot = _send(_client(PILOT_PROFILE), operation)
    full = _send(_client(FULL_PROFILE), operation)
    assert pilot.status_code == full.status_code == operation.anonymous_status
    assert pilot.json() == full.json()


def test_the_retained_operations_leave_no_side_effect_when_anonymous(windows):
    pilot = _client(PILOT_PROFILE)
    for operation in RETAINED_OPERATIONS:
        _send(pilot, operation)
    assert windows.reached == ["submission windows read"]


def test_the_root_answers_in_both_profiles(env):
    for profile in (PILOT_PROFILE, FULL_PROFILE):
        assert TestClient(create_app(profile)).get("/", follow_redirects=False).status_code == 200


def test_logout_is_not_a_backend_route_in_either_profile(env):
    for profile in (PILOT_PROFILE, FULL_PROFILE):
        _assert_router_not_found(TestClient(create_app(profile)).post("/api/v1/auth/logout"))


def test_the_pilot_keeps_register_entries_and_submission_windows_but_not_reporting_windows(env, windows):
    env.setattr(register_router, "list_active_register_entries", windows.returning("register entries read", []))
    pilot = _client(PILOT_PROFILE)
    full = _client(FULL_PROFILE)
    assert pilot.get("/api/v1/register/entries", headers=_bearer("compliance_lead")).json() == []
    assert pilot.get("/api/v1/submissions/windows").json() == []
    _assert_router_not_found(pilot.get("/api/v1/register/windows"))
    assert full.get("/api/v1/register/windows").json() == []
    assert windows.reached == ["register entries read", "submission windows read", "reporting windows read"]


# ── The real lifespan: start only on the app's own assigned profile ─────────
#
# `with TestClient(...)` is what runs the lifespan; a bare TestClient never would.


def _assigned(value):
    """A stand-in for read_assigned_profile returning value."""
    def read_assigned_profile():
        """Return the scripted assignment."""
        return value

    return read_assigned_profile


def _failing_profile_read():
    """A stand-in for read_assigned_profile whose query fails."""
    raise psycopg2.ProgrammingError("permission denied for function session_runtime_profile")


def _identity(identity: runtime_identity.ConnectionIdentity):
    """A stand-in for read_connection_identity returning identity."""
    def read_connection_identity():
        """Return the scripted identity."""
        return identity

    return read_connection_identity


@pytest.fixture
def lifespan_env(env):
    """A restricted runtime login whose assigned profile each test scripts."""
    env.setattr(runtime_identity, "read_connection_identity", _identity(RESTRICTED))
    return env


def _start(serving_profile: str) -> int:
    """Run the real app's lifespan and one request; return the request's status."""
    with TestClient(create_app(serving_profile)) as client:
        return client.get("/", follow_redirects=False).status_code


@pytest.mark.parametrize("kerno_env", ["production", "development"])
@pytest.mark.parametrize("profile", [FULL_PROFILE, PILOT_PROFILE])
def test_an_app_starts_on_its_own_assigned_profile(lifespan_env, caplog, kerno_env, profile):
    lifespan_env.setenv("KERNO_ENV", kerno_env)
    lifespan_env.setattr(runtime_identity, "read_assigned_profile", _assigned(profile))
    caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
    assert _start(profile) == 200
    assert [record for record in caplog.records if record.name == _GATE_LOGGER] == []


@pytest.mark.parametrize("kerno_env", ["production", "development"])
@pytest.mark.parametrize("assigned, phrase", MISMATCHES_FOR_PILOT)
def test_a_pilot_app_refuses_any_other_assignment_in_every_environment(lifespan_env, kerno_env, assigned, phrase):
    lifespan_env.setenv("KERNO_ENV", kerno_env)
    lifespan_env.setattr(runtime_identity, "read_assigned_profile", _assigned(assigned))
    with pytest.raises(RuntimeError, match="not a restricted runtime identity for this profile") as refused:
        _start(PILOT_PROFILE)
    assert phrase in str(refused.value)


@pytest.mark.parametrize("assigned, phrase", MISMATCHES_FOR_FULL)
def test_a_full_app_refuses_any_other_assignment_outside_development(lifespan_env, assigned, phrase):
    lifespan_env.setenv("KERNO_ENV", "production")
    lifespan_env.setattr(runtime_identity, "read_assigned_profile", _assigned(assigned))
    with pytest.raises(RuntimeError, match="not a restricted runtime identity for this profile") as refused:
        _start(FULL_PROFILE)
    assert phrase in str(refused.value)


@pytest.mark.parametrize("assigned, phrase", MISMATCHES_FOR_FULL)
def test_a_full_app_in_development_starts_with_a_warning(lifespan_env, caplog, assigned, phrase):
    lifespan_env.setenv("KERNO_ENV", "development")
    lifespan_env.setattr(runtime_identity, "read_assigned_profile", _assigned(assigned))
    caplog.set_level(logging.WARNING, logger=_GATE_LOGGER)
    assert _start(FULL_PROFILE) == 200
    warnings = [record.getMessage() for record in caplog.records if record.name == _GATE_LOGGER]
    assert len(warnings) == 1 and phrase in warnings[0]


@pytest.mark.parametrize("kerno_env", ["production", "development"])
def test_a_failing_profile_read_refuses_a_pilot_app_in_every_environment(lifespan_env, kerno_env):
    lifespan_env.setenv("KERNO_ENV", kerno_env)
    lifespan_env.setattr(runtime_identity, "read_assigned_profile", _failing_profile_read)
    with pytest.raises(RuntimeError, match=r"assigned grant profile could not be read \(ProgrammingError\)") as refused:
        _start(PILOT_PROFILE)
    assert "permission denied" not in str(refused.value)


def test_a_pilot_app_refuses_an_elevated_login_even_in_development(lifespan_env):
    lifespan_env.setenv("KERNO_ENV", "development")
    owner = dataclasses.replace(RESTRICTED, owns_database=True)
    lifespan_env.setattr(runtime_identity, "read_connection_identity", _identity(owner))
    lifespan_env.setattr(runtime_identity, "read_assigned_profile", _assigned(PILOT_PROFILE))
    with pytest.raises(RuntimeError, match="owns the database"):
        _start(PILOT_PROFILE)
