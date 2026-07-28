"""Orphan-fix integration tests — webhook ingest links evidence on arrival (live DB).

Proves against a real database that a signed delivery carrying control_ref
creates BOTH the context_record and its control_evidence_links row in one
transaction, that linked_by records the verified registration id, and that an
unknown control_ref is a 422 that writes nothing at all. Required by the §11
live-database rule: linking on ingest is a new production database path, and
mocked spies cannot prove the RLS policy (which reaches control_evidence_links
indirectly, through the record's tenant) actually permits the insert.

Run: pytest tests/integration/test_ker205_evidence_link.py -m integration -v
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn

_CONTROL_UUID = str(uuid.UUID("c2050000-0000-4000-c000-000000000001"))
_CONTROL_REF = "KER205-LINK-TEST"
_REGISTRATION_ID = str(uuid.UUID("d2050000-0000-4000-d000-000000000001"))
_SECRET = "a1b2" * 16  # 64 chars, same shape as a real signing secret


def _client(db_connection) -> TestClient:
    """App wired to the live test connection, so writes hit the real database."""
    app = create_app()

    def _conn():
        yield db_connection

    app.dependency_overrides[get_conn] = _conn
    return TestClient(app)


def _delivery(control_ref: str | None, external_ref: str) -> bytes:
    event = {
        "source_system": "jira",
        "event_type": "generic.evidence.submitted",
        "external_ref": external_ref,
        "payload": {"summary": "Quarterly access review", "description": "Completed."},
        "tenant_id_hint": None,
    }
    if control_ref is not None:
        event["control_ref"] = control_ref
    return json.dumps(event).encode("utf-8")


def _headers(body: bytes) -> dict:
    digest = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Kerno-Webhook-Id": _REGISTRATION_ID,
        "X-Kerno-Signature": f"sha256={digest}",
        "Content-Type": "application/json",
    }


@pytest.fixture
def ker205_link_seed(db_connection, tenant_a_id):
    """Seed the catalogue control and Tenant A's webhook registration.

    The control is platform-global; the registration lives on the unforced
    webhook_registrations table (migration 022's auth-bootstrap exception), so
    neither insert needs tenant context. Records and links created by the tests
    are cleaned by the shared conftest teardown, which now removes links before
    the records they reference.
    """
    with db_connection.transaction():
        db_connection.execute(
            """INSERT INTO compliance_controls
               (control_id, framework, control_ref, category, title,
                obligation_text, entity_types, is_active)
               VALUES (%s, 'NIS2', %s, 'governance', 'Link test control',
                       'Test obligation.', %s, TRUE)
               ON CONFLICT (control_id) DO NOTHING""",
            [_CONTROL_UUID, _CONTROL_REF, ["essential"]],
        )
        db_connection.execute(
            """INSERT INTO webhook_registrations
               (id, tenant_id, source_system, signing_secret, is_active)
               VALUES (%s, %s, 'jira', %s, TRUE)
               ON CONFLICT (id) DO NOTHING""",
            [_REGISTRATION_ID, str(tenant_a_id), _SECRET],
        )

    yield

    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        db_connection.execute(
            "DELETE FROM control_evidence_links WHERE control_id = %s", [_CONTROL_UUID]
        )
    with db_connection.transaction():
        db_connection.execute(
            "DELETE FROM webhook_registrations WHERE id = %s", [_REGISTRATION_ID]
        )
        db_connection.execute(
            "DELETE FROM compliance_controls WHERE control_id = %s", [_CONTROL_UUID]
        )


@pytest.mark.integration
def test_control_ref_links_evidence_on_arrival(db_connection, tenant_a_id, ker205_link_seed):
    body = _delivery(_CONTROL_REF, "ACCESS-REVIEW-Q1")
    response = _client(db_connection).post(
        "/api/v1/webhooks/ingest", content=body, headers=_headers(body)
    )
    assert response.status_code == 201
    record_id = response.json()["correlation_id"]

    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        row = db_connection.execute(
            "SELECT linked_by, relevance_score, removed_at FROM control_evidence_links "
            "WHERE control_id = %s AND record_id = %s",
            [_CONTROL_UUID, record_id],
        ).fetchone()

    assert row is not None, "ingest with control_ref must create the link — not an orphan"
    linked_by, relevance_score, removed_at = row
    assert linked_by == f"webhook:{_REGISTRATION_ID}", "linked_by is the verified registration"
    assert relevance_score is None, "an automated link carries no human relevance score"
    assert removed_at is None, "the link is active"


@pytest.mark.integration
def test_unknown_control_ref_writes_nothing(db_connection, tenant_a_id, ker205_link_seed):
    body = _delivery("KER205-DOES-NOT-EXIST", "ACCESS-REVIEW-Q2")
    response = _client(db_connection).post(
        "/api/v1/webhooks/ingest", content=body, headers=_headers(body)
    )
    assert response.status_code == 422

    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        count = db_connection.execute(
            "SELECT count(*) FROM context_records WHERE external_id = %s",
            ["ACCESS-REVIEW-Q2"],
        ).fetchone()[0]
    assert count == 0, "a rejected delivery must leave no half-written record"


@pytest.mark.integration
def test_delivery_without_control_ref_still_ingests_unlinked(
    db_connection, tenant_a_id, ker205_link_seed
):
    # Backward compatibility: senders that predate control_ref keep working;
    # their evidence lands unlinked and is surfaced by KER-406's AC-3 list.
    body = _delivery(None, "ACCESS-REVIEW-Q3")
    response = _client(db_connection).post(
        "/api/v1/webhooks/ingest", content=body, headers=_headers(body)
    )
    assert response.status_code == 201
    record_id = response.json()["correlation_id"]

    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        link_count = db_connection.execute(
            "SELECT count(*) FROM control_evidence_links WHERE record_id = %s",
            [record_id],
        ).fetchone()[0]
    assert link_count == 0
