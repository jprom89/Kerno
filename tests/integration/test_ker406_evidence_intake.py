"""KER-406 integration tests — evidence intake against a live database.

Covers every new production database path the §11 rule names: upload, dedupe
on content_hash, link, and soft-delete via removed_at. Mocked spies cannot
prove any of these: the link insert passes through an RLS policy that reaches
control_evidence_links INDIRECTLY (via a subquery to the record's tenant), and
the two bad-id cases fail with different driver exceptions — which is exactly
why AC-4 pre-validates instead of catching them.

Run: pytest tests/integration/test_ker406_evidence_intake.py -m integration -v
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
from src.api.routers.overrides import get_reviewer_id

_CONTROL_UUID = str(uuid.UUID("c4060000-0000-4000-c000-000000000001"))
_USER_ID = str(uuid.UUID("d4060000-0000-4000-d000-000000000004"))
_POLICY_TEXT = b"Access review policy v2. Approved by the CISO on 2026-03-01."


def _client(db_connection, tenant_id, role: str = "compliance_lead") -> TestClient:
    app = create_app()

    def _conn():
        yield db_connection

    app.dependency_overrides[get_conn] = _conn
    app.dependency_overrides[get_tenant_id] = lambda: str(tenant_id)
    app.dependency_overrides[get_reviewer_id] = lambda: _USER_ID
    app.dependency_overrides[get_role] = lambda: role
    return TestClient(app)


def _upload(client, content: bytes = _POLICY_TEXT, filename: str = "policy.txt"):
    return client.post(
        "/api/v1/evidence",
        files={"file": (filename, content, "text/plain")},
        data={"record_type": "policy", "title": "Access review policy"},
    )


@pytest.fixture
def ker406_control(db_connection, tenant_a_id):
    """Seed the catalogue control the link tests attach evidence to.

    Teardown removes this control's links BEFORE the control itself: they hold
    an FK to compliance_controls, and they need tenant context to be visible at
    all (their RLS policy reaches through the record's tenant_id).
    """
    with db_connection.transaction():
        db_connection.execute(
            """INSERT INTO compliance_controls
               (control_id, framework, control_ref, category, title,
                obligation_text, entity_types, is_active)
               VALUES (%s, 'NIS2', 'KER406-TEST', 'governance', 'Intake test control',
                       'Test obligation.', %s, TRUE)
               ON CONFLICT (control_id) DO NOTHING""",
            [_CONTROL_UUID, ["essential"]],
        )
    yield
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        db_connection.execute(
            "DELETE FROM control_evidence_links WHERE control_id = %s", [_CONTROL_UUID]
        )
    with db_connection.transaction():
        db_connection.execute(
            "DELETE FROM compliance_controls WHERE control_id = %s", [_CONTROL_UUID]
        )


@pytest.mark.integration
def test_upload_creates_record_and_dedupes_on_reupload(db_connection, tenant_a_id):
    client = _client(db_connection, tenant_a_id)

    first = _upload(client)
    assert first.status_code == 201
    body = first.json()
    assert body["deduplicated"] is False
    assert body["source_system"] == "upload"
    assert len(body["content_hash"]) == 64
    record_id = body["record_id"]

    # AC-2: the same content again returns the SAME record, not a twin.
    second = _upload(client)
    assert second.status_code == 201
    assert second.json()["deduplicated"] is True
    assert second.json()["record_id"] == record_id

    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        count = db_connection.execute(
            "SELECT count(*) FROM context_records WHERE tenant_id = %s AND source_system = 'upload'",
            [str(tenant_a_id)],
        ).fetchone()[0]
    assert count == 1, "re-upload must not create a second row"


@pytest.mark.integration
def test_extracted_text_is_stored_and_original_bytes_are_not(db_connection, tenant_a_id):
    client = _client(db_connection, tenant_a_id)
    record_id = _upload(client).json()["record_id"]

    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        body, embedding = db_connection.execute(
            "SELECT body, embedding FROM context_records WHERE record_id = %s", [record_id]
        ).fetchone()
    assert "Approved by the CISO" in body
    # §16 decision 4: embedding stays NULL at upload; a backfill can walk these.
    assert embedding is None


@pytest.mark.integration
def test_link_lifecycle_and_linked_filters(db_connection, tenant_a_id, ker406_control):
    client = _client(db_connection, tenant_a_id)
    record_id = _upload(client).json()["record_id"]

    # AC-3: a fresh upload is an orphan and shows under ?linked=false.
    assert record_id in [i["record_id"] for i in client.get("/api/v1/evidence?linked=false").json()["items"]]

    linked = client.post(
        f"/api/v1/evidence/{record_id}/links",
        json={"control_id": _CONTROL_UUID, "relevance_score": 0.8, "note": "policy covers it"},
    )
    assert linked.status_code == 201
    # AC-6: linked_by is the verified JWT user, never a free string.
    assert linked.json()["linked_by"] == _USER_ID
    assert linked.json()["relevance_score"] == 0.8

    listed = client.get("/api/v1/evidence?linked=true").json()["items"]
    assert [i for i in listed if i["record_id"] == record_id][0]["link_count"] == 1

    # AC-5: unlink is a SOFT delete — the row survives with removed_at set.
    assert client.delete(f"/api/v1/evidence/{record_id}/links/{_CONTROL_UUID}").status_code == 204
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        removed_at, linked_by = db_connection.execute(
            "SELECT removed_at, linked_by FROM control_evidence_links WHERE record_id = %s",
            [record_id],
        ).fetchone()
    assert removed_at is not None, "history must survive an unlink"
    assert linked_by == _USER_ID
    assert record_id in [i["record_id"] for i in client.get("/api/v1/evidence?linked=false").json()["items"]]


@pytest.mark.integration
def test_relinking_updates_rather_than_duplicating(db_connection, tenant_a_id, ker406_control):
    client = _client(db_connection, tenant_a_id)
    record_id = _upload(client).json()["record_id"]
    client.post(f"/api/v1/evidence/{record_id}/links",
                json={"control_id": _CONTROL_UUID, "relevance_score": 0.4})
    again = client.post(f"/api/v1/evidence/{record_id}/links",
                        json={"control_id": _CONTROL_UUID, "relevance_score": 0.9})
    assert again.status_code == 201
    assert again.json()["relevance_score"] == 0.9

    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        count = db_connection.execute(
            "SELECT count(*) FROM control_evidence_links WHERE record_id = %s", [record_id]
        ).fetchone()[0]
    assert count == 1, "uq_control_evidence_links_pair means upsert, never duplicate"


@pytest.mark.integration
def test_bad_ids_return_identical_404s(db_connection, tenant_a_id, ker406_control):
    client = _client(db_connection, tenant_a_id)
    record_id = _upload(client).json()["record_id"]

    unknown_control = client.post(
        f"/api/v1/evidence/{record_id}/links", json={"control_id": str(uuid.uuid4())}
    )
    unknown_record = client.post(
        f"/api/v1/evidence/{uuid.uuid4()}/links", json={"control_id": _CONTROL_UUID}
    )
    assert unknown_control.status_code == 404
    assert unknown_record.status_code == 404
    # AC-4: no existence oracle — the two misses are indistinguishable.
    assert unknown_control.json() == unknown_record.json()


@pytest.mark.integration
def test_auditor_can_list_but_not_upload_or_link(db_connection, tenant_a_id, ker406_control):
    writer = _client(db_connection, tenant_a_id)
    record_id = _upload(writer).json()["record_id"]

    auditor = _client(db_connection, tenant_a_id, role="auditor")
    assert auditor.get("/api/v1/evidence").status_code == 200, "auditors must see evidence"
    assert auditor.post(
        "/api/v1/evidence",
        files={"file": ("x.txt", b"content", "text/plain")},
        data={"record_type": "policy"},
    ).status_code == 403
    assert auditor.post(
        f"/api/v1/evidence/{record_id}/links", json={"control_id": _CONTROL_UUID}
    ).status_code == 403


@pytest.mark.integration
def test_unsupported_file_type_is_rejected_with_no_write(db_connection, tenant_a_id):
    client = _client(db_connection, tenant_a_id)
    response = client.post(
        "/api/v1/evidence",
        files={"file": ("scan.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        data={"record_type": "policy"},
    )
    assert response.status_code == 422
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        count = db_connection.execute(
            "SELECT count(*) FROM context_records WHERE tenant_id = %s", [str(tenant_a_id)]
        ).fetchone()[0]
    assert count == 0
