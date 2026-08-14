"""KER-409 integration tests — register and submission writes against a live database.

Every DORA register row is a line in a filed regulatory artefact, and until now
nothing recorded who put it there. These tests prove the ledger entry is written
on the same connection and in the same transaction as the row it describes, so
neither can exist without the other, and that an unknown submission window is a
404 rather than a correlation ID.

Spies cannot prove any of this: the ledger append takes an advisory lock, reads
the tenant's previous entry_hash, and inserts through an append-only trigger and
FORCE RLS. Only a real connection exercises that chain.

Run: pytest tests/integration/test_ker409_register_ledger.py -m integration -v
"""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
from src.api.routers.overrides import get_reviewer_id
from src.services.audit_log import get_entries_by_actor

_USER_ID = str(uuid.UUID("d4090000-0000-4000-d000-000000000004"))
_WINDOW_ID = str(uuid.UUID("a4090000-0000-4000-a000-000000000001"))
_REPORTING_YEAR = 2031


class _DeliberateRollback(Exception):
    pass


def _client(db_connection, tenant_id, role: str = "compliance_lead") -> TestClient:
    app = create_app()

    def _conn():
        yield db_connection

    app.dependency_overrides[get_conn] = _conn
    app.dependency_overrides[get_tenant_id] = lambda: str(tenant_id)
    app.dependency_overrides[get_reviewer_id] = lambda: _USER_ID
    app.dependency_overrides[get_role] = lambda: role
    return TestClient(app)


def _entry_body(provider_name: str = "Acme Cloud") -> dict:
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
    """Return this actor's ledger entries.

    Queried by actor rather than by control: these events carry control_id=None,
    so get_entries_by_control would never see them.
    """
    with db_connection.transaction():
        return get_entries_by_actor(db_connection, tenant_id, _USER_ID)


def _audit_row_count(db_connection, tenant_id) -> int:
    """Return the tenant's total audit_log row count, regardless of actor or action.

    Filtering by action_type would only prove that no entry of the expected kind
    was written; this proves no entry at all was.
    """
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return db_connection.execute(
            "SELECT count(*) FROM audit_log WHERE tenant_id = %s", [str(tenant_id)]
        ).fetchone()[0]


