"""test_c2_runtime_application.py

What:  drives the real FastAPI application through a connection pool opened as
       the non-owner runtime login kerno_test_app (the runtime_pool fixture),
       with real JWTs issued by the application's own login, across every
       route PILOT-READY-002 mounts in the full profile: login through the H1
       helper, the DORA register, submission runs and the frozen package,
       evidence intake, recommendations and their bound review decisions,
       coverage and the evidence-pack export, webhooks through the H2 helper,
       the Trust Center, remediation, manual recalculation, the AI-decision
       log, the panel and the reference windows. Every test ends by verifying
       both fixture tenants' KER-107 hash chains.
Why:   docs/pilot_readiness_kickoff.md §2.4. The rest of the live suite runs
       as the table owner, so it proves nothing about the non-owner boundary;
       these tests show the full-profile grants of migration 029 are enough
       for every mounted route. Owner work (seeding, read-back and chain
       verification) stays on db_connection and is never runtime evidence.
       Requests reach FastAPI with bearer tokens, as the Next.js server sends
       them: the browser-origin and Fetch Metadata gates (SEC-REMED-002) live
       in the Next.js routes, not in FastAPI, and every upload carries its
       token so SEC-REMED-003's authenticate-before-body order is exercised.
       Only the LLM and Jira are mocked.
How:   runs only when the runtime identity is configured; otherwise every
       test skips by name (§2.3 item 8):
       python -m pytest tests/integration/test_c2_runtime_application.py --require-live-database --require-runtime-identity
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import uuid
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from config.constants import EMBEDDING_DIMENSION, RECOMMENDATIONS_MAX_PAGE_SIZE, SCORING_ENGINE_VERSION
from src.api import dependencies, trust_center
from src.api.app import create_app
from src.api.rate_limit import limiter
from src.services.audit_log import verify_audit_chain
from src.services.auth_service import hash_password
from tests.conftest import TENANT_A_ID, TENANT_B_ID, _format_vector
from tests.unit.services.test_evidence_intake import _minimal_pdf

_PASSWORD = "c2-runtime-fixture-password"
_UNKNOWN_SLUG = "c2-runtime-no-such-org"
_SLUGS = {TENANT_A_ID: "c2-runtime-tenant-a", TENANT_B_ID: "c2-runtime-tenant-b"}
_INVALID_CREDENTIALS = {"detail": "invalid credentials"}
_INVALID_SIGNATURE = {"detail": "invalid webhook signature"}

_CATEGORY = "c2_runtime"
_CONTROL = "c2000000-0000-4000-c000-000000000001"
_CONTROL_REF = "C2RT-01"
_CORRECTED_CONTROL = "c2000000-0000-4000-c000-000000000002"
_CONTROLS = ((_CONTROL, _CONTROL_REF), (_CORRECTED_CONTROL, "C2RT-02"))
_CONTROL_IDS = [_CONTROL, _CORRECTED_CONTROL]
_RECORD = "c2000000-0000-4000-c000-000000000011"
_LINK = "c2000000-0000-4000-c000-000000000012"
_SUBMISSION_WINDOW = "c2000000-0000-4000-c000-000000000021"
_REPORTING_WINDOW = "c2000000-0000-4000-c000-000000000022"
_REPORTING_YEAR = 2035
_REFERENCE_DATE = date(_REPORTING_YEAR, 3, 31)
_WINDOW_OPENS = date(_REPORTING_YEAR, 4, 1)
_WINDOW_CLOSES = date(_REPORTING_YEAR, 4, 30)

_ENTRIES = "/api/v1/register/entries"
_RUNS = "/api/v1/submissions/runs"
_VISIBILITY = "/api/v1/trust-center/visibility"
_RECALCULATE = "/api/v1/scheduler/run-recalculation"

# The deterministic scorer calls one link at 0.9 met/high.
_MET_RELEVANCE = 0.9
_LINK_RELEVANCE = 0.8
_LLM_MODEL = "mistral-large-latest"
_LLM_RATIONALE = "The access review policy directly evidences this control."
_LLM_OWN_CONFIDENCE = 0.8
_JUSTIFICATION = "The reviewer maps this evidence to the other control."
_REVIEW_ACTIONS = ("approve", "edit", "reject")

_POLICY_TEXT = b"Access review policy v3. Quarterly reviews are signed off by the CISO."
_PDF_TEXT = "Board approved ISMS policy"
_DELIVERY_REFS = ("C2RT-JIRA-1", "C2RT-JIRA-2")
_SECRET_HEX_LENGTH = 64
_JIRA_PROJECT = "C2RT"
_JIRA_ISSUE = "C2RT-101"
_JIRA_ASSIGNEE = "jira-account-c2rt"
_SLA_DAYS = 14

_SOURCE_EMBEDDING_CONTROL = "c2_runtime_source"
_TARGET_EMBEDDING_CONTROL = "c2_runtime_target"
_JUST_AFTER = timedelta(microseconds=1)


@dataclasses.dataclass(frozen=True)
class _Account:
    """One seeded login: its tenant, user id, email, RBAC role and whether it may sign in."""

    tenant_id: uuid.UUID
    user_id: str
    email: str
    role: str
    is_active: bool = True


_LEAD_A = _Account(TENANT_A_ID, "d2000000-0000-4000-d000-000000000001", "lead@c2-runtime.example", "compliance_lead")
_PLATFORM_A = _Account(
    TENANT_A_ID, "d2000000-0000-4000-d000-000000000002", "platform@c2-runtime.example", "platform_engineer"
)
_INACTIVE_A = _Account(
    TENANT_A_ID, "d2000000-0000-4000-d000-000000000003", "former@c2-runtime.example", "compliance_lead",
    is_active=False,
)
# The same email as _LEAD_A in the other organisation: the slug is part of the credential.
_LEAD_B = _Account(TENANT_B_ID, "d2000000-0000-4000-d000-000000000004", "lead@c2-runtime.example", "compliance_lead")
_ACCOUNTS = (_LEAD_A, _PLATFORM_A, _INACTIVE_A, _LEAD_B)


@pytest.fixture
def api(runtime_pool, db_connection, monkeypatch):
    """Return a TestClient on a fresh app whose every request leases from the runtime pool.

    No dependency is overridden, so requests carry the JWTs the application's
    own login issues. Seeds, as the owner, both fixture tenants' slugs and
    logins and this module's two catalogue controls; forces the template
    rationale (a test that wants the LLM mocks it); and starts from an empty
    rate-limit store and Trust Center cache. No with-block, because the
    startup identity gate is not under test. Afterwards removes what the
    shared teardown does not.
    """
    assert dependencies._pool is runtime_pool, "requests must lease from the runtime pool"
    assert dependencies._get_pool() is runtime_pool
    monkeypatch.delenv("KERNO_LLM_MODEL", raising=False)
    limiter.reset()
    trust_center._clear_snapshot_cache()
    _seed_logins_and_controls(db_connection)
    yield TestClient(create_app())
    limiter.reset()
    trust_center._clear_snapshot_cache()
    _remove_seeded_rows(db_connection)


def _seed_logins_and_controls(db_connection) -> None:
    """Give both fixture tenants a known slug and their logins, and add this module's two catalogue controls.

    Owner setup on db_connection. Each user row is inserted under its own
    tenant's context, so the strict users policy would admit it even if the
    table were ever forced.
    """
    password_hash = hash_password(_PASSWORD)
    with db_connection.transaction():
        for tenant_id, slug in _SLUGS.items():
            db_connection.execute("UPDATE tenants SET tenant_slug = %s WHERE tenant_id = %s", [slug, str(tenant_id)])
        for account in _ACCOUNTS:
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(account.tenant_id)])
            db_connection.execute(
                """INSERT INTO users (user_id, tenant_id, email, password_hash, role, is_active)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [account.user_id, str(account.tenant_id), account.email, password_hash,
                 account.role, account.is_active],
            )
        for control_id, control_ref in _CONTROLS:
            db_connection.execute(
                """INSERT INTO compliance_controls
                   (control_id, framework, control_ref, category, title,
                    obligation_text, entity_types, is_active)
                   VALUES (%s, 'NIS2', %s, %s, 'C2 runtime test control', 'Test obligation.', %s, TRUE)
                   ON CONFLICT (control_id) DO NOTHING""",
                [control_id, control_ref, _CATEGORY, ["essential"]],
            )


