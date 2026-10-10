"""SEC-REMED-005: a review decision binds to the recommendation the reviewer saw, end to end through the real API.

Real JWTs, the real app and the approved kerno_test database; only get_conn is
replaced, by a stand-in that commits or rolls back as production does. Run:
pytest tests/integration/test_sec_remed_005_review_binding.py --require-live-database -m integration
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import jwt
import psycopg2
import pytest
from fastapi.testclient import TestClient

from config.constants import RECOMMENDATIONS_MAX_PAGE_SIZE
from src.api.app import create_app
from src.api.dependencies import get_conn
from src.api.rate_limit import limiter
from src.services import export_service
from src.services.recommendation_service import generate_recommendation
from tests.conftest import _DbConnection

_CATEGORY = "sec_remed_005_review"
_CONTROL = "c5050000-0000-4000-c000-000000000001"
_OTHER_CONTROL = "c5050000-0000-4000-c000-000000000002"
_RECORD = "c5050000-0000-4000-c000-000000000011"
_LINK = "c5050000-0000-4000-c000-000000000012"
_REVIEWER = "d5050000-0000-4000-d000-000000000001"
_ROLE = "compliance_lead"
_TOKEN_LIFETIME_SECONDS = 3600
_LEGACY_DECISION_DELAY = timedelta(seconds=1)

# Relevance scores that make the deterministic scorer say met, then gap.
_MET_RELEVANCE = 0.9
_GAP_RELEVANCE = 0.2

_LOCK_TIMEOUT_MS = 10_000
_STATEMENT_TIMEOUT_FACTOR = 2


@pytest.fixture
def review_seed(db_connection, tenant_a_id, tenant_b_id, monkeypatch):
    """Seed two catalogue controls and one met-scored Tenant A evidence link; remove everything afterwards.

    The template rationale path is forced (no LLM). recommendations and links
    are outside the shared teardown, and bound decisions must go first.
    """
    monkeypatch.delenv("KERNO_LLM_MODEL", raising=False)
    _seed_controls_and_evidence(db_connection, tenant_a_id)
    yield
    _remove_seeded_rows(db_connection, (tenant_a_id, tenant_b_id))


def _seed_controls_and_evidence(db_connection, tenant_a_id) -> None:
    with db_connection.transaction():
        for control_id, ref in ((_CONTROL, "SR005-A"), (_OTHER_CONTROL, "SR005-B")):
            db_connection.execute(
                """INSERT INTO compliance_controls
                   (control_id, framework, control_ref, category, title,
                    obligation_text, entity_types, is_active)
                   VALUES (%s, 'NIS2', %s, %s, 'Review binding test control',
                           'Test obligation.', %s, TRUE)
                   ON CONFLICT (control_id) DO NOTHING""",
                [control_id, ref, _CATEGORY, ["essential"]],
            )
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        db_connection.execute(
            """INSERT INTO context_records
               (record_id, tenant_id, source_system, record_type, title, body)
               VALUES (%s, %s, 'confluence', 'policy', 'Access policy', 'Reviewed access policy.')""",
            [_RECORD, str(tenant_a_id)],
        )
        db_connection.execute(
            """INSERT INTO control_evidence_links
               (link_id, control_id, record_id, linked_by, linked_at, relevance_score)
               VALUES (%s, %s, %s, 'integration-test', now(), %s)""",
            [_LINK, _CONTROL, _RECORD, _MET_RELEVANCE],
        )


def _remove_seeded_rows(db_connection, tenant_ids) -> None:
    db_connection.rollback()
    for tenant_id in tenant_ids:
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
            db_connection.execute(
                "DELETE FROM overrides WHERE original_control_id IN (%s, %s)",
                [_CONTROL, _OTHER_CONTROL],
            )
            db_connection.execute(
                "DELETE FROM recommendations WHERE control_id IN (%s, %s)",
                [_CONTROL, _OTHER_CONTROL],
            )
            db_connection.execute("DELETE FROM control_evidence_links WHERE link_id = %s", [_LINK])
            db_connection.execute("DELETE FROM context_records WHERE record_id = %s", [_RECORD])
    with db_connection.transaction():
        db_connection.execute(
            "DELETE FROM compliance_controls WHERE control_id IN (%s, %s)", [_CONTROL, _OTHER_CONTROL]
        )


@pytest.fixture
def api(db_connection, review_seed):
    limiter.reset()
    app = create_app()

    def _conn():
        try:
            yield db_connection
            db_connection.commit()
        except Exception:
            db_connection.rollback()
            raise

    app.dependency_overrides[get_conn] = _conn
    yield TestClient(app, raise_server_exceptions=False)
    limiter.reset()


def _bearer(tenant_id, role: str = _ROLE) -> dict:
    token = jwt.encode(
        {
            "sub": _REVIEWER,
            "tenant_id": str(tenant_id),
            "role": role,
            "email": "reviewer@example.test",
            "exp": int(time.time()) + _TOKEN_LIFETIME_SECONDS,
        },
        os.environ["KERNO_JWT_SECRET"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _session() -> _DbConnection:
    raw = psycopg2.connect(os.environ["DATABASE_URL"])
    raw.set_session(isolation_level="READ COMMITTED", autocommit=False)
    conn = _DbConnection(raw)
    conn.execute("SET lock_timeout = %s", [f"{_LOCK_TIMEOUT_MS}ms"])
    conn.execute("SET statement_timeout = %s", [f"{_LOCK_TIMEOUT_MS * _STATEMENT_TIMEOUT_FACTOR}ms"])
    conn.commit()
    return conn


def _generate(conn, tenant_id, control_id: str = _CONTROL):
    with conn.transaction():
        return generate_recommendation(
            conn, tenant_id, control_id, triggered_by_user_id=_REVIEWER, triggered_by_role=_ROLE
        )


def _set_relevance(conn, tenant_id, score: float) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        conn.execute(
            "UPDATE control_evidence_links SET relevance_score = %s WHERE link_id = %s", [score, _LINK]
        )


def _queue_ids(api, tenant_id, control_id: str = _CONTROL) -> list[str]:
    response = api.get(
        "/api/v1/recommendations",
        headers=_bearer(tenant_id),
        params={"page_size": RECOMMENDATIONS_MAX_PAGE_SIZE},
    )
    assert response.status_code == 200, response.text
    return [row["recommendation_id"] for row in response.json()["items"] if row["control_id"] == control_id]


def _coverage(api, tenant_id, control_id: str = _CONTROL) -> dict:
    response = api.get("/api/v1/coverage/controls", headers=_bearer(tenant_id), params={"category": _CATEGORY})
    assert response.status_code == 200, response.text
    return next(row for row in response.json() if row["control_id"] == control_id)


def _pack_entry(api, tenant_id, control_id: str = _CONTROL) -> dict:
    response = api.get(
        "/api/v1/export/evidence-pack", headers=_bearer(tenant_id), params={"control_family": _CATEGORY}
    )
    assert response.status_code == 200, response.text
    return next(entry for entry in json.loads(response.content)["controls"] if entry["control_id"] == control_id)


def _submit(api, tenant_id, recommendation_id, control_id: str = _CONTROL, action: str = "approve"):
    body = {
        "action_type": action,
        "original_control_id": control_id,
        "corrected_control_id": None if action == "approve" else _OTHER_CONTROL,
        "justification_text": None if action == "approve" else "The reviewer disagrees with this mapping.",
    }
    if recommendation_id is not None:
        body["recommendation_id"] = recommendation_id
    return api.post("/api/v1/overrides", headers=_bearer(tenant_id), json=body)


def _decision_count(conn, tenant_id, control_id: str = _CONTROL) -> int:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return conn.execute(
            "SELECT count(*) FROM overrides WHERE original_control_id = %s", [control_id]
        ).fetchone()[0]


def _decision_event_count(conn, tenant_id, control_id: str = _CONTROL) -> int:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return conn.execute(
            "SELECT count(*) FROM audit_log WHERE object_type = 'override' AND control_id = %s",
            [control_id],
        ).fetchone()[0]


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _approve_r1_then_generate_changed_r2(api, conn, tenant_id):
    r1 = _generate(conn, tenant_id)
    assert _queue_ids(api, tenant_id) == [r1.recommendation_id]
    approval = _submit(api, tenant_id, r1.recommendation_id)
    assert approval.status_code == 201, approval.text
    _set_relevance(conn, tenant_id, _GAP_RELEVANCE)
    r2 = _generate(conn, tenant_id)
    assert (r1.status, r2.status) == ("met", "gap")
    return r1, r2, approval.json()


@pytest.mark.integration
def test_approving_r1_then_generating_a_changed_r2_leaves_r2_unconfirmed_and_open(
    api, db_connection, tenant_a_id
):
    r1, r2, _ = _approve_r1_then_generate_changed_r2(api, db_connection, tenant_a_id)

    coverage = _coverage(api, tenant_a_id)
    assert coverage["human_confirmed"] is False
    assert (coverage["status"], coverage["status_source"]) == ("gap", "recommendation")
    assert _queue_ids(api, tenant_a_id) == [r2.recommendation_id]

    entry = _pack_entry(api, tenant_a_id)
    assert entry["decided_by"] == "ai_unconfirmed"
    assert entry["system_of_record_status"] == "gap"
    assert (entry["rationale"], entry["gaps"]) == (r2.rationale, r2.gaps)
    assert _instant(entry["decided_at"]) == r2.generated_at
    assert [d["action_type"] for d in entry["decisions"]] == ["approve"]

    assert coverage["recommendation_id"] == r2.recommendation_id
    assert entry["recommendation_id"] == r2.recommendation_id
    assert [d["recommendation_id"] for d in entry["decisions"]] == [r1.recommendation_id]


@pytest.mark.integration
def test_explicit_approval_of_r2_makes_coverage_queue_and_export_agree(api, db_connection, tenant_a_id):
    r1, r2, _ = _approve_r1_then_generate_changed_r2(api, db_connection, tenant_a_id)

    approval = _submit(api, tenant_a_id, r2.recommendation_id)
    assert approval.status_code == 201, approval.text
    assert approval.json()["recommendation_id"] == r2.recommendation_id

    coverage = _coverage(api, tenant_a_id)
    assert (coverage["status"], coverage["status_source"], coverage["human_confirmed"]) == (
        "gap", "override", True,
    )
    assert coverage["recommendation_id"] == r2.recommendation_id
    assert _queue_ids(api, tenant_a_id) == []

    entry = _pack_entry(api, tenant_a_id)
    assert (entry["decided_by"], entry["system_of_record_status"]) == ("human_confirmed", "gap")
    assert entry["recommendation_id"] == r2.recommendation_id
    assert (entry["rationale"], entry["gaps"]) == (r2.rationale, r2.gaps)
    assert _instant(entry["decided_at"]) == _instant(approval.json()["created_at"])
    assert [d["recommendation_id"] for d in entry["decisions"]] == [
        r1.recommendation_id, r2.recommendation_id,
    ]


@pytest.mark.integration
@pytest.mark.parametrize("action", ["approve", "edit", "reject"])
def test_a_stale_r1_screen_cannot_decide_r2(api, db_connection, tenant_a_id, action):
    r1 = _generate(db_connection, tenant_a_id)
    displayed = _queue_ids(api, tenant_a_id)
    assert displayed == [r1.recommendation_id]
    _set_relevance(db_connection, tenant_a_id, _GAP_RELEVANCE)
    r2 = _generate(db_connection, tenant_a_id)

    response = _submit(api, tenant_a_id, displayed[0], action=action)

    assert response.status_code == 409, response.text
    assert "newer recommendation" in response.json()["detail"]
    assert _decision_count(db_connection, tenant_a_id) == 0
    assert _decision_event_count(db_connection, tenant_a_id) == 0
    coverage = _coverage(api, tenant_a_id)
    assert (coverage["human_confirmed"], coverage["recommendation_id"]) == (False, r2.recommendation_id)
    assert _queue_ids(api, tenant_a_id) == [r2.recommendation_id]


@pytest.mark.integration
def test_a_recommendation_from_another_tenant_is_refused_like_a_missing_one(
    api, db_connection, tenant_a_id, tenant_b_id
):
    _generate(db_connection, tenant_a_id)
    tenant_b_recommendation = _generate(db_connection, tenant_b_id)

    cross_tenant = _submit(api, tenant_a_id, tenant_b_recommendation.recommendation_id)
    nonexistent = _submit(api, tenant_a_id, str(uuid.uuid4()))

    assert (cross_tenant.status_code, cross_tenant.json()) == (404, {"detail": "entry not found"})
    assert (nonexistent.status_code, nonexistent.json()) == (404, {"detail": "entry not found"})
    for tenant_id in (tenant_a_id, tenant_b_id):
        assert _decision_count(db_connection, tenant_id) == 0
        assert _decision_event_count(db_connection, tenant_id) == 0
    assert _queue_ids(api, tenant_b_id) == [tenant_b_recommendation.recommendation_id]


@pytest.mark.integration
def test_a_recommendation_for_another_control_is_refused(api, db_connection, tenant_a_id):
    _generate(db_connection, tenant_a_id)
    other = _generate(db_connection, tenant_a_id, _OTHER_CONTROL)

    response = _submit(api, tenant_a_id, other.recommendation_id, control_id=_CONTROL)

    assert response.status_code == 422, response.text
    assert "does not belong to control" in response.json()["detail"]
    for control_id in (_CONTROL, _OTHER_CONTROL):
        assert _decision_count(db_connection, tenant_a_id, control_id) == 0
        assert _decision_event_count(db_connection, tenant_a_id, control_id) == 0


@pytest.mark.integration
@pytest.mark.parametrize("recommendation_id", [None, "", "not-a-uuid"])
def test_a_missing_or_malformed_recommendation_id_is_refused(
    api, db_connection, tenant_a_id, recommendation_id
):
    _generate(db_connection, tenant_a_id)

    response = _submit(api, tenant_a_id, recommendation_id)

    assert response.status_code == 422, response.text
    assert _decision_count(db_connection, tenant_a_id) == 0
    assert _decision_event_count(db_connection, tenant_a_id) == 0


@pytest.mark.integration
def test_a_legacy_unbound_decision_stays_visible_without_confirming_anything(
    api, db_connection, tenant_a_id
):
    r1 = _generate(db_connection, tenant_a_id)
    legacy_id = str(uuid.uuid4())
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        db_connection.execute(
            """INSERT INTO overrides
               (override_id, tenant_id, reviewer_id, reviewer_role, action_type,
                original_control_id, reviewer_confidence_weight, created_at)
               VALUES (%s, %s, %s, 'vciso', 'approve', %s, 1.0, %s)""",
            [legacy_id, str(tenant_a_id), _REVIEWER, _CONTROL, r1.generated_at + _LEGACY_DECISION_DELAY],
        )

    coverage = _coverage(api, tenant_a_id)
    assert (coverage["human_confirmed"], coverage["status_source"]) == (False, "recommendation")
    assert _queue_ids(api, tenant_a_id) == [r1.recommendation_id]
    entry = _pack_entry(api, tenant_a_id)
    assert entry["decided_by"] == "ai_unconfirmed"
    assert _instant(entry["decided_at"]) == r1.generated_at
    assert [d["override_id"] for d in entry["decisions"]] == [legacy_id]
    assert entry["decisions"][0]["recommendation_id"] is None


@pytest.mark.integration
def test_a_failed_ledger_append_leaves_neither_the_decision_nor_its_event(api, db_connection, tenant_a_id):
    r1 = _generate(db_connection, tenant_a_id)

    with patch(
        "src.services.override_service.append_audit_entry",
        side_effect=RuntimeError("ledger unavailable"),
    ):
        response = _submit(api, tenant_a_id, r1.recommendation_id)

    assert response.status_code == 500
    assert _decision_count(db_connection, tenant_a_id) == 0
    assert _decision_event_count(db_connection, tenant_a_id) == 0
    assert _queue_ids(api, tenant_a_id) == [r1.recommendation_id]


@pytest.mark.integration
def test_export_never_pairs_r2_with_the_confirmation_of_r1(api, db_connection, tenant_a_id):
    r1 = _generate(db_connection, tenant_a_id)
    approval = _submit(api, tenant_a_id, r1.recommendation_id)
    assert approval.status_code == 201, approval.text
    real_coverage = export_service.get_coverage_controls
    replacements = []

    def coverage_then_replacement_commits(conn, tenant_id, category=None):
        rows = real_coverage(conn, tenant_id, category=category)
        other = _session()
        try:
            _set_relevance(other, tenant_a_id, _GAP_RELEVANCE)
            replacements.append(_generate(other, tenant_a_id))
        finally:
            other._conn.close()
        return rows

    with patch.object(export_service, "get_coverage_controls", coverage_then_replacement_commits):
        entry = _pack_entry(api, tenant_a_id)

    assert replacements and replacements[0].status == "gap"
    assert (entry["decided_by"], entry["system_of_record_status"]) == ("human_confirmed", "met")
    assert (entry["rationale"], entry["gaps"]) == (r1.rationale, r1.gaps)
    assert _instant(entry["decided_at"]) == _instant(approval.json()["created_at"])
    assert entry["recommendation_id"] == r1.recommendation_id
