"""test_pilot_ready_004_upload_limits.py — bounded PDF uploads on the runtime login, in the full and the pilot app.

What:  uploads real PDFs through the real application, with real JWTs from
       its own login, on a connection pool opened as the non-owner runtime
       login kerno_test_app (the runtime_pool fixture), so the PDF is read by
       the real bounded worker process and stored as the runtime role. In the
       full app: a PDF is stored once with its stripped text, the SHA-256 of
       that text and the signed-in actor, with exactly one evidence_uploaded
       ledger entry, and a re-upload of the same document is deduplicated
       with no second row and no second entry; a PDF over the per-stream
       limit and a malformed PDF are refused with their exact details,
       leaving no context_records row, no audit_log entry and no runtime
       lease. In the pilot app (pilot_profile, after the real lifespan has
       verified the runtime identity and its pilot assignment): the same
       stored-once upload and one refusal without a trace.
Why:   §11 live-database rule for PILOT-READY-004 (gate G6): extraction moved
       into a separate worker process and before the connection lease, and
       the upload is now admitted and deadline-bound, so only a live run on
       the runtime identity shows the stored row, its hash, its actor and its
       ledger entry are still right, and that a refused upload touches
       nothing. Owner reads and chain verification stay on db_connection and
       are never runtime evidence.
How:   Checkpoint B. Full profile:
       python -m pytest tests/integration/test_pilot_ready_004_upload_limits.py --require-live-database --require-runtime-identity
       Pilot profile, after the guarded switch to the pilot grants:
       python -m pytest tests/integration/test_pilot_ready_004_upload_limits.py --require-live-database --require-runtime-identity --runtime-profile pilot
       Without test-database settings the full-profile tests skip by name
       and the pilot_profile test is deselected, like every other live test.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from config.constants import PDF_MAX_STREAM_OUTPUT_BYTES
from src.api import dependencies
from src.api.app import create_app
from src.api.rate_limit import limiter
from tests.conftest import TENANT_A_ID, TENANT_B_ID
from tests.integration import test_c2_runtime_application as c2
from tests.integration.test_runtime_serving_profile_startup import (
    RUNTIME_ROLE,
    pin_startup_environment,
    record_startup_reads,
)
from tests.unit.services._pdf_documents import (
    build_pdf,
    build_pdf_with_deflated_page,
    deflated_text_with_padding,
    reference_text,
    text_operations,
)

_PDF_FILENAME = "isms-policy.pdf"
_PDF_COPY_FILENAME = "isms-policy-copy.pdf"
_PDF_LINES = ("Board approved ISMS policy", "Access is reviewed every quarter by the CISO")
_ANNEX_LINE = "Annex A (Zürich data centre)"
_UNREADABLE = "the PDF could not be read"
_OVER_LIMITS = "the PDF exceeds the text-extraction limits"
_MALFORMED_PDF = b"%PDF-1.4 this is not a real pdf"


@pytest.fixture
def full_api(runtime_pool, db_connection, monkeypatch):
    """Yield a TestClient on a fresh full app whose every request leases from the runtime pool.

    No dependency is overridden, so uploads carry the JWTs the application's
    own login issues. Seeds, as the owner, the C2 module's slugs, logins and
    catalogue controls, and removes them afterwards; the shared teardown
    removes the tenants' records and ledger entries.
    """
    assert dependencies._get_pool() is runtime_pool, "requests must lease from the runtime pool"
    monkeypatch.delenv("KERNO_LLM_MODEL", raising=False)
    limiter.reset()
    c2._seed_logins_and_controls(db_connection)
    try:
        yield TestClient(create_app())
    finally:
        limiter.reset()
        c2._remove_seeded_rows(db_connection)


@pytest.fixture
def pilot_startup_reads(runtime_pool, monkeypatch):
    """Record what the real lifespan reads from the database; set up before the pilot app is entered."""
    return record_startup_reads(monkeypatch)


@pytest.fixture
def pilot_api(runtime_pool, db_connection, monkeypatch, pilot_startup_reads):
    """Yield a TestClient inside the real lifespan of a fresh pilot app on the runtime pool.

    The environment is pinned to a non-development host without the owner
    URL, so the lifespan's identity and profile check is the one a deployed
    pilot runs. Seeds and removes the C2 logins and controls like full_api.
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


def _policy_pdf() -> bytes:
    """Return a real three-page PDF: two lines of text, an empty page, and a non-ASCII line."""
    return build_pdf([text_operations(*_PDF_LINES), None, text_operations(_ANNEX_LINE)])