def _remove_seeded_rows(db_connection) -> None:
    """Delete, children first, what this module causes and the shared teardown leaves behind.

    Bound decisions go before their recommendations (composite foreign key),
    links before the controls they name, runs before their window, and users
    before the shared teardown deletes the tenants they reference. The other
    tenant rows are left to that teardown.
    """
    db_connection.rollback()
    for tenant_id in (TENANT_A_ID, TENANT_B_ID):
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
            db_connection.execute("DELETE FROM overrides WHERE original_control_id IN (%s, %s)", _CONTROL_IDS)
            db_connection.execute("DELETE FROM recommendations WHERE control_id IN (%s, %s)", _CONTROL_IDS)
            db_connection.execute("DELETE FROM control_evidence_links WHERE control_id IN (%s, %s)", _CONTROL_IDS)
            db_connection.execute(
                "DELETE FROM dora_submission_runs WHERE submission_window_id = %s", [_SUBMISSION_WINDOW]
            )
            db_connection.execute("DELETE FROM users WHERE tenant_id = %s", [str(tenant_id)])
    with db_connection.transaction():
        db_connection.execute("DELETE FROM compliance_controls WHERE control_id IN (%s, %s)", _CONTROL_IDS)
        db_connection.execute("DELETE FROM dora_submission_windows WHERE id = %s", [_SUBMISSION_WINDOW])
        db_connection.execute(
            "DELETE FROM dora_reporting_windows WHERE reporting_window_id = %s", [_REPORTING_WINDOW]
        )


def _seed_scored_evidence(db_connection, relevance_score: float) -> None:
    """Seed one Tenant A record linked to the control with relevance_score, under Tenant A's context (FORCE RLS)."""
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(TENANT_A_ID)])
        db_connection.execute(
            """INSERT INTO context_records (record_id, tenant_id, source_system, record_type, title, body)
               VALUES (%s, %s, 'confluence', 'policy', 'Access review policy', 'Quarterly access reviews.')""",
            [_RECORD, str(TENANT_A_ID)],
        )
        db_connection.execute(
            """INSERT INTO control_evidence_links
               (link_id, control_id, record_id, linked_by, linked_at, relevance_score)
               VALUES (%s, %s, %s, 'integration-test', now(), %s)""",
            [_LINK, _CONTROL, _RECORD, relevance_score],
        )


