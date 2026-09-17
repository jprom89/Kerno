"""Unit tests for the /api/v1/submissions endpoints covering POST /runs,
GET /runs, GET /runs/{id}, and the unauthenticated GET /windows endpoint."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
# KER-409: register and submission writes attribute to the verified JWT user.
from src.api.routers.overrides import get_reviewer_id
from src.services.dora_roi_submission_service import (
    FrozenFilingPackage,
    SubmissionRunOutput,
    SubmissionWindowOutput,
)

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_ACTOR_ID = "d0000000-0000-4000-d000-000000000004"
_WINDOW_ID = "b0000000-0000-4000-b000-000000000001"
_RUN_ID = "f0000000-0000-4000-f000-000000000001"

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _override_get_conn():
    yield MagicMock()


def _make_run_output() -> SubmissionRunOutput:
    now = datetime(2025, 6, 1, tzinfo=timezone.utc)
    return SubmissionRunOutput(
        id=_RUN_ID,
        tenant_id=_TENANT_ID,
        submission_window_id=_WINDOW_ID,
        reporting_year=2025,
        status="ready",
        validation_overall_status="pass",
        validation_issue_count=0,
        entry_count=3,
        created_at=now,
        updated_at=now,
        submitted_at=None,
        submission_reference=None,
    )


def _make_window_output() -> SubmissionWindowOutput:
    now = datetime(2025, 6, 1, tzinfo=timezone.utc)
    return SubmissionWindowOutput(
        id=_WINDOW_ID,
        authority_code="MFSA",
        reporting_year=2025,
        register_reference_date=date(2025, 12, 31),
        window_open_date=date(2026, 1, 1),
        window_close_date=date(2026, 3, 31),
        created_at=now,
        updated_at=now,
    )


def _app_both_overrides():
    _app = create_app()
    _app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    _app.dependency_overrides[get_role] = lambda: "compliance_lead"
    _app.dependency_overrides[get_reviewer_id] = lambda: _ACTOR_ID
    _app.dependency_overrides[get_conn] = _override_get_conn
    return _app


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_create_run_returns_200():
    run = _make_run_output()
    with patch(
        "src.api.routers.submissions.build_and_record_submission",
        return_value=(run, MagicMock()),
    ):
        client = TestClient(_app_both_overrides())
        response = client.post("/api/v1/submissions/runs", json={"submission_window_id": _WINDOW_ID})
    assert response.status_code == 200
    assert response.json()["id"] == _RUN_ID
    assert response.json()["tenant_id"] == _TENANT_ID


def test_list_runs_returns_200():
    run = _make_run_output()
    with patch("src.api.routers.submissions.list_tenant_submission_runs", return_value=[run]):
        client = TestClient(_app_both_overrides())
        response = client.get("/api/v1/submissions/runs")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == _RUN_ID


def test_get_run_by_id_returns_200():
    run = _make_run_output()
    with patch("src.api.routers.submissions.get_submission_run", return_value=run):
        client = TestClient(_app_both_overrides())
        response = client.get(f"/api/v1/submissions/runs/{_RUN_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == _RUN_ID
    assert body["status"] == "ready"
    assert body["validation_overall_status"] == "pass"


def test_get_run_by_id_not_found_returns_404():
    with patch("src.api.routers.submissions.get_submission_run", return_value=None):
        client = TestClient(_app_both_overrides())
        response = client.get(f"/api/v1/submissions/runs/{_RUN_ID}")
    assert response.status_code == 404


def test_list_windows_requires_no_auth():
    window = _make_window_output()
    with patch("src.api.routers.submissions.list_open_windows", return_value=[window]):
        # No get_tenant_id override and no Authorization header.
        _app = create_app()
        _app.dependency_overrides[get_conn] = _override_get_conn
        client = TestClient(_app)
        response = client.get("/api/v1/submissions/windows")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert response.json()[0]["authority_code"] == "MFSA"


# ── KER-412: a bad id is a 404, never a 500 ─────────────────────────────────


def test_malformed_run_id_is_a_404_not_a_500():
    # run_id is compared against a uuid column, so a malformed value used to
    # fail the cast inside PostgreSQL and reach the generic 500 handler.
    client = TestClient(_app_both_overrides())
    response = client.get("/api/v1/submissions/runs/not-a-uuid")
    assert response.status_code == 404


def test_malformed_and_missing_run_ids_are_indistinguishable():
    client = TestClient(_app_both_overrides())
    with patch("src.api.routers.submissions.get_submission_run", return_value=None):
        missing = client.get(f"/api/v1/submissions/runs/{_RUN_ID}")
    malformed = client.get("/api/v1/submissions/runs/not-a-uuid")
    assert missing.status_code == malformed.status_code == 404
    assert missing.json() == malformed.json()


def test_malformed_window_id_on_create_is_a_404_not_a_500():
    client = TestClient(_app_both_overrides())
    response = client.post(
        "/api/v1/submissions/runs", json={"submission_window_id": "not-a-uuid"}
    )
    assert response.status_code == 404


def test_malformed_window_id_never_starts_a_run():
    # The guard runs before build_and_record_submission, so a junk window id
    # cannot build an export, write a run row, or append a ledger entry.
    with patch("src.api.routers.submissions.build_and_record_submission") as service:
        client = TestClient(_app_both_overrides())
        response = client.post(
            "/api/v1/submissions/runs", json={"submission_window_id": "not-a-uuid"}
        )
    assert response.status_code == 404
    service.assert_not_called()


def test_download_package_returns_stored_bytes_unchanged():
    filing = FrozenFilingPackage(
        run_id=_RUN_ID,
        reporting_year=2025,
        entry_count=3,
        package_json='{"entry_count":3,"reporting_year":2025}',
    )
    with patch(
        "src.api.routers.submissions.get_frozen_filing_package", return_value=filing
    ), patch(
        "src.api.routers.submissions.record_filing_package_download"
    ) as record:
        client = TestClient(_app_both_overrides())
        response = client.get(f"/api/v1/submissions/runs/{_RUN_ID}/package")
    assert response.status_code == 200
    assert response.content == b'{"entry_count":3,"reporting_year":2025}'
    assert response.headers["content-type"].startswith("application/json")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"kerno-dora-filing-2025-{_RUN_ID}.json" in disposition
    record.assert_called_once()


def test_download_missing_package_is_404_and_does_not_ledger():
    with patch(
        "src.api.routers.submissions.get_frozen_filing_package", return_value=None
    ), patch(
        "src.api.routers.submissions.record_filing_package_download"
    ) as record:
        client = TestClient(_app_both_overrides())
        response = client.get(f"/api/v1/submissions/runs/{_RUN_ID}/package")
    assert response.status_code == 404
    assert response.json() == {"detail": "entry not found"}
    record.assert_not_called()


def test_download_malformed_run_id_is_a_404_not_a_500():
    client = TestClient(_app_both_overrides())
    response = client.get("/api/v1/submissions/runs/not-a-uuid/package")
    assert response.status_code == 404
    assert response.json() == {"detail": "entry not found"}


def test_download_malformed_and_missing_are_indistinguishable():
    client = TestClient(_app_both_overrides())
    with patch(
        "src.api.routers.submissions.get_frozen_filing_package", return_value=None
    ):
        missing = client.get(f"/api/v1/submissions/runs/{_RUN_ID}/package")
    malformed = client.get("/api/v1/submissions/runs/not-a-uuid/package")
    assert missing.status_code == malformed.status_code == 404
    assert missing.json() == malformed.json()


def test_download_malformed_id_never_looks_up_the_package():
    with patch(
        "src.api.routers.submissions.get_frozen_filing_package"
    ) as lookup, patch(
        "src.api.routers.submissions.record_filing_package_download"
    ) as record:
        client = TestClient(_app_both_overrides())
        response = client.get("/api/v1/submissions/runs/not-a-uuid/package")
    assert response.status_code == 404
    lookup.assert_not_called()
    record.assert_not_called()