def _over_limit_pdf() -> bytes:
    """Return a small PDF whose one content stream decodes to one byte over PDF_MAX_STREAM_OUTPUT_BYTES."""
    padded_stream = deflated_text_with_padding(text_operations("decompression bomb"), PDF_MAX_STREAM_OUTPUT_BYTES + 1)
    return build_pdf_with_deflated_page(padded_stream)


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


def _tenant_a_record_count(db_connection) -> int:
    """Return how many context_records Tenant A holds, read as the owner."""
    rows = c2._owner_rows(
        db_connection, TENANT_A_ID, "SELECT count(*) FROM context_records WHERE tenant_id = %s", [str(TENANT_A_ID)]
    )
    return rows[0][0]


def _assert_pdf_stored_once_and_deduplicated(client, db_connection) -> None:
    """Upload a real PDF twice as Tenant A's lead: one row with the right text, hash and actor, one ledger entry."""
    headers = c2._sign_in(client, c2._LEAD_A)
    pdf = _policy_pdf()
    expected_text = reference_text(pdf).strip()
    expected_hash = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()

    first = c2._upload(client, headers, _PDF_FILENAME, pdf, "application/pdf")
    assert first.status_code == 201, first.text
    record_id = first.json()["record_id"]
    assert (first.json()["deduplicated"], first.json()["content_hash"]) == (False, expected_hash)
    stored = c2._owner_rows(
        db_connection, TENANT_A_ID,
        "SELECT body, content_hash, source_system, external_id, record_type FROM context_records "
        "WHERE record_id = %s",
        [record_id],
    )
    assert stored == [(expected_text, expected_hash, "upload", _PDF_FILENAME, "policy")]

    again = c2._upload(client, headers, _PDF_COPY_FILENAME, pdf, "application/pdf")
    assert again.status_code == 201, again.text
    assert (again.json()["deduplicated"], again.json()["record_id"]) == (True, record_id)
    assert _tenant_a_record_count(db_connection) == 1

    rows = c2._ledger(db_connection, TENANT_A_ID)
    assert [row[:4] for row in rows] == [("evidence_uploaded", "context_record", record_id, c2._LEAD_A.user_id)]
    assert rows[0][5] == {"external_id": _PDF_FILENAME, "record_type": "policy"}
    assert c2._ledger(db_connection, TENANT_B_ID) == []
    c2._assert_ledger_chains_valid(db_connection)


def _assert_refused_without_a_trace(client, db_connection, pool, monkeypatch, refusals: list) -> None:
    """Upload each (filename, PDF, detail) as Tenant A's lead and assert each is a 422 with its detail.

    Nothing may be written (no record, no ledger entry) and no runtime
    connection may be leased by any of them.
    """
    headers = c2._sign_in(client, c2._LEAD_A)
    leases = _count_leases(monkeypatch, pool)
    for filename, pdf, detail in refusals:
        response = c2._upload(client, headers, filename, pdf, "application/pdf")
        assert (response.status_code, response.json()) == (422, {"detail": detail}), response.text
    assert leases == [], f"{len(leases)} runtime connection(s) leased by refused uploads"
    assert _tenant_a_record_count(db_connection) == 0
    assert c2._ledger(db_connection, TENANT_A_ID) == []
    c2._assert_ledger_chains_valid(db_connection)


# ── Full profile ─────────────────────────────────────────────────────────────


def test_a_real_pdf_is_stored_once_on_the_runtime_login_with_its_text_hash_actor_and_ledger_entry(
    full_api, db_connection,
):
    _assert_pdf_stored_once_and_deduplicated(full_api, db_connection)


def test_an_over_limit_and_a_malformed_pdf_leave_no_row_no_ledger_entry_and_no_lease(
    full_api, db_connection, runtime_pool, monkeypatch,
):
    refusals = [("bomb.pdf", _over_limit_pdf(), _OVER_LIMITS), ("broken.pdf", _MALFORMED_PDF, _UNREADABLE)]
    _assert_refused_without_a_trace(full_api, db_connection, runtime_pool, monkeypatch, refusals)


# ── Pilot profile ────────────────────────────────────────────────────────────


@pytest.mark.pilot_profile
def test_the_pilot_app_stores_a_real_pdf_once_and_refuses_an_over_limit_one_without_a_trace(
    pilot_api, db_connection, runtime_pool, monkeypatch, pilot_startup_reads,
):
    assert pilot_api.app.state.serving_profile == "pilot"
    assert pilot_startup_reads == {"identity": [RUNTIME_ROLE], "profile": ["pilot"]}
    _assert_refused_without_a_trace(
        pilot_api, db_connection, runtime_pool, monkeypatch, [("bomb.pdf", _over_limit_pdf(), _OVER_LIMITS)],
    )
    _assert_pdf_stored_once_and_deduplicated(pilot_api, db_connection)