def _seed_submission_window(db_connection) -> None:
    """Seed the global submission window the run tests file against."""
    with db_connection.transaction():
        db_connection.execute(
            """INSERT INTO dora_submission_windows
               (id, authority_code, reporting_year, register_reference_date,
                window_open_date, window_close_date)
               VALUES (%s, 'EBA', %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            [_SUBMISSION_WINDOW, _REPORTING_YEAR, _REFERENCE_DATE, _WINDOW_OPENS, _WINDOW_CLOSES],
        )


def _seed_reporting_window(db_connection) -> None:
    """Seed one global reporting window for GET /api/v1/register/windows to list."""
    with db_connection.transaction():
        db_connection.execute(
            """INSERT INTO dora_reporting_windows
               (reporting_window_id, authority_code, authority_name, member_state,
                reporting_year, submission_open_date, submission_close_date)
               VALUES (%s, 'C2RT', 'C2 runtime test authority', 'DE', %s, %s, %s)
               ON CONFLICT (reporting_window_id) DO NOTHING""",
            [_REPORTING_WINDOW, _REPORTING_YEAR, _WINDOW_OPENS, _WINDOW_CLOSES],
        )


def _seed_default_routing_rule(db_connection) -> None:
    """Seed Tenant A's default remediation routing rule (no category), which the trigger falls back to."""
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(TENANT_A_ID)])
        db_connection.execute(
            """INSERT INTO remediation_routing_rules
               (tenant_id, control_category, assignee_jira_account_id, sla_days)
               VALUES (%s, NULL, %s, %s)""",
            [str(TENANT_A_ID), _JIRA_ASSIGNEE, _SLA_DAYS],
        )


def _seed_embeddings(db_connection) -> None:
    """Seed Tenant A embeddings, perpendicular unit vectors, for the two controls the unbound edit moves between."""
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(TENANT_A_ID)])
        for axis, control_id in enumerate((_SOURCE_EMBEDDING_CONTROL, _TARGET_EMBEDDING_CONTROL)):
            vector = [0.0] * EMBEDDING_DIMENSION
            vector[axis] = 1.0
            db_connection.execute(
                "INSERT INTO tenant_embeddings (tenant_id, control_id, embedding) VALUES (%s, %s, %s::vector)",
                [str(TENANT_A_ID), control_id, _format_vector(vector)],
            )


def _seed_unbound_edit(db_connection, created_at) -> None:
    """Seed one Tenant A edit from the source to the target control, bound to no recommendation (as KER-201's tests do).

    created_at None stamps it an hour in the past, so the first
    recalculation's own timestamp is later whichever clock, Python's or
    PostgreSQL's, runs ahead.
    """
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(TENANT_A_ID)])
        db_connection.execute(
            """INSERT INTO overrides
               (tenant_id, reviewer_id, reviewer_role, action_type, original_control_id,
                corrected_control_id, reviewer_confidence_weight, created_at)
               VALUES (%s, %s, 'vciso', 'edit', %s, %s, 1.0,
                       COALESCE(%s::timestamptz, now() - interval '1 hour'))""",
            [str(TENANT_A_ID), _LEAD_A.user_id, _SOURCE_EMBEDDING_CONTROL, _TARGET_EMBEDDING_CONTROL, created_at],
        )


def _owner_rows(db_connection, tenant_id, sql: str, params: list) -> list[tuple]:
    """Run one read on the owner connection under tenant_id's context and return its rows.

    This is how a test checks what the runtime wrote; the read itself is not
    runtime evidence.
    """
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return db_connection.execute(sql, params).fetchall()


def _ledger(db_connection, tenant_id) -> list[tuple]:
    """Return the tenant's ledger in chain order as (action, object type, object id, actor, before, after).

    The shared teardown empties both tenants' ledgers before each test, so
    these are exactly the entries the test caused.
    """
    return _owner_rows(
        db_connection, tenant_id,
        "SELECT action_type, object_type, object_id, actor_id::text, before_state, after_state "
        "FROM audit_log WHERE tenant_id = %s ORDER BY sequence_number",
        [str(tenant_id)],
    )


def _assert_ledger_chains_valid(db_connection) -> None:
    """Assert verify_audit_chain holds for both fixture tenants after the workflow.

    Read on the owner connection (kerno_test) under each tenant's context: the
    verifier checks what the runtime appended, and the read is not runtime
    evidence.
    """
    for tenant_id in (TENANT_A_ID, TENANT_B_ID):
        with db_connection.transaction():
            result = verify_audit_chain(db_connection, tenant_id)
        assert result.is_valid, f"tenant {tenant_id}: {result.failure_reason}"


def _login(client, slug: str, email: str, password: str = _PASSWORD):
    """POST the login form; the account lookup runs through H1 on a runtime connection."""
    return client.post("/api/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": password})


def _token(client, slug: str, email: str) -> str:
    """Sign in through the API and return the JWT it issued."""
    response = _login(client, slug, email)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _bearer(token: str) -> dict:
    """Return the Authorization header carrying token."""
    return {"Authorization": f"Bearer {token}"}


def _sign_in(client, account: _Account) -> dict:
    """Return the bearer header of an account signed in through the API."""
    return _bearer(_token(client, _SLUGS[account.tenant_id], account.email))


def _claims(token: str) -> dict:
    """Decode a token the API issued, with the secret the API verifies it with."""
    return jwt.decode(token, os.environ["KERNO_JWT_SECRET"], algorithms=["HS256"])


def _entry_body(provider_name: str) -> dict:
    """Return a valid register entry request for provider_name."""
    return {
        "provider_name": provider_name,
        "service_name": "Hosting",
        "provider_type": "cloud",
        "criticality_level": "critical",
        "business_function": "Platform",
        "data_types": ["operational"],
        "countries_supported": ["DE"],
    }


