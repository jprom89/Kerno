"""Frozen DORA filing download — the package stored at Start-run, served unchanged.

A download that rebuilt from today's register would change when someone edited
a vendor. These tests prove the HTTP attachment is the TEXT column written at
record time, that a later register edit does not change it, that Start-run
again replaces it, and that a missing freeze is the same 404 as a missing run
with no ledger row.

Spies cannot prove this: the freeze is a real INSERT of canonical JSON, and
the hash an auditor would take is of those exact bytes. Only a live connection
exercises that path.

Run: pytest tests/integration/test_frozen_filing_download.py -m integration -v
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
from src.api.routers.overrides import get_reviewer_id
from src.services.audit_log import get_entries_by_actor
from src.services.dora_roi_submission_service import create_submission_run

_USER_ID = str(uuid.UUID("d4180000-0000-4000-d000-000000000004"))
_WINDOW_ID = str(uuid.UUID("c0000000-0000-4000-c000-000000000018"))
_REPORTING_YEAR = 2032


def _client(db_connection, tenant_id, role: str = "compliance_lead") -> TestClient:
    app = create_app()

    def _conn():
        yield db_connection

    app.dependency_overrides[get_conn] = _conn
    app.dependency_overrides[get_tenant_id] = lambda: str(tenant_id)
    app.dependency_overrides[get_reviewer_id] = lambda: _USER_ID
    app.dependency_overrides[get_role] = lambda: role
    return TestClient(app)


def _entry_body(provider_name: str) -> dict:
    return {
        "provider_name": provider_name,
        "service_name": "Hosting",
        "provider_type": "cloud",
        "criticality_level": "critical",
        "business_function": "Platform",
        "data_types": ["operational"],
        "countries_supported": ["DE"],
    }


def _ledger(db_connection, tenant_id) -> list:
    """Return this actor's ledger entries, including control_id=None events."""
    with db_connection.transaction():
        return get_entries_by_actor(db_connection, tenant_id, _USER_ID)


def _audit_row_count(db_connection, tenant_id) -> int:
    """Return the tenant's total audit_log row count, regardless of actor or action."""
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return db_connection.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id = %s", [str(tenant_id)]
        ).fetchone()[0]


def _stored_freeze(db_connection, tenant_id, run_id: str) -> str | None:
    """Return frozen_package_json for this run, or None."""
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        row = db_connection.execute(
            "SELECT frozen_package_json FROM dora_submission_runs WHERE id = %s",
            [run_id],
        ).fetchone()
    return None if row is None else row[0]


