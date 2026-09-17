"""Unit tests for the evidence router's failure mapping (KER-406).

Deliberately narrow: the endpoints' behaviour is proven end-to-end against a
real database in tests/integration/test_ker406_evidence_intake.py, and
re-asserting it here against mocks would be the exact antipattern the §11
live-database rule exists to prevent. This file covers only what integration
cannot cover cheaply — the oversize path, which would need a real 10 MB upload.

How to run
----------
    pytest tests/unit/api/test_evidence.py -v
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from config.constants import MAX_EVIDENCE_UPLOAD_BYTES
from src.api.app import create_app
from src.api.dependencies import get_conn, get_role, get_tenant_id
from src.api.routers.overrides import get_reviewer_id

os.environ.setdefault("KERNO_JWT_SECRET", "test-secret-for-unit-tests")

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_USER_ID = "d0000000-0000-4000-d000-000000000004"


def _app(role: str = "compliance_lead") -> TestClient:
    app = create_app()

    def _conn():
        yield MagicMock()

    app.dependency_overrides[get_conn] = _conn
    app.dependency_overrides[get_tenant_id] = lambda: _TENANT_ID
    app.dependency_overrides[get_reviewer_id] = lambda: _USER_ID
    app.dependency_overrides[get_role] = lambda: role
    return TestClient(app)


def test_oversize_upload_returns_413_before_touching_the_database():
    oversize = b"x" * (MAX_EVIDENCE_UPLOAD_BYTES + 1)
    response = _app().post(
        "/api/v1/evidence",
        files={"file": ("huge.txt", oversize, "text/plain")},
        data={"record_type": "policy"},
    )
    # 413 is the honest status for a payload refused on size alone — not a 422,
    # which would imply the content was wrong rather than simply too large.
    assert response.status_code == 413


def test_missing_record_type_is_a_422():
    response = _app().post(
        "/api/v1/evidence", files={"file": ("policy.txt", b"content", "text/plain")}
    )
    assert response.status_code == 422
