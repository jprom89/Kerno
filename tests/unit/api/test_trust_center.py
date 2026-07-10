"""Unit tests for the Trust Center endpoints (KER-204).

Covers the public status page and the authenticated visibility toggle without
a database: a spy connection serves the tenants slug lookup; the KER-109
coverage pass and the KER-107 ledger writer are patched at the trust_center
module level. The security assertions mandated by §13 KER-204 are all here:
  - public tenant -> coverage summary (NIS2-only counts);
  - private tenant -> 404 with a body IDENTICAL to a nonexistent slug;
  - nonexistent slug -> 404;
  - a cache hit does NOT emit a second ledger entry;
  - the visibility toggle is role-gated (auditor -> 403);
  - the tenant_id never appears in any response body.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
from src.api.trust_center import _clear_snapshot_cache
from src.services.coverage_service import CoverageControl

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_PUBLIC_SLUG = "acme-gmbh"
_PRIVATE_SLUG = "stealth-co"


def _control(category: str, status: str, framework: str = "NIS2") -> CoverageControl:
    return CoverageControl(
        control_id=str(uuid.uuid4()),
        control_ref="Art.21",
        title="A control",
        category=category,
        framework=framework,
        status=status,
        status_source="recommendation",
        human_confirmed=False,
        confidence_level="high",
        confidence_score=0.9,
        evidence_count=1,
    )


_COVERAGE = [
    _control("governance", "met"),
    _control("governance", "gap"),
    _control("incident-handling", "partial"),
    _control("dora-only", "met", framework="DORA"),  # must NOT be counted
]


class _RowResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class _SpyConn:
    """Serves the tenants slug lookup and the visibility UPDATE; records SQL."""

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "FROM tenants" in sql and params and params.get("tenant_slug"):
            slug = params["tenant_slug"]
            if slug == _PUBLIC_SLUG:
                return _RowResult((_TENANT_ID, True))
            if slug == _PRIVATE_SLUG:
                return _RowResult((_TENANT_ID, False))
            return _RowResult(None)
        if "UPDATE tenants" in sql:
            return _RowResult((_PUBLIC_SLUG,))
        return _RowResult(None)


def _public_app():
    app = create_app()
    def _conn():
        yield _SpyConn()
    app.dependency_overrides[get_conn] = _conn
    return app


def _admin_app(role: str):
    app = _public_app()
    app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    app.dependency_overrides[get_role] = lambda: role
    return app


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts and ends with an empty snapshot cache."""
    _clear_snapshot_cache()
    yield
    _clear_snapshot_cache()


def _get_status(client, slug):
    with patch("src.api.trust_center.get_coverage_controls", return_value=_COVERAGE), \
         patch("src.api.trust_center.append_audit_entry") as mock_ledger:
        response = client.get(f"/trust-center/{slug}/status")
    return response, mock_ledger


# ── Public status page ────────────────────────────────────────────────────────


def test_public_tenant_returns_nis2_coverage_summary():
    response, _ = _get_status(TestClient(_public_app()), _PUBLIC_SLUG)
    assert response.status_code == 200
    body = response.json()
    assert body["tenant_slug"] == _PUBLIC_SLUG
    # Three NIS2 controls counted; the DORA control excluded.
    assert body["total_controls"] == 3
    assert body["met"] == 1
    assert body["partial"] == 1
    assert body["gap"] == 1
    categories = {c["category"]: c for c in body["categories"]}
    assert set(categories) == {"governance", "incident-handling"}
    assert categories["governance"]["met"] == 1
    assert categories["governance"]["gap"] == 1
    assert body["generated_at"]


def test_private_tenant_returns_404_identical_to_nonexistent_slug():
    client = TestClient(_public_app())
    private_response, private_ledger = _get_status(client, _PRIVATE_SLUG)
    missing_response, _ = _get_status(client, "no-such-slug")
    assert private_response.status_code == 404
    assert missing_response.status_code == 404
    # Byte-identical bodies: existence must not be confirmable (AC-2/AC-7).
    assert private_response.content == missing_response.content
    private_ledger.assert_not_called()


def test_nonexistent_slug_returns_404():
    response, mock_ledger = _get_status(TestClient(_public_app()), "no-such-slug")
    assert response.status_code == 404
    mock_ledger.assert_not_called()


def test_cache_hit_does_not_emit_second_ledger_entry():
    client = TestClient(_public_app())
    with patch("src.api.trust_center.get_coverage_controls", return_value=_COVERAGE), \
         patch("src.api.trust_center.append_audit_entry") as mock_ledger:
        first = client.get(f"/trust-center/{_PUBLIC_SLUG}/status")
        second = client.get(f"/trust-center/{_PUBLIC_SLUG}/status")
    assert first.status_code == second.status_code == 200
    assert mock_ledger.call_count == 1, "only the cache FILL writes the ledger"
    assert first.json() == second.json()
    ledger_kwargs = mock_ledger.call_args
    assert ledger_kwargs[1]["action_type"] == "trust_center_snapshot"
    assert ledger_kwargs[1]["object_type"] == "trust_center"


def test_tenant_id_never_appears_in_any_response_body():
    client = TestClient(_public_app())
    ok_response, _ = _get_status(client, _PUBLIC_SLUG)
    missing_response, _ = _get_status(client, "no-such-slug")
    for response in (ok_response, missing_response):
        assert _TENANT_ID not in response.text


# ── Visibility toggle ─────────────────────────────────────────────────────────


def test_visibility_toggle_allows_permitted_roles():
    for role in ("compliance_lead", "vciso", "platform_engineer"):
        client = TestClient(_admin_app(role))
        response = client.put(
            "/api/v1/trust-center/visibility", json={"public": True}
        )
        assert response.status_code == 200, f"{role} must be permitted"
        body = response.json()
        assert body == {"tenant_slug": _PUBLIC_SLUG, "trust_center_public": True}
        assert _TENANT_ID not in response.text


def test_visibility_toggle_forbids_auditor():
    client = TestClient(_admin_app("auditor"))
    response = client.put("/api/v1/trust-center/visibility", json={"public": True})
    assert response.status_code == 403


def test_visibility_toggle_forbids_unlisted_role():
    client = TestClient(_admin_app("security_engineer"))
    response = client.put("/api/v1/trust-center/visibility", json={"public": True})
    assert response.status_code == 403