def _upload(client, headers: dict, filename: str, content: bytes, media_type: str):
    """POST one evidence file to the bounded multipart intake, with its token so authentication runs first."""
    return client.post(
        "/api/v1/evidence",
        files={"file": (filename, content, media_type)},
        data={"record_type": "policy"},
        headers=headers,
    )


def _upload_new(client, headers: dict, filename: str, content: bytes, media_type: str) -> str:
    """Upload a file that must be stored as a new record, and return its record_id."""
    response = _upload(client, headers, filename, content, media_type)
    assert response.status_code == 201, response.text
    assert (response.json()["deduplicated"], response.json()["source_system"]) == (False, "upload")
    return response.json()["record_id"]


def _listed(client, headers: dict, linked: str) -> dict:
    """Return {record_id: link_count} from the evidence list filtered by link status."""
    response = client.get("/api/v1/evidence", params={"linked": linked}, headers=headers)
    assert response.status_code == 200, response.text
    return {item["record_id"]: item["link_count"] for item in response.json()["items"]}


def _assert_tenant_b_cannot_reach(client, record_id: str) -> None:
    """Assert Tenant B's lead neither lists nor can link a Tenant A record."""
    headers = _sign_in(client, _LEAD_B)
    listed = client.get("/api/v1/evidence", headers=headers)
    assert (listed.status_code, listed.json()["total"]) == (200, 0)
    crossed = client.post(f"/api/v1/evidence/{record_id}/links", json={"control_id": _CONTROL}, headers=headers)
    assert (crossed.status_code, crossed.json()) == (404, {"detail": "evidence record or control not found"})


def _mock_llm_client() -> MagicMock:
    """Return an LLM client stand-in whose completion explains the fixed score, with no network."""
    choice = MagicMock()
    choice.message.content = json.dumps(
        {"rationale": _LLM_RATIONALE, "own_status": "met", "own_confidence": _LLM_OWN_CONFIDENCE}
    )
    client = MagicMock()
    client.chat.complete.return_value = MagicMock(choices=[choice])
    return client


