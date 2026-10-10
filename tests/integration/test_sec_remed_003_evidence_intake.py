"""SEC-REMED-003 integration tests — bounded evidence intake against the approved test database.

What:  uploads through the real route, with the endpoint's connection bound to
       the live kerno_test connection. Valid text and PDF uploads still store a
       record and its KER-107 ledger entry. Every intake refusal, at the file
       limit, the body limit, the part counts and the media type, leaves no
       record and no ledger entry behind.
Why:   §11 live-database rule: SEC-REMED-003 moved where the upload path
       leases its connection (after intake, not before), so mocks alone cannot
       show the write path still runs.
How:   pytest tests/integration/test_sec_remed_003_evidence_intake.py -m integration -v
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

import src.api.evidence_upload_intake as intake
import src.services.evidence_intake as evidence_intake_service
from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
from src.api.routers.overrides import get_reviewer_id
from tests.unit.services.test_evidence_intake import _minimal_pdf

_USER_ID = str(uuid.UUID("d4060000-0000-4000-d000-000000000003"))
_SMALL_FILE_LIMIT = 64
_SMALL_BODY_LIMIT = 1024


def _client(db_connection, tenant_id) -> TestClient:
    app = create_app()

    def _conn():
        yield db_connection

    app.dependency_overrides[get_conn] = _conn
    app.dependency_overrides[get_tenant_id] = lambda: str(tenant_id)
    app.dependency_overrides[get_reviewer_id] = lambda: _USER_ID
    app.dependency_overrides[get_role] = lambda: "compliance_lead"
    return TestClient(app)


def _tenant_counts(db_connection, tenant_id) -> tuple[int, int]:
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        records = db_connection.execute(
            "SELECT count(*) FROM context_records WHERE tenant_id = %s", [str(tenant_id)]
        ).fetchone()[0]
        uploads = db_connection.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND action_type = 'evidence_uploaded'",
            [str(tenant_id)],
        ).fetchone()[0]
    return records, uploads


def _ledger_entry_for(db_connection, tenant_id, record_id: str):
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return db_connection.execute(
            "SELECT actor_id, object_type FROM audit_log "
            "WHERE tenant_id = %s AND action_type = 'evidence_uploaded' AND object_id = %s",
            [str(tenant_id), record_id],
        ).fetchone()


@pytest.fixture
def small_limits(monkeypatch) -> None:
    monkeypatch.setattr(intake, "MAX_EVIDENCE_UPLOAD_BYTES", _SMALL_FILE_LIMIT)
    monkeypatch.setattr(intake, "EVIDENCE_UPLOAD_MAX_BODY_BYTES", _SMALL_BODY_LIMIT)
    monkeypatch.setattr(evidence_intake_service, "MAX_EVIDENCE_UPLOAD_BYTES", _SMALL_FILE_LIMIT)


@pytest.mark.integration
def test_a_small_pdf_upload_is_stored_with_its_ledger_entry(db_connection, tenant_a_id):
    client = _client(db_connection, tenant_a_id)
    response = client.post(
        "/api/v1/evidence",
        files={"file": ("isms.pdf", _minimal_pdf("Board approved ISMS policy"), "application/pdf")},
        data={"record_type": "policy"},
    )
    assert response.status_code == 201
    record_id = response.json()["record_id"]
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        body = db_connection.execute(
            "SELECT body FROM context_records WHERE record_id = %s", [record_id]
        ).fetchone()[0]
    assert "Board approved ISMS policy" in body
    assert _ledger_entry_for(db_connection, tenant_a_id, record_id) == (_USER_ID, "context_record")


@pytest.mark.integration
def test_a_text_file_of_exactly_the_limit_is_stored(db_connection, tenant_a_id, small_limits):
    client = _client(db_connection, tenant_a_id)
    content = b"a" * _SMALL_FILE_LIMIT
    response = client.post(
        "/api/v1/evidence",
        files={"file": ("limit.txt", content, "text/plain")},
        data={"record_type": "policy", "title": "Exactly at the limit"},
    )
    assert response.status_code == 201
    assert _tenant_counts(db_connection, tenant_a_id) == (1, 1)


@pytest.mark.integration
@pytest.mark.parametrize(("files", "data", "status"), [
    ([("file", ("over.txt", b"a" * (_SMALL_FILE_LIMIT + 1), "text/plain"))], {"record_type": "policy"}, 413),
    ([("file", ("huge.txt", b"a" * (_SMALL_BODY_LIMIT + 1), "text/plain"))], {"record_type": "policy"}, 413),
    ([("file", ("one.txt", b"one", "text/plain")), ("file", ("two.txt", b"two", "text/plain"))],
     {"record_type": "policy"}, 400),
    ([("file", ("x.txt", b"evidence", "text/plain"))], {"record_type": "policy", "colour": "red"}, 422),
], ids=["file-one-byte-over", "body-over", "two-files", "unexpected-field"])
def test_a_refused_upload_writes_no_record_and_no_ledger_entry(
    db_connection, tenant_a_id, small_limits, files, data, status,
):
    client = _client(db_connection, tenant_a_id)
    response = client.post("/api/v1/evidence", files=files, data=data)
    assert response.status_code == status
    assert _tenant_counts(db_connection, tenant_a_id) == (0, 0)


@pytest.mark.integration
def test_a_non_multipart_upload_writes_nothing(db_connection, tenant_a_id):
    client = _client(db_connection, tenant_a_id)
    response = client.post("/api/v1/evidence", json={"file": "not an upload", "record_type": "policy"})
    assert response.status_code == 415
    assert _tenant_counts(db_connection, tenant_a_id) == (0, 0)