@pytest.fixture
def ker409_window(db_connection, tenant_a_id):
    """Seed one submission window for the run tests, and remove it afterwards.

    dora_submission_windows is global reference data — no tenant_id, no RLS —
    so seeding it is a plain insert. Clearing the runs it produced is not:
    dora_submission_runs is FORCE RLS, so the delete needs a tenant context, and
    a pooled connection comes back with app.current_tenant_id set to the empty
    string rather than unset, which fails the policy's uuid cast.
    """
    with db_connection.transaction():
        db_connection.execute(
            """
            INSERT INTO dora_submission_windows
                (id, authority_code, reporting_year, register_reference_date,
                 window_open_date, window_close_date)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            [_WINDOW_ID, "EBA", _REPORTING_YEAR, date(2031, 3, 31),
             date(2031, 4, 1), date(2031, 4, 30)],
        )
    yield _WINDOW_ID
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        db_connection.execute(
            "DELETE FROM dora_submission_runs WHERE submission_window_id = %s", [_WINDOW_ID]
        )
        db_connection.execute("DELETE FROM dora_submission_windows WHERE id = %s", [_WINDOW_ID])


@pytest.mark.integration
def test_creating_a_register_entry_writes_a_ledger_entry(db_connection, tenant_a_id):
    client = _client(db_connection, tenant_a_id)
    response = client.post("/api/v1/register/entries", json=_entry_body())
    assert response.status_code == 201
    entry_id = response.json()["register_entry_id"]

    entries = _ledger(db_connection, tenant_a_id)
    created = [e for e in entries if e.action_type == "register_entry_created"]
    assert len(created) == 1
    assert created[0].object_type == "register_entry"
    assert created[0].object_id == entry_id
    assert created[0].actor_id == _USER_ID
    assert created[0].control_id is None
    assert created[0].before_state is None
    assert created[0].after_state["provider_name"] == "Acme Cloud"

    # JSON-safety, asserted rather than assumed: dataclasses.asdict leaves date
    # and datetime objects as objects, so the service isoformats them explicitly.
    # Read back from the database, every value is already a string, and the whole
    # state round-trips through json.dumps without a custom encoder.
    assert isinstance(created[0].after_state["created_at"], str)
    assert created[0].after_state["created_at"].startswith("20")
    json.dumps(created[0].after_state)


@pytest.mark.integration
def test_a_rolled_back_create_leaves_neither_the_row_nor_the_ledger_entry(
    db_connection, tenant_a_id
):
    # The invariant that matters: a register row can never exist without the
    # record of who added it, and the reverse must hold too.
    from src.services.dora_roi_service import RegisterEntryInput, create_register_entry

    captured: dict = {}
    with pytest.raises(_DeliberateRollback):
        with db_connection.transaction():
            created = create_register_entry(
                db_connection,
                tenant_a_id,
                RegisterEntryInput(
                    **_entry_body(provider_name="Rollback Ltd"),
                    contract_start_date=None,
                    contract_end_date=None,
                    exit_strategy_summary=None,
                    is_active=True,
                    source_record_id=None,
                ),
                actor_id=_USER_ID,
                actor_role="vciso",
            )
            captured["entry_id"] = created.register_entry_id
            raise _DeliberateRollback()

    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        rows = db_connection.execute(
            "SELECT register_entry_id FROM dora_register_entries WHERE register_entry_id = %s",
            [captured["entry_id"]],
        ).fetchall()
    assert rows == []
    assert [e for e in _ledger(db_connection, tenant_a_id) if e.object_id == captured["entry_id"]] == []


@pytest.mark.integration
def test_updating_a_register_entry_records_the_previous_row(db_connection, tenant_a_id):
    client = _client(db_connection, tenant_a_id)
    created = client.post("/api/v1/register/entries", json=_entry_body(provider_name="Before Ltd"))
    entry_id = created.json()["register_entry_id"]

    amended = _entry_body(provider_name="After Ltd")
    amended["criticality_level"] = "standard"
    response = client.patch(f"/api/v1/register/entries/{entry_id}", json=amended)
    assert response.status_code == 200

    updates = [
        e for e in _ledger(db_connection, tenant_a_id)
        if e.action_type == "register_entry_updated"
    ]
    assert len(updates) == 1
    assert updates[0].object_type == "register_entry"
    assert updates[0].object_id == entry_id
    assert updates[0].actor_id == _USER_ID
    assert updates[0].control_id is None
    # An amendment is only reconstructable if what it replaced was captured.
    assert updates[0].before_state["provider_name"] == "Before Ltd"
    assert updates[0].before_state["criticality_level"] == "critical"
    assert updates[0].after_state["provider_name"] == "After Ltd"
    assert updates[0].after_state["criticality_level"] == "standard"


@pytest.mark.integration
def test_a_submission_run_writes_a_run_row_and_a_ledger_entry(
    db_connection, tenant_a_id, ker409_window
):
    # An empty register is fine here: the run is still recorded, as a draft that
    # failed validation, and recording the attempt is the point.
    client = _client(db_connection, tenant_a_id)
    response = client.post(
        "/api/v1/submissions/runs", json={"submission_window_id": ker409_window}
    )
    assert response.status_code == 200
    run = response.json()
    assert run["submission_window_id"] == ker409_window

    # First assertion: the run row itself. An empty register fails validation
    # with the ROI_000 issue, which is why this lands as a draft rather than
    # ready — the attempt is still recorded, which is the point.
    assert run["status"] == "draft"
    assert run["validation_overall_status"] == "fail"
    assert run["entry_count"] == 0
    assert run["validation_issue_count"] >= 1
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        run_rows = db_connection.execute(
            "SELECT id, status FROM dora_submission_runs WHERE id = %s", [run["id"]]
        ).fetchall()
    assert len(run_rows) == 1
    assert run_rows[0][1] == "draft"

    # Second, independent assertion: the ledger entry. Deliberately not inferred
    # from the run row existing — the whole ticket is that one used to happen
    # without the other.
    recorded = [
        e for e in _ledger(db_connection, tenant_a_id)
        if e.action_type == "submission_run_recorded"
    ]
    assert len(recorded) == 1
    assert recorded[0].object_type == "submission_run"
    assert recorded[0].object_id == run["id"]
    assert recorded[0].actor_id == _USER_ID
    assert recorded[0].control_id is None
    assert recorded[0].before_state is None
    assert recorded[0].after_state["submission_window_id"] == ker409_window


@pytest.mark.integration
def test_an_unknown_submission_window_is_a_404_and_writes_nothing(db_connection, tenant_a_id):
    # Previously a ValueError reaching the generic handler: a 500 and a
    # correlation ID for what is simply a wrong id.
    unknown_window = str(uuid.uuid4())
    audit_rows_before = _audit_row_count(db_connection, tenant_a_id)
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        runs_before = db_connection.execute(
            "SELECT count(*) FROM dora_submission_runs WHERE tenant_id = %s",
            [str(tenant_a_id)],
        ).fetchone()[0]

    client = _client(db_connection, tenant_a_id)
    response = client.post(
        "/api/v1/submissions/runs", json={"submission_window_id": unknown_window}
    )

    # 404, not a 500 with a correlation ID an operator has to quote at support
    # for what is only a wrong identifier.
    assert response.status_code == 404
    assert "correlation_id" not in response.json()

    # The lookup fails before the export builds, before the run upserts, and
    # before any ledger append — so these are counted in total, not filtered by
    # action_type, which would only prove the expected entry was absent.
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        runs_after = db_connection.execute(
            "SELECT count(*) FROM dora_submission_runs WHERE tenant_id = %s",
            [str(tenant_a_id)],
        ).fetchone()[0]
        for_window = db_connection.execute(
            "SELECT id FROM dora_submission_runs WHERE submission_window_id = %s",
            [unknown_window],
        ).fetchall()
    assert runs_after == runs_before
    assert for_window == []
    assert _audit_row_count(db_connection, tenant_a_id) == audit_rows_before


@pytest.mark.integration
def test_patching_a_missing_entry_is_a_404_and_writes_no_ledger_row(db_connection, tenant_a_id):
    # update_register_entry returns None before it reaches the UPDATE or the
    # ledger append, so a wrong id must leave the ledger exactly as it was.
    missing_entry_id = str(uuid.uuid4())
    audit_rows_before = _audit_row_count(db_connection, tenant_a_id)

    client = _client(db_connection, tenant_a_id)
    response = client.patch(
        f"/api/v1/register/entries/{missing_entry_id}", json=_entry_body(provider_name="Ghost Ltd")
    )

    assert response.status_code == 404
    assert _audit_row_count(db_connection, tenant_a_id) == audit_rows_before
    assert [
        e for e in _ledger(db_connection, tenant_a_id) if e.object_id == missing_entry_id
    ] == []