def _generate(client, headers: dict) -> dict:
    """Run Analyse on the seeded control through the API and return the 201 body."""
    response = client.post("/api/v1/recommendations/generate", json={"control_id": _CONTROL}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _decide(client, headers: dict, recommendation_id: str, action: str):
    """Submit one review decision bound to recommendation_id; edit and reject name the other control and a reason."""
    body = {"action_type": action, "original_control_id": _CONTROL, "recommendation_id": recommendation_id}
    if action != "approve":
        body["corrected_control_id"] = _CORRECTED_CONTROL
        body["justification_text"] = _JUSTIFICATION
    return client.post("/api/v1/overrides", json=body, headers=headers)


def _queue_ids(client, headers: dict) -> list[str]:
    """Return the open review queue's recommendation ids for the seeded control."""
    response = client.get(
        "/api/v1/recommendations", params={"page_size": RECOMMENDATIONS_MAX_PAGE_SIZE}, headers=headers
    )
    assert response.status_code == 200, response.text
    return [item["recommendation_id"] for item in response.json()["items"] if item["control_id"] == _CONTROL]


def _generate_then_decide(client, headers: dict, action: str) -> dict:
    """Generate a recommendation, check it is the open one, record one bound decision on it, and return it."""
    generated = _generate(client, headers)
    assert _queue_ids(client, headers) == [generated["recommendation_id"]]
    decision = _decide(client, headers, generated["recommendation_id"], action)
    assert decision.status_code == 201, decision.text
    assert decision.json()["recommendation_id"] == generated["recommendation_id"]
    return generated


def _category(categories: list[dict]) -> dict:
    """Return this module's category from a coverage or Trust Center breakdown."""
    return next(item for item in categories if item["category"] == _CATEGORY)


def _coverage_by_control(client, headers: dict) -> dict:
    """Return this module's coverage rows keyed by control_id."""
    response = client.get("/api/v1/coverage/controls", params={"category": _CATEGORY}, headers=headers)
    assert response.status_code == 200, response.text
    return {row["control_id"]: row for row in response.json()}


def _pack_entry(content: bytes, control_id: str) -> dict:
    """Return one control's entry from an exported evidence pack."""
    controls = json.loads(content)["controls"]
    return next(entry for entry in controls if entry["control_id"] == control_id)


def _register_webhook(client, headers: dict) -> tuple[str, str]:
    """Create a jira registration, check its read-back never carries the secret, and return (id, secret)."""
    created = client.post("/api/v1/webhooks", json={"source_system": "jira"}, headers=headers)
    assert created.status_code == 201, created.text
    registration = created.json()
    assert (registration["tenant_id"], len(registration["signing_secret"])) == (str(TENANT_A_ID), _SECRET_HEX_LENGTH)
    fetched = client.get(f"/api/v1/webhooks/{registration['id']}", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert "signing_secret" not in fetched.json()
    assert (fetched.json()["source_system"], fetched.json()["is_active"]) == ("jira", True)
    return registration["id"], registration["signing_secret"]


def _delivery(external_ref: str) -> bytes:
    """Return one jira.issue.updated delivery naming the seeded control, with a Tenant B hint that must be ignored."""
    return json.dumps({
        "source_system": "jira",
        "event_type": "jira.issue.updated",
        "external_ref": external_ref,
        "payload": {"summary": f"Access review {external_ref}", "description": "Quarterly review completed."},
        "tenant_id_hint": str(TENANT_B_ID),
        "control_ref": _CONTROL_REF,
    }).encode("utf-8")


def _ingest(client, registration_id: str, secret: str, body: bytes):
    """POST one delivery signed over its exact bytes with secret, as the upstream system would."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {
        "X-Kerno-Webhook-Id": registration_id,
        "X-Kerno-Signature": f"sha256={digest}",
        "Content-Type": "application/json",
    }
    return client.post("/api/v1/webhooks/ingest", content=body, headers=headers)


def _assert_deliveries_landed_in_tenant_a(db_connection, registration_id: str) -> None:
    """Assert both deliveries became Tenant A records linked by the verified registration, and none reached Tenant B."""
    linked = _owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT r.external_id, l.linked_by FROM context_records r "
        "JOIN control_evidence_links l ON l.record_id = r.record_id "
        "WHERE r.tenant_id = %s AND l.control_id = %s ORDER BY r.external_id",
        [str(TENANT_A_ID), _CONTROL],
    )
    assert linked == [(external_ref, f"webhook:{registration_id}") for external_ref in _DELIVERY_REFS]
    dedup = _owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT count(*) FROM webhook_ingest_dedup WHERE tenant_id = %s", [str(TENANT_A_ID)],
    )
    assert dedup == [(len(_DELIVERY_REFS),)]
    tenant_b_records = _owner_rows(
        db_connection, TENANT_B_ID, "SELECT count(*) FROM context_records WHERE tenant_id = %s", [str(TENANT_B_ID)]
    )
    assert tenant_b_records == [(0,)]


def _bias_state(db_connection) -> tuple:
    """Return Tenant A's (override_count, last_recalculated_at) from its one retrieval_bias row."""
    rows = _owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT override_count, last_recalculated_at FROM retrieval_bias WHERE tenant_id = %s",
        [str(TENANT_A_ID)],
    )
    assert len(rows) == 1, rows
    return rows[0]


def test_login_resolves_through_h1_and_refuses_every_failure_alike(api, db_connection):
    token = _token(api, _SLUGS[TENANT_A_ID], _LEAD_A.email)
    claims = _claims(token)
    assert (claims["sub"], claims["tenant_id"], claims["role"], claims["email"]) == (
        _LEAD_A.user_id, str(TENANT_A_ID), "compliance_lead", _LEAD_A.email,
    )
    me = api.get("/api/v1/auth/me", headers=_bearer(token))
    assert (me.status_code, me.json()) == (200, {"email": _LEAD_A.email, "role": "compliance_lead"})

    other_organisation = _claims(_token(api, _SLUGS[TENANT_B_ID], _LEAD_B.email))
    assert (other_organisation["sub"], other_organisation["tenant_id"]) == (_LEAD_B.user_id, str(TENANT_B_ID))
    normalised = _login(api, f" {_SLUGS[TENANT_A_ID].upper()} ", _LEAD_A.email.upper())
    assert normalised.status_code == 200, normalised.text

    refusals = [
        _login(api, _SLUGS[TENANT_A_ID], _LEAD_A.email, password="not-the-password"),
        _login(api, _UNKNOWN_SLUG, _LEAD_A.email),
        _login(api, _SLUGS[TENANT_A_ID], "nobody@c2-runtime.example"),
        _login(api, _SLUGS[TENANT_A_ID], _INACTIVE_A.email),
        _login(api, _SLUGS[TENANT_B_ID], _PLATFORM_A.email),
    ]
    for refused in refusals:
        assert (refused.status_code, refused.json()) == (401, _INVALID_CREDENTIALS)
    assert _ledger(db_connection, TENANT_A_ID) == []
    _assert_ledger_chains_valid(db_connection)


def test_register_entry_create_and_amend_each_append_a_ledger_entry(api, db_connection):
    headers = _sign_in(api, _LEAD_A)
    created = api.post(_ENTRIES, json=_entry_body("Runtime Cloud Ltd"), headers=headers)
    assert created.status_code == 201, created.text
    entry_id = created.json()["register_entry_id"]
    assert created.json()["tenant_id"] == str(TENANT_A_ID)

    amendment = _entry_body("Runtime Cloud GmbH")
    amendment["criticality_level"] = "standard"
    amended = api.patch(f"{_ENTRIES}/{entry_id}", json=amendment, headers=headers)
    assert amended.status_code == 200, amended.text
    assert api.get(f"{_ENTRIES}/{entry_id}", headers=headers).json()["provider_name"] == "Runtime Cloud GmbH"
    assert [entry["register_entry_id"] for entry in api.get(_ENTRIES, headers=headers).json()] == [entry_id]

    rows = _ledger(db_connection, TENANT_A_ID)
    assert [row[:4] for row in rows] == [
        ("register_entry_created", "register_entry", entry_id, _LEAD_A.user_id),
        ("register_entry_updated", "register_entry", entry_id, _LEAD_A.user_id),
    ]
    assert rows[1][4]["provider_name"] == "Runtime Cloud Ltd"
    assert (rows[1][5]["provider_name"], rows[1][5]["criticality_level"]) == ("Runtime Cloud GmbH", "standard")
    _assert_ledger_chains_valid(db_connection)


def test_submission_run_repeat_read_and_frozen_package_download(api, db_connection):
    _seed_submission_window(db_connection)
    headers = _sign_in(api, _LEAD_A)
    entry = api.post(_ENTRIES, json=_entry_body("Filing Cloud Ltd"), headers=headers)
    assert entry.status_code == 201, entry.text
    run_request = {"submission_window_id": _SUBMISSION_WINDOW}

    started = api.post(_RUNS, json=run_request, headers=headers)
    assert started.status_code == 200, started.text
    run_id = started.json()["id"]
    restarted = api.post(_RUNS, json=run_request, headers=headers)
    assert (restarted.status_code, restarted.json()["id"]) == (200, run_id)
    read = api.get(f"{_RUNS}/{run_id}", headers=headers)
    assert (read.status_code, read.json()["id"], read.json()["tenant_id"]) == (200, run_id, str(TENANT_A_ID))

    package = api.get(f"{_RUNS}/{run_id}/package", headers=headers)
    assert package.status_code == 200, package.text
    assert "attachment" in package.headers["content-disposition"]
    assert b"Filing Cloud Ltd" in package.content
    stored = _owner_rows(
        db_connection, TENANT_A_ID, "SELECT frozen_package_json FROM dora_submission_runs WHERE id = %s", [run_id]
    )
    assert package.content.decode("utf-8") == stored[0][0]

    rows = _ledger(db_connection, TENANT_A_ID)
    assert [(row[0], row[2]) for row in rows] == [
        ("register_entry_created", entry.json()["register_entry_id"]),
        ("submission_run_recorded", run_id),
        ("submission_run_recorded", run_id),
        ("filing_package_downloaded", run_id),
    ]
    assert {row[3] for row in rows} == {_LEAD_A.user_id}
    _assert_ledger_chains_valid(db_connection)


def test_evidence_upload_link_unlink_and_list_as_runtime(api, db_connection):
    headers = _sign_in(api, _LEAD_A)
    text_id = _upload_new(api, headers, "access-review.txt", _POLICY_TEXT, "text/plain")
    pdf_id = _upload_new(api, headers, "isms.pdf", _minimal_pdf(_PDF_TEXT), "application/pdf")
    repeat = _upload(api, headers, "access-review.txt", _POLICY_TEXT, "text/plain")
    assert (repeat.status_code, repeat.json()["deduplicated"], repeat.json()["record_id"]) == (201, True, text_id)

    link_body = {"control_id": _CONTROL, "relevance_score": _LINK_RELEVANCE, "note": "Covers access reviews."}
    linked = api.post(f"/api/v1/evidence/{text_id}/links", json=link_body, headers=headers)
    assert linked.status_code == 201, linked.text
    assert (linked.json()["linked_by"], linked.json()["relevance_score"]) == (_LEAD_A.user_id, _LINK_RELEVANCE)
    assert _listed(api, headers, "true") == {text_id: 1}
    unlinked = api.delete(f"/api/v1/evidence/{text_id}/links/{_CONTROL}", headers=headers)
    assert unlinked.status_code == 204, unlinked.text
    assert _listed(api, headers, "false") == {text_id: 0, pdf_id: 0}

    removed = _owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT removed_at IS NOT NULL FROM control_evidence_links WHERE record_id = %s", [text_id],
    )
    assert removed == [(True,)]
    pdf_body = _owner_rows(
        db_connection, TENANT_A_ID, "SELECT body FROM context_records WHERE record_id = %s", [pdf_id]
    )
    assert _PDF_TEXT in pdf_body[0][0]
    _assert_tenant_b_cannot_reach(api, text_id)

    rows = _ledger(db_connection, TENANT_A_ID)
    assert [(row[0], row[2], row[3]) for row in rows] == [
        ("evidence_uploaded", text_id, _LEAD_A.user_id),
        ("evidence_uploaded", pdf_id, _LEAD_A.user_id),
        ("evidence_linked", text_id, _LEAD_A.user_id),
        ("evidence_unlinked", text_id, _LEAD_A.user_id),
    ]
    assert _ledger(db_connection, TENANT_B_ID) == []
    _assert_ledger_chains_valid(db_connection)


