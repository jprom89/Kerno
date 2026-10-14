"""test_pilot_runtime_application.py — the pilot app, on the real runtime login holding the pilot grants.

What:  Builds create_app(serving_profile="pilot") on the runtime pool and enters
       it with TestClient, so the real lifespan checks the runtime identity and
       the database's 'pilot' assignment before any request. It then runs the
       retained workflow (docs/pilot_readiness_kickoff.md §4.1) end to end
       with real logins and JWTs: login and /me with every failure refused
       alike; register entry create, amend, list and read; evidence upload
       (.txt and .pdf), link, unlink and list; coverage; Analyse with bound
       approve, edit and reject and a stale decision refused 409; submission
       runs started, repeated, read, listed and downloaded as the frozen
       package; the submission windows; and the evidence-pack export, each
       ending with both tenants' ledger chains verified. Every excluded
       method (§4.3) answers 404, anonymous or signed in, without leasing a
       runtime connection, calling Jira, ingesting a signed delivery or
       writing the ledger; development and KERNO_ENABLE_DOCS reopen nothing;
       and a full app refuses to start under the pilot grants.
Why:   PILOT-READY-003 (issue #20, gate G10): the retained workflow must work
       under the pilot grants alone, and the excluded surfaces must be absent
       from the serving path, not just hidden. The six retained scenarios are
       the full-profile C2 scenarios of test_c2_runtime_application.py, called
       unchanged with the pilot client, so both profiles assert the same
       behaviour. Owner seeding and read-back stay on db_connection; only the
       LLM and Jira are mocked.
How:   Only in the pilot run, after the guarded switch to the pilot grants:
       python scripts/migrate_test_database.py grant-runtime --profile pilot
       python -m pytest --require-live-database --require-runtime-identity --runtime-profile pilot
       Every test is marked pilot_profile, so the full-profile run deselects
       them; without runtime settings they skip by name.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.api import dependencies
from src.api.app import create_app
from src.api.rate_limit import limiter
from tests.conftest import TENANT_A_ID, TENANT_B_ID
from tests.integration import test_c2_runtime_application as c2
from tests.integration.test_runtime_serving_profile_startup import (
    RUNTIME_ROLE,
    assert_no_connection_secret,
    pin_startup_environment,
    record_startup_reads,
    start_and_capture_refusal,
)

pytestmark = pytest.mark.pilot_profile

_NOT_FOUND = {"detail": "Not Found"}
_REGISTRATION_ID = "e3000000-0000-4000-e000-000000000001"
_REGISTRATION_SECRET = "pilot-runtime-signing-secret"
_DELIVERY_REF = "PILOT-RT-JIRA-1"
_OPEN_WINDOW = "e3000000-0000-4000-e000-000000000021"
_OPEN_WINDOW_AUTHORITY = "PILOTRT"
_TRUST_CENTER_STATUS = f"/trust-center/{c2._SLUGS[TENANT_A_ID]}/status"
_DOCS_AND_DASHBOARD = ("/docs", "/redoc", "/openapi.json", "/dashboard/", "/dashboard/login.html")
_ANY_BODY = {"control_id": c2._CONTROL, "public": False, "source_system": "jira", "jira_issue_key": "PILOT-RT-1"}

# Every concrete method of every excluded surface (kickoff §4.3), with the
# trailing-slash spellings that redirect in the full app, and a plain OPTIONS.
_EXCLUDED_REQUESTS = (
    ("GET", "/api/v1/register/windows"),
    ("GET", "/api/v1/register/windows/"),
    ("HEAD", "/api/v1/register/windows"),
    ("GET", f"/api/v1/panel/controls/{c2._CONTROL}"),
    ("POST", "/api/v1/remediation/trigger"),
    ("POST", "/api/v1/remediation/trigger/"),
    ("POST", "/api/v1/remediation/close-callback"),
    ("POST", "/api/v1/scheduler/run-recalculation"),
    ("POST", "/api/v1/scheduler/run-recalculation/"),
    ("GET", "/api/v1/ai-decisions"),
    ("GET", "/api/v1/ai-decisions/"),
    ("GET", _TRUST_CENTER_STATUS),
    ("GET", f"{_TRUST_CENTER_STATUS}/"),
    ("PUT", "/api/v1/trust-center/visibility"),
    ("POST", "/api/v1/webhooks"),
    ("POST", "/api/v1/webhooks/"),
    ("GET", f"/api/v1/webhooks/{_REGISTRATION_ID}"),
    ("POST", f"/api/v1/webhooks/{_REGISTRATION_ID}/rotate"),
    ("POST", "/api/v1/webhooks/ingest"),
    ("OPTIONS", "/api/v1/webhooks/ingest"),
) + tuple(("GET", path) for path in _DOCS_AND_DASHBOARD)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def startup_reads(runtime_pool, monkeypatch):
    """Record what the real lifespan reads from the database; set up before the pilot app is entered."""
    return record_startup_reads(monkeypatch)


@pytest.fixture
def pilot_api(runtime_pool, db_connection, monkeypatch, startup_reads):
    """Yield a TestClient inside the real lifespan of a fresh pilot app whose every request leases from the runtime pool.

    No dependency is overridden. The environment is pinned to a non-development
    host without the owner URL, so the lifespan's identity and profile check is
    the one a deployed pilot runs. Seeds, as the owner, the C2 module's slugs,
    logins and two catalogue controls, and removes them afterwards.
    """
    assert dependencies._get_pool() is runtime_pool, "requests must lease from the runtime pool"
    pin_startup_environment(monkeypatch)
    monkeypatch.delenv("KERNO_LLM_MODEL", raising=False)
    limiter.reset()
    c2._seed_logins_and_controls(db_connection)
    try:
        with TestClient(create_app(serving_profile="pilot")) as client:
            yield client
    finally:
        limiter.reset()
        c2._remove_seeded_rows(db_connection)


@pytest.fixture
def open_submission_window(db_connection):
    """Seed one submission window open today, so the window list has a row to show; delete it afterwards."""
    with db_connection.transaction():
        db_connection.execute(
            """INSERT INTO dora_submission_windows
               (id, authority_code, reporting_year, register_reference_date, window_open_date, window_close_date)
               VALUES (%s, %s, %s, %s, current_date - 1, current_date + 1)""",
            [_OPEN_WINDOW, _OPEN_WINDOW_AUTHORITY, c2._REPORTING_YEAR, c2._REFERENCE_DATE],
        )
    yield _OPEN_WINDOW
    db_connection.rollback()
    with db_connection.transaction():
        db_connection.execute("DELETE FROM dora_submission_windows WHERE id = %s", [_OPEN_WINDOW])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _count_leases(monkeypatch, pool) -> list:
    """Count every lease from the runtime pool from now on; return the list each lease is appended to."""
    leases = []
    lease = pool.getconn

    def counted_lease(*args, **kwargs):
        """Record the lease, then lease as usual."""
        leases.append(args)
        return lease(*args, **kwargs)

    monkeypatch.setattr(pool, "getconn", counted_lease)
    return leases


def _seed_public_trust_center_and_registration(db_connection) -> None:
    """Make tenant A's Trust Center public and give it one webhook registration, as the owner."""
    with db_connection.transaction():
        db_connection.execute("UPDATE tenants SET trust_center_public = TRUE WHERE tenant_id = %s", [str(TENANT_A_ID)])
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(TENANT_A_ID)])
        db_connection.execute(
            """INSERT INTO webhook_registrations (id, tenant_id, source_system, signing_secret, is_active)
               VALUES (%s, %s, 'jira', %s, TRUE)""",
            [_REGISTRATION_ID, str(TENANT_A_ID), _REGISTRATION_SECRET],
        )