@pytest.fixture
def filing_window(db_connection, tenant_a_id):
    """Seed one submission window for the freeze tests, and remove it afterwards."""
    with db_connection.transaction():
        db_connection.execute(
            """
            INSERT INTO dora_submission_windows
                (id, authority_code, reporting_year, register_reference_date,
                 window_open_date, window_close_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [_WINDOW_ID, "EBA", _REPORTING_YEAR, date(2032, 3, 31),
             date(2032, 4, 1), date(2032, 4, 30)],
        )
    yield _WINDOW_ID
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        db_connection.execute(
            "DELETE FROM dora_submission_runs WHERE submission_window_id = %s", [_WINDOW_ID]
        )
        db_connection.execute("DELETE FROM dora_submission_windows WHERE id = %s", [_WINDOW_ID])


@pytest.mark.integration
def test_download_bytes_equal_the_stored_text(db_connection, tenant_a_id, filing_window):
    client = _client(db_connection, tenant_a_id)
    created = client.post(
        "/api/v1/submissions/runs", json={"submission_window_id": filing_window}
    )
    assert created.status_code == 200
    run_id = created.json()["id"]
    stored = _stored_freeze(db_connection, tenant_a_id, run_id)
    assert stored is not None

    response = client.get(f"/api/v1/submissions/runs/{run_id}/package")
    assert response.status_code == 200
    assert response.content.decode("utf-8") == stored
    assert "attachment" in response.headers["content-disposition"]

    downloaded = [
        e for e in _ledger(db_connection, tenant_a_id)
        if e.action_type == "filing_package_downloaded"
    ]
    assert len(downloaded) == 1
    assert downloaded[0].object_type == "submission_run"
    assert downloaded[0].object_id == run_id
    assert downloaded[0].actor_id == _USER_ID
    assert downloaded[0].control_id is None
    assert downloaded[0].after_state["run_id"] == run_id
    assert downloaded[0].after_state["reporting_year"] == _REPORTING_YEAR
    assert "package_json" not in downloaded[0].after_state


@pytest.mark.integration
def test_a_register_edit_does_not_change_the_frozen_download(
    db_connection, tenant_a_id, filing_window
):
    client = _client(db_connection, tenant_a_id)
    created_entry = client.post(
        "/api/v1/register/entries", json=_entry_body("Before Ltd")
    )
    assert created_entry.status_code == 201
    entry_id = created_entry.json()["register_entry_id"]

    run = client.post(
        "/api/v1/submissions/runs", json={"submission_window_id": filing_window}
    )
    assert run.status_code == 200
    run_id = run.json()["id"]
    frozen_before = client.get(f"/api/v1/submissions/runs/{run_id}/package")
    assert frozen_before.status_code == 200
    assert b"Before Ltd" in frozen_before.content

    amended = _entry_body("After Ltd")
    patch = client.patch(f"/api/v1/register/entries/{entry_id}", json=amended)
    assert patch.status_code == 200

    frozen_after = client.get(f"/api/v1/submissions/runs/{run_id}/package")
    assert frozen_after.status_code == 200
    assert frozen_after.content == frozen_before.content
    assert b"After Ltd" not in frozen_after.content


@pytest.mark.integration
def test_starting_the_run_again_replaces_the_freeze(
    db_connection, tenant_a_id, filing_window
):
    client = _client(db_connection, tenant_a_id)
    created_entry = client.post(
        "/api/v1/register/entries", json=_entry_body("First Ltd")
    )
    assert created_entry.status_code == 201
    entry_id = created_entry.json()["register_entry_id"]

    first = client.post(
        "/api/v1/submissions/runs", json={"submission_window_id": filing_window}
    )
    assert first.status_code == 200
    run_id = first.json()["id"]
    first_bytes = client.get(f"/api/v1/submissions/runs/{run_id}/package").content
    assert b"First Ltd" in first_bytes

    amended = _entry_body("Second Ltd")
    assert client.patch(f"/api/v1/register/entries/{entry_id}", json=amended).status_code == 200

    second = client.post(
        "/api/v1/submissions/runs", json={"submission_window_id": filing_window}
    )
    assert second.status_code == 200
    assert second.json()["id"] == run_id
    second_bytes = client.get(f"/api/v1/submissions/runs/{run_id}/package").content
    assert second_bytes != first_bytes
    assert b"Second Ltd" in second_bytes
    assert second_bytes.decode("utf-8") == _stored_freeze(db_connection, tenant_a_id, run_id)


@pytest.mark.integration
def test_unknown_run_is_a_404_and_writes_no_ledger(db_connection, tenant_a_id):
    unknown_run = str(uuid.uuid4())
    audit_before = _audit_row_count(db_connection, tenant_a_id)
    client = _client(db_connection, tenant_a_id)
    response = client.get(f"/api/v1/submissions/runs/{unknown_run}/package")
    assert response.status_code == 404
    assert response.json() == {"detail": "entry not found"}
    assert _audit_row_count(db_connection, tenant_a_id) == audit_before


@pytest.mark.integration
def test_malformed_run_id_is_a_404_and_writes_no_ledger(db_connection, tenant_a_id):
    audit_before = _audit_row_count(db_connection, tenant_a_id)
    client = _client(db_connection, tenant_a_id)
    response = client.get("/api/v1/submissions/runs/not-a-uuid/package")
    assert response.status_code == 404
    assert response.json() == {"detail": "entry not found"}
    assert _audit_row_count(db_connection, tenant_a_id) == audit_before


@pytest.mark.integration
def test_auditor_download_is_403(db_connection, tenant_a_id, filing_window):
    lead = _client(db_connection, tenant_a_id, role="compliance_lead")
    created = lead.post(
        "/api/v1/submissions/runs", json={"submission_window_id": filing_window}
    )
    assert created.status_code == 200
    run_id = created.json()["id"]

    auditor = _client(db_connection, tenant_a_id, role="auditor")
    audit_before = _audit_row_count(db_connection, tenant_a_id)
    response = auditor.get(f"/api/v1/submissions/runs/{run_id}/package")
    assert response.status_code == 403
    assert _audit_row_count(db_connection, tenant_a_id) == audit_before


@pytest.mark.integration
def test_draft_without_a_package_is_a_404_and_writes_no_ledger(
    db_connection, tenant_a_id, filing_window
):
    with db_connection.transaction():
        draft = create_submission_run(db_connection, str(tenant_a_id), filing_window)
    assert _stored_freeze(db_connection, tenant_a_id, draft.id) is None

    audit_before = _audit_row_count(db_connection, tenant_a_id)
    client = _client(db_connection, tenant_a_id)
    response = client.get(f"/api/v1/submissions/runs/{draft.id}/package")
    assert response.status_code == 404
    assert response.json() == {"detail": "entry not found"}
    assert _audit_row_count(db_connection, tenant_a_id) == audit_before