def test_generated_recommendations_take_bound_approve_edit_and_reject_decisions(api, db_connection, monkeypatch):
    _seed_scored_evidence(db_connection, _MET_RELEVANCE)
    monkeypatch.setenv("KERNO_LLM_MODEL", _LLM_MODEL)
    headers = _sign_in(api, _LEAD_A)
    generations = []
    with patch("src.services.recommendation_service.get_llm_client", return_value=_mock_llm_client()):
        for action in _REVIEW_ACTIONS:
            generations.append(_generate_then_decide(api, headers, action))
    generated_ids = [generated["recommendation_id"] for generated in generations]
    for generated in generations:
        assert (generated["status"], generated["rationale_source"], generated["rationale"]) == (
            "met", "llm", _LLM_RATIONALE,
        )
        assert generated["evidence_ids"] == [_RECORD]

    stale = _decide(api, headers, generated_ids[0], "approve")
    assert stale.status_code == 409, stale.text
    assert "newer recommendation" in stale.json()["detail"]
    assert _queue_ids(api, headers) == []

    decisions = _owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT action_type, recommendation_id::text FROM overrides "
        "WHERE original_control_id = %s ORDER BY created_at, override_id", [_CONTROL],
    )
    assert decisions == list(zip(_REVIEW_ACTIONS, generated_ids))
    superseded = _owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT is_superseded FROM recommendations WHERE control_id = %s ORDER BY generated_at", [_CONTROL],
    )
    assert superseded == [(True,), (True,), (False,)]
    rows = _ledger(db_connection, TENANT_A_ID)
    assert [row[0] for row in rows] == [
        "recommendation_generated", "approve", "recommendation_generated", "edit", "recommendation_generated", "reject",
    ]
    assert {row[3] for row in rows} == {_LEAD_A.user_id}
    assert [row[4]["recommendation_id"] for row in rows if row[1] == "override"] == generated_ids
    _assert_ledger_chains_valid(db_connection)