def _signed_delivery_headers(body: bytes) -> dict:
    """Return the headers of a delivery correctly signed with the seeded registration's secret."""
    digest = hmac.new(_REGISTRATION_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "X-Kerno-Webhook-Id": _REGISTRATION_ID,
        "X-Kerno-Signature": f"sha256={digest}",
        "Content-Type": "application/json",
    }


def _request_every_excluded_route(client, header_sets: list[dict]) -> list:
    """Send every excluded request once per header set, plus one correctly signed ingest; return the responses."""
    responses = []
    for headers in header_sets:
        for method, path in _EXCLUDED_REQUESTS:
            body = _ANY_BODY if method in ("POST", "PUT") else None
            responses.append((method, path, client.request(method, path, json=body, headers=headers)))
    delivery = c2._delivery(_DELIVERY_REF)
    signed = client.post("/api/v1/webhooks/ingest", content=delivery, headers=_signed_delivery_headers(delivery))
    responses.append(("POST", "/api/v1/webhooks/ingest (signed)", signed))
    return responses


def _owner_counts(db_connection) -> dict:
    """Read, as the owner, every tenant A row an excluded route could have written."""
    counts = {}
    for table in ("context_records", "webhook_ingest_dedup", "remediation_tasks", "retrieval_bias"):
        counts[table] = c2._owner_rows(
            db_connection, TENANT_A_ID, f"SELECT count(*) FROM {table} WHERE tenant_id = %s", [str(TENANT_A_ID)]
        )[0][0]
    counts["trust_center_public"] = c2._owner_rows(
        db_connection, TENANT_A_ID, "SELECT trust_center_public FROM tenants WHERE tenant_id = %s", [str(TENANT_A_ID)]
    )[0][0]
    return counts


# ── Startup: the real lifespan checked the pilot assignment ──────────────────


def test_the_pilot_app_started_only_after_reading_the_runtime_identity_and_the_pilot_assignment(pilot_api, startup_reads):
    assert pilot_api.app.state.serving_profile == "pilot"
    assert startup_reads == {"identity": [RUNTIME_ROLE], "profile": ["pilot"]}
    assert pilot_api.get("/api/v1/auth/me").status_code == 401


