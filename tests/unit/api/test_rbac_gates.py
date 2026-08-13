"""RBAC gate tests: every protected route, driven with real per-user JWTs.

What:  drives each role-gated endpoint once per RBAC role with a genuinely
       signed token, asserting 403 for roles outside the route's allow-list and
       something-other-than-403 for roles inside it. Plus a structural sweep
       that fails if any mutating route has no role gate at all.
Why:   Ticket A. Six mutating or sensitive routes shipped with only
       get_tenant_id on them, so any authenticated user of any role could
       trigger a recalculation, export the tenant's entire evidence base, open
       Jira tickets, or write the DORA register. The per-route tests prove the
       gates work; the structural sweep is the one that catches the NEXT route
       somebody adds without a gate.
How:   pytest tests/unit/api/test_rbac_gates.py -v

The allow-lists below are written out as literal role strings rather than
imported from the service modules on purpose. Imported constants would make
this file mirror whatever the code says; literals make it an independent
statement of the approved authorisation matrix, so editing a *_CAPABLE_ROLES
tuple fails these tests instead of silently redefining the policy.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Must be set before src.api.app is imported: load_dotenv() runs at import time
# and would otherwise install the real .env secret, breaking these signatures.
_JWT_SECRET = "test-secret-for-unit-tests"
os.environ["KERNO_JWT_SECRET"] = _JWT_SECRET

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_USER_ID = "d0000000-0000-4000-d000-000000000004"
_SOME_ID = "b0000000-0000-4000-b000-000000000009"

ALL_ROLES = (
    "compliance_lead",
    "vciso",
    "security_engineer",
    "platform_engineer",
    "auditor",
    "end_customer_admin",
)

LEAD_AND_VCISO = ("compliance_lead", "vciso")
PLATFORM_ONLY = ("platform_engineer",)


@dataclass(frozen=True)
class GatedRoute:
    """One role-gated endpoint and the roles approved to reach it."""

    method: str
    path: str
    allowed: tuple[str, ...]
    json: dict | None = None
    params: dict = field(default_factory=dict)
    # The registered path, when `path` substitutes a concrete id for a path
    # parameter. Only the walker cross-check needs it; requests use `path`.
    template: str | None = None

    def __str__(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def registered(self) -> str:
        return f"{self.method} {self.template or self.path}"


# The approved authorisation matrix. Ticket A added the first seven; the rest
# were already gated and are included so this table is the whole picture.
GATED_ROUTES = [
    # ── Ticket A ────────────────────────────────────────────────────────────
    GatedRoute("POST", "/api/v1/scheduler/run-recalculation", LEAD_AND_VCISO, json={}),
    GatedRoute(
        "GET",
        "/api/v1/export/evidence-pack",
        ("compliance_lead", "vciso", "security_engineer", "platform_engineer"),
        params={"control_family": "access-control"},
    ),
    GatedRoute(
        "POST", "/api/v1/remediation/trigger", PLATFORM_ONLY, json={"control_id": "ctrl-001"}
    ),
    GatedRoute(
        "POST",
        "/api/v1/remediation/close-callback",
        PLATFORM_ONLY,
        json={"jira_issue_key": "KERNO-1", "control_id": "ctrl-001"},
    ),
    GatedRoute(
        "POST",
        "/api/v1/register/entries",
        LEAD_AND_VCISO,
        json={
            "provider_name": "Acme Cloud",
            "service_name": "Hosting",
            "provider_type": "cloud",
            "criticality_level": "critical",
            "business_function": "Platform",
            "data_types": ["operational"],
            "countries_supported": ["DE"],
        },
    ),
    GatedRoute(
        "PATCH",
        f"/api/v1/register/entries/{_SOME_ID}",
        LEAD_AND_VCISO,
        template="/api/v1/register/entries/{entry_id}",
        json={
            "provider_name": "Acme Cloud",
            "service_name": "Hosting",
            "provider_type": "cloud",
            "criticality_level": "critical",
            "business_function": "Platform",
            "data_types": ["operational"],
            "countries_supported": ["DE"],
        },
    ),
    GatedRoute(
        "POST",
        "/api/v1/submissions/runs",
        LEAD_AND_VCISO,
        json={"submission_window_id": _SOME_ID},
    ),
    # ── Gated before Ticket A ───────────────────────────────────────────────
    GatedRoute(
        "POST",
        "/api/v1/overrides",
        ("compliance_lead", "vciso", "security_engineer", "platform_engineer",
         "end_customer_admin"),
        json={"action_type": "approve", "original_control_id": "ctrl-001"},
    ),
    GatedRoute(
        "POST",
        "/api/v1/recommendations/generate",
        ("compliance_lead", "vciso", "security_engineer"),
        json={"control_id": "ctrl-001"},
    ),
    GatedRoute(
        "POST",
        f"/api/v1/evidence/{_SOME_ID}/links",
        ("compliance_lead", "vciso", "security_engineer"),
        json={"control_id": "ctrl-001", "relevance_score": 0.8},
        template="/api/v1/evidence/{record_id}/links",
    ),
    GatedRoute(
        "DELETE",
        f"/api/v1/evidence/{_SOME_ID}/links/ctrl-001",
        ("compliance_lead", "vciso", "security_engineer"),
        template="/api/v1/evidence/{record_id}/links/{control_id}",
    ),
    GatedRoute(
        "PUT",
        "/api/v1/trust-center/visibility",
        ("compliance_lead", "vciso", "platform_engineer"),
        json={"public": True},
    ),
    GatedRoute("POST", "/api/v1/webhooks", PLATFORM_ONLY, json={"source_system": "jira"}),
    GatedRoute(
        "POST",
        f"/api/v1/webhooks/{_SOME_ID}/rotate",
        PLATFORM_ONLY,
        json={},
        template="/api/v1/webhooks/{registration_id}/rotate",
    ),
]

# Mutating routes that legitimately carry no role gate, with the reason each is
# exempt. Anything mutating and ungated that is NOT in here fails the sweep.
UNGATED_BY_DESIGN = {
    # Runs before authentication — there is no role to check yet.
    "POST /api/v1/auth/login",
    # Machine-to-machine. Authenticated by a per-tenant HMAC signature
    # (KER-205 design decision 2), not by a role on a human's JWT.
    "POST /api/v1/webhooks/ingest",
}


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


def _bearer(role: str) -> dict:
    return {"Authorization": f"Bearer {_token(role)}"}


def _override_get_conn():
    yield MagicMock()


def _app():
    app = create_app()
    app.dependency_overrides[get_conn] = _override_get_conn
    return app


def _call(route: GatedRoute, headers: dict) -> int:
    # raise_server_exceptions=False: past the gate these endpoints run real
    # service code against a MagicMock connection and usually blow up. That is
    # fine and expected here — a 500 still proves the caller got through the
    # gate, which is the only thing this file is testing.
    client = TestClient(_app(), raise_server_exceptions=False)
    response = client.request(
        route.method, route.path, json=route.json, params=route.params, headers=headers
    )
    return response.status_code


# ── The matrix ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("route", GATED_ROUTES, ids=str)
@pytest.mark.parametrize("role", ALL_ROLES)
def test_role_matrix(route: GatedRoute, role: str):
    status = _call(route, _bearer(role))
    if role in route.allowed:
        assert status != 403, f"{role} should be permitted on {route} but got 403"
    else:
        assert status == 403, f"{role} should be refused on {route} but got {status}"


# ── Structural sweep ────────────────────────────────────────────────────────


def _walk_endpoints(routes, prefix=""):
    """Yield (path, methods, route) for every endpoint, descending into included routers.

    FastAPI 0.138 keeps each include_router() call as an _IncludedRouter wrapper
    in app.routes rather than flattening the tree, so a naive iteration finds no
    endpoints at all. Descend through both shapes.
    """
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            nested_prefix = getattr(route.include_context, "prefix", "") or ""
            yield from _walk_endpoints(route.original_router.routes, prefix + nested_prefix)
        elif hasattr(route, "routes") and not hasattr(route, "dependant"):
            yield from _walk_endpoints(route.routes, prefix + (getattr(route, "prefix", "") or ""))
        elif hasattr(route, "dependant"):
            yield prefix + route.path, getattr(route, "methods", set()) or set(), route


def _has_role_gate(route) -> bool:
    stack = list(route.dependant.dependencies)
    while stack:
        dependency = stack.pop()
        if getattr(dependency.call, "__name__", "") == "_require_role_dependency":
            return True
        stack.extend(dependency.dependencies)
    return False


def test_route_walker_finds_the_known_routes():
    # Guards the sweep below against passing vacuously: if a FastAPI upgrade
    # changes the router internals, _walk_endpoints silently yields nothing and
    # "no ungated routes" becomes true for the wrong reason.
    discovered = {
        f"{method} {path}"
        for path, methods, _ in _walk_endpoints(create_app().routes)
        for method in methods
    }
    missing = [route.registered for route in GATED_ROUTES if route.registered not in discovered]
    assert not missing, f"route walker did not find: {missing}"


def test_every_mutating_route_has_a_role_gate():
    ungated = []
    for path, methods, route in _walk_endpoints(create_app().routes):
        for method in methods & {"POST", "PUT", "PATCH", "DELETE"}:
            name = f"{method} {path}"
            if name not in UNGATED_BY_DESIGN and not _has_role_gate(route):
                ungated.append(name)
    assert not ungated, (
        "these mutating routes have no require_role() gate — add one, or add it "
        f"to UNGATED_BY_DESIGN with a reason: {sorted(ungated)}"
    )


# ── Token shape ─────────────────────────────────────────────────────────────


def _fake_override() -> SimpleNamespace:
    return SimpleNamespace(
        override_id=uuid.UUID("e0000000-0000-4000-e000-000000000001"),
        action_type="approve",
        original_control_id="ctrl-001",
        corrected_control_id=None,
        created_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )


def _post_override(headers: dict) -> int:
    with patch("src.api.routers.overrides.capture_override", return_value=_fake_override()):
        client = TestClient(_app())
        return client.post(
            "/api/v1/overrides",
            json={"action_type": "approve", "original_control_id": "ctrl-001"},
            headers=headers,
        ).status_code


def test_override_capable_role_gets_through_to_a_real_201():
    assert _post_override(_bearer("vciso")) == 201


def test_unknown_role_is_forbidden():
    assert _post_override(_bearer("intruder")) == 403


def test_missing_role_claim_is_unauthorized():
    headers = {"Authorization": f"Bearer {_token(None, with_role=False)}"}
    assert _post_override(headers) == 401


def test_no_token_is_unauthorized():
    assert _post_override({}) == 401