def test_coverage_and_evidence_pack_export_read_as_runtime(api, db_connection):
    _seed_scored_evidence(db_connection, _MET_RELEVANCE)
    headers = _sign_in(api, _LEAD_A)
    recommendation_id = _generate(api, headers)["recommendation_id"]
    approved = _decide(api, headers, recommendation_id, "approve")
    assert approved.status_code == 201, approved.text

    summary = api.get("/api/v1/coverage/summary", headers=headers)
    assert summary.status_code == 200, summary.text
    assert _category(summary.json()["categories"]) == {
        "category": _CATEGORY, "met": 1, "partial": 0, "gap": 1, "total": len(_CONTROLS),
    }
    assert summary.json()["last_recalculated_at"] is None
    controls = _coverage_by_control(api, headers)
    decided = controls[_CONTROL]
    assert (decided["status"], decided["status_source"], decided["human_confirmed"]) == ("met", "override", True)
    assert (decided["recommendation_id"], decided["evidence_count"]) == (recommendation_id, 1)
    untouched = controls[_CORRECTED_CONTROL]
    assert (untouched["status"], untouched["status_source"], untouched["recommendation_id"]) == ("gap", "none", None)

    exported = api.get("/api/v1/export/evidence-pack", params={"control_family": _CATEGORY}, headers=headers)
    assert exported.status_code == 200, exported.text
    assert "attachment" in exported.headers["content-disposition"]
    entry = _pack_entry(exported.content, _CONTROL)
    assert (entry["decided_by"], entry["system_of_record_status"], entry["recommendation_id"]) == (
        "human_confirmed", "met", recommendation_id,
    )
    assert [decision["action_type"] for decision in entry["decisions"]] == ["approve"]

    rows = _ledger(db_connection, TENANT_A_ID)
    assert [row[0] for row in rows] == ["recommendation_generated", "approve", "export_generated"]
    assert rows[2][1] == "evidence_pack"
    _assert_ledger_chains_valid(db_connection)


def test_webhook_registration_rotation_and_ingest_verify_through_h2(api, db_connection):
    headers = _sign_in(api, _PLATFORM_A)
    registration_id, first_secret = _register_webhook(api, headers)
    first_ref, second_ref = _DELIVERY_REFS

    accepted = _ingest(api, registration_id, first_secret, _delivery(first_ref))
    assert (accepted.status_code, accepted.json()["status"]) == (201, "ingested"), accepted.text
    repeated = _ingest(api, registration_id, first_secret, _delivery(first_ref))
    assert (repeated.status_code, repeated.json()) == (200, {"status": "duplicate", "correlation_id": None})

    rotated = api.post(f"/api/v1/webhooks/{registration_id}/rotate", headers=headers)
    assert rotated.status_code == 200, rotated.text
    second_secret = rotated.json()["signing_secret"]
    assert second_secret != first_secret
    retired = _ingest(api, registration_id, first_secret, _delivery(second_ref))
    unknown = _ingest(api, str(uuid.uuid4()), second_secret, _delivery(second_ref))
    for refused in (retired, unknown):
        assert (refused.status_code, refused.json()) == (401, _INVALID_SIGNATURE)
    fresh = _ingest(api, registration_id, second_secret, _delivery(second_ref))
    assert (fresh.status_code, fresh.json()["status"]) == (201, "ingested"), fresh.text

    _assert_deliveries_landed_in_tenant_a(db_connection, registration_id)
    rows = _ledger(db_connection, TENANT_A_ID)
    assert [(row[0], row[2]) for row in rows] == [
        ("webhook_ingested", accepted.json()["correlation_id"]),
        ("webhook_ingested", fresh.json()["correlation_id"]),
    ]
    assert _ledger(db_connection, TENANT_B_ID) == []
    _assert_ledger_chains_valid(db_connection)


def test_trust_center_status_follows_the_visibility_toggle(api, db_connection):
    slug = _SLUGS[TENANT_A_ID]
    status_path = f"/trust-center/{slug}/status"
    for hidden in (api.get(status_path), api.get(f"/trust-center/{_UNKNOWN_SLUG}/status")):
        assert (hidden.status_code, hidden.json()) == (404, {"detail": "not found"})

    headers = _sign_in(api, _LEAD_A)
    opened = api.put(_VISIBILITY, json={"public": True}, headers=headers)
    assert (opened.status_code, opened.json()) == (200, {"tenant_slug": slug, "trust_center_public": True})
    published = api.get(status_path)
    cached = api.get(status_path)
    assert published.status_code == 200, published.text
    assert "tenant_id" not in published.json()
    assert _category(published.json()["categories"]) == {
        "category": _CATEGORY, "met": 0, "partial": 0, "gap": len(_CONTROLS), "total": len(_CONTROLS),
    }
    assert cached.json()["generated_at"] == published.json()["generated_at"]

    closed = api.put(_VISIBILITY, json={"public": False}, headers=headers)
    assert (closed.status_code, closed.json()["trust_center_public"]) == (200, False)
    assert api.get(status_path).status_code == 404
    flags = _owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT tenant_id::text, trust_center_public FROM tenants WHERE tenant_id IN (%s, %s) ORDER BY tenant_id",
        [str(TENANT_A_ID), str(TENANT_B_ID)],
    )
    assert flags == [(str(TENANT_A_ID), False), (str(TENANT_B_ID), False)]
    rows = _ledger(db_connection, TENANT_A_ID)
    assert [row[:3] for row in rows] == [("trust_center_snapshot", "trust_center", slug)]
    _assert_ledger_chains_valid(db_connection)