def test_a_full_app_refuses_to_start_under_the_pilot_grants(runtime_pool, monkeypatch):
    pin_startup_environment(monkeypatch)
    reads = record_startup_reads(monkeypatch)
    message = start_and_capture_refusal(create_app(serving_profile="full"))
    assert "the database assigns this login the 'pilot' grant profile but this process serves 'full'" in message
    assert reads == {"identity": [RUNTIME_ROLE], "profile": ["pilot"]}
    assert_no_connection_secret(message)


# ── The retained workflow under the pilot grants ─────────────────────────────


def test_login_and_me_resolve_through_h1_and_every_failure_is_refused_alike(pilot_api, db_connection):
    c2.test_login_resolves_through_h1_and_refuses_every_failure_alike(pilot_api, db_connection)


def test_register_entries_are_created_amended_listed_and_read_with_a_ledger_entry_each(pilot_api, db_connection):
    c2.test_register_entry_create_and_amend_each_append_a_ledger_entry(pilot_api, db_connection)


def test_evidence_upload_link_unlink_and_list(pilot_api, db_connection):
    c2.test_evidence_upload_link_unlink_and_list_as_runtime(pilot_api, db_connection)


def test_analyse_then_bound_approve_edit_and_reject_with_a_stale_decision_refused(pilot_api, db_connection, monkeypatch):
    c2.test_generated_recommendations_take_bound_approve_edit_and_reject_decisions(pilot_api, db_connection, monkeypatch)


def test_coverage_and_the_evidence_pack_export(pilot_api, db_connection):
    c2.test_coverage_and_evidence_pack_export_read_as_runtime(pilot_api, db_connection)


def test_submission_run_start_repeat_read_and_frozen_package_download(pilot_api, db_connection):
    c2.test_submission_run_repeat_read_and_frozen_package_download(pilot_api, db_connection)


def test_submission_runs_are_listed_and_the_submission_windows_stay_served(pilot_api, db_connection, open_submission_window):
    c2._seed_submission_window(db_connection)
    headers = c2._sign_in(pilot_api, c2._LEAD_A)
    started = pilot_api.post(c2._RUNS, json={"submission_window_id": c2._SUBMISSION_WINDOW}, headers=headers)
    assert started.status_code == 200, started.text
    listed = pilot_api.get(c2._RUNS, headers=headers)
    assert listed.status_code == 200, listed.text
    assert [run["id"] for run in listed.json()] == [started.json()["id"]]

    windows = pilot_api.get("/api/v1/submissions/windows")
    assert windows.status_code == 200, windows.text
    assert [window["id"] for window in windows.json() if window["id"] == open_submission_window] == [open_submission_window]
    rows = c2._ledger(db_connection, TENANT_A_ID)
    assert [(row[0], row[2], row[3]) for row in rows] == [
        ("submission_run_recorded", started.json()["id"], c2._LEAD_A.user_id),
    ]
    c2._assert_ledger_chains_valid(db_connection)


# ── The excluded surfaces are absent ─────────────────────────────────────────


def test_every_excluded_method_answers_404_without_a_lease_an_external_call_or_a_write(
    pilot_api, db_connection, runtime_pool, monkeypatch
):
    _seed_public_trust_center_and_registration(db_connection)
    header_sets = [{}, c2._sign_in(pilot_api, c2._LEAD_A), c2._sign_in(pilot_api, c2._PLATFORM_A)]
    before = _owner_counts(db_connection)
    leases = _count_leases(monkeypatch, runtime_pool)
    with patch("src.services.remediation_service.JiraClient") as jira_class:
        responses = _request_every_excluded_route(pilot_api, header_sets)
    assert leases == [], f"{len(leases)} runtime connection(s) leased by excluded routes"
    assert jira_class.call_count == 0
    for method, path, response in responses:
        assert response.status_code == 404, f"{method} {path}: {response.status_code} {response.text}"
        if method != "HEAD":
            assert response.json() == _NOT_FOUND, f"{method} {path}: {response.text}"
    assert _owner_counts(db_connection) == before
    assert before["trust_center_public"] is True
    assert c2._ledger(db_connection, TENANT_A_ID) == [] and c2._ledger(db_connection, TENANT_B_ID) == []
    c2._assert_ledger_chains_valid(db_connection)


def test_development_and_the_docs_switch_reopen_nothing_in_a_pilot_app_on_the_runtime(runtime_pool, db_connection, monkeypatch):
    pin_startup_environment(monkeypatch, "development")
    monkeypatch.setenv("KERNO_ENABLE_DOCS", "1")
    app = create_app(serving_profile="pilot")
    with TestClient(app) as client:
        excluded = {path: client.get(path).status_code for path in _DOCS_AND_DASHBOARD + ("/api/v1/register/windows",)}
        retained = client.get("/api/v1/submissions/windows").status_code
    assert app.state.serving_profile == "pilot"
    assert excluded == dict.fromkeys(excluded, 404)
    assert retained == 200