def test_remediation_trigger_and_close_callback_with_jira_mocked(api, db_connection):
    _seed_default_routing_rule(db_connection)
    headers = _sign_in(api, _PLATFORM_A)
    with patch("src.services.remediation_service.JiraClient") as jira_class:
        jira = jira_class.return_value
        jira.project_key = _JIRA_PROJECT
        jira.create_issue.return_value = _JIRA_ISSUE
        triggered = api.post("/api/v1/remediation/trigger", json={"control_id": _CONTROL}, headers=headers)
    assert triggered.status_code == 201, triggered.text
    assert (triggered.json()["control_id"], triggered.json()["jira_issue_key"]) == (_CONTROL, _JIRA_ISSUE)
    assert jira.create_issue.call_args.kwargs["assignee_account_id"] == _JIRA_ASSIGNEE

    callback = {"jira_issue_key": _JIRA_ISSUE, "control_id": _CONTROL}
    closed = api.post("/api/v1/remediation/close-callback", json=callback, headers=headers)
    assert (closed.status_code, closed.json()) == (200, {"control_id": _CONTROL, "flagged_for_rereview": True})
    repeated = api.post("/api/v1/remediation/close-callback", json=callback, headers=headers)
    assert repeated.status_code == 422, repeated.text

    stamps = _owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT closed_at IS NOT NULL, re_review_flagged_at IS NOT NULL FROM remediation_tasks "
        "WHERE tenant_id = %s AND jira_issue_key = %s",
        [str(TENANT_A_ID), _JIRA_ISSUE],
    )
    assert stamps == [(True, True)]
    rows = _ledger(db_connection, TENANT_A_ID)
    assert [(row[0], row[2]) for row in rows] == [
        ("remediation_triggered", _CONTROL),
        ("remediation_closed", _CONTROL),
    ]
    _assert_ledger_chains_valid(db_connection)


def test_manual_recalculation_inserts_then_updates_the_bias_row(api, db_connection):
    _seed_embeddings(db_connection)
    _seed_unbound_edit(db_connection, created_at=None)
    headers = _sign_in(api, _LEAD_A)

    first = api.post(_RECALCULATE, headers=headers)
    assert first.status_code == 200, first.text
    assert (first.json()["tenant_id"], first.json()["status"]) == (str(TENANT_A_ID), "recalculated")
    assert (first.json()["override_count"], first.json()["dimensions"]) == (1, EMBEDDING_DIMENSION)
    first_count, first_run_at = _bias_state(db_connection)
    assert first_count == 1

    _seed_unbound_edit(db_connection, created_at=first_run_at + _JUST_AFTER)
    second = api.post(_RECALCULATE, headers=headers)
    assert (second.status_code, second.json()["status"], second.json()["override_count"]) == (200, "recalculated", 1)
    second_count, second_run_at = _bias_state(db_connection)
    assert second_count == 1
    assert second_run_at > first_run_at

    idle = api.post(_RECALCULATE, headers=headers)
    assert (idle.status_code, idle.json()["status"], idle.json()["override_count"]) == (200, "no_new_overrides", 0)
    rows = _ledger(db_connection, TENANT_A_ID)
    assert [(row[0], row[2]) for row in rows] == [
        ("bias_recalculated", str(TENANT_A_ID)),
        ("bias_recalculated", str(TENANT_A_ID)),
    ]
    _assert_ledger_chains_valid(db_connection)


def test_ai_decisions_panel_and_reference_windows_read_as_runtime(api, db_connection):
    _seed_scored_evidence(db_connection, _MET_RELEVANCE)
    _seed_reporting_window(db_connection)
    headers = _sign_in(api, _LEAD_A)
    generated = _generate(api, headers)

    decisions = api.get("/api/v1/ai-decisions", params={"control_id": _CONTROL}, headers=headers)
    assert decisions.status_code == 200, decisions.text
    assert decisions.json()["count"] == 1
    decision = decisions.json()["decisions"][0]
    assert (decision["control_id"], decision["output_status"], decision["evidence_ids"]) == (_CONTROL, "met", [_RECORD])
    assert decision["model_version"] == f"{SCORING_ENGINE_VERSION}+template"

    panel = api.get(f"/api/v1/panel/controls/{_CONTROL}", headers=headers)
    assert panel.status_code == 200, panel.text
    assert panel.json()["recommendation"]["recommendation_id"] == generated["recommendation_id"]
    assert [(item["record_id"], item["link_status"]) for item in panel.json()["evidence"]] == [(_RECORD, "active")]

    windows = api.get("/api/v1/register/windows")
    assert windows.status_code == 200, windows.text
    assert _REPORTING_WINDOW in [window["reporting_window_id"] for window in windows.json()]
    submission_windows = api.get("/api/v1/submissions/windows")
    assert submission_windows.status_code == 200, submission_windows.text

    rows = _ledger(db_connection, TENANT_A_ID)
    assert [(row[0], row[2]) for row in rows] == [("recommendation_generated", generated["recommendation_id"])]
    _assert_ledger_chains_valid(db_connection)
