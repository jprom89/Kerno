"""Unit tests for src/services/webhook_service.py (KER-205).

Covers, without a database: registration (CSPRNG secret, context-first,
column binding), rotation (new secret, tenant scoping, unknown id), the HMAC
verification gate (valid signature, tampered signature, missing/malformed
header, unknown id, inactive registration — all failing with ONE
indistinguishable error, and the registration lookup proven to be a
pre-context read), the dedup window check and upsert, and the normaliser's
four supported event types plus its unknown-type rejection.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from config.constants import WEBHOOK_DEDUP_WINDOW_HOURS
from src.exceptions import TenantContextMissingError
from src.services.webhook_service import (
    UnsupportedEventTypeError,
    WebhookAuthenticationError,
    WebhookNormaliser,
    is_duplicate,
    normalise_event,
    record_dedup,
    register_webhook,
    rotate_secret,
    verify_and_resolve_tenant,
)

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_REGISTRATION_ID = str(uuid.UUID("d0000000-0000-4000-d000-000000000005"))
_SECRET = "f" * 64
_BODY = b'{"source_system":"jira","event_type":"jira.issue.updated"}'


class _RowResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class _SpyConn:
    """Records SQL; serves one canned row for SELECT/UPDATE...RETURNING."""

    def __init__(self, row=None):
        self.calls: list[tuple[str, object]] = []
        self._row = row

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _RowResult(self._row)

    def statements(self):
        return [sql for sql, _ in self.calls]


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── register_webhook ──────────────────────────────────────────────────────────


def test_register_generates_64_hex_secret_and_binds_columns():
    spy = _SpyConn()
    record, secret = register_webhook(spy, _TENANT_ID, "jira")
    assert len(secret) == 64
    int(secret, 16)  # pure hex
    params = next(p for s, p in spy.calls if "INSERT INTO webhook_registrations" in s)
    assert params["signing_secret"] == secret
    assert params["tenant_id"] == str(_TENANT_ID)
    assert params["source_system"] == "jira"
    assert record.id == params["id"]
    assert record.is_active is True


def test_register_sets_tenant_context_first():
    spy = _SpyConn()
    register_webhook(spy, _TENANT_ID, "jira")
    assert "SET LOCAL" in spy.calls[0][0]


def test_register_invalid_tenant_raises_before_sql():
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        register_webhook(spy, None, "jira")
    assert len(spy.calls) == 0


def test_two_registrations_get_distinct_secrets():
    _, first = register_webhook(_SpyConn(), _TENANT_ID, "jira")
    _, second = register_webhook(_SpyConn(), _TENANT_ID, "jira")
    assert first != second


# ── rotate_secret ─────────────────────────────────────────────────────────────


def test_rotate_updates_secret_scoped_to_tenant():
    spy = _SpyConn(row=(_REGISTRATION_ID,))
    new_secret = rotate_secret(spy, _REGISTRATION_ID, _TENANT_ID)
    assert len(new_secret) == 64
    sql, params = next((s, p) for s, p in spy.calls if "UPDATE webhook_registrations" in s)
    assert "tenant_id = :tenant_id" in sql, "rotation must be tenant-scoped"
    assert params["signing_secret"] == new_secret
    assert params["tenant_id"] == str(_TENANT_ID)


def test_rotate_unknown_registration_raises_lookup_error():
    spy = _SpyConn(row=None)
    with pytest.raises(LookupError):
        rotate_secret(spy, _REGISTRATION_ID, _TENANT_ID)


# ── verify_and_resolve_tenant ─────────────────────────────────────────────────


def test_verify_valid_signature_returns_registration_tenant():
    spy = _SpyConn(row=(str(_TENANT_ID), _SECRET, True))
    resolved = verify_and_resolve_tenant(
        spy, _REGISTRATION_ID, _signature(_SECRET, _BODY), _BODY
    )
    assert resolved == str(_TENANT_ID)


def test_verify_is_a_pre_context_read():
    # The auth-bootstrap read (§13 KER-205 decision 2): no SET LOCAL may run —
    # there is no tenant to set until the signature has verified.
    spy = _SpyConn(row=(str(_TENANT_ID), _SECRET, True))
    verify_and_resolve_tenant(spy, _REGISTRATION_ID, _signature(_SECRET, _BODY), _BODY)
    assert not any("SET LOCAL" in s for s in spy.statements())


def test_verify_rejects_tampered_signature():
    spy = _SpyConn(row=(str(_TENANT_ID), _SECRET, True))
    good = _signature(_SECRET, _BODY)
    tampered = good[:-4] + ("0000" if not good.endswith("0000") else "ffff")
    with pytest.raises(WebhookAuthenticationError):
        verify_and_resolve_tenant(spy, _REGISTRATION_ID, tampered, _BODY)


def test_verify_rejects_signature_over_different_body():
    spy = _SpyConn(row=(str(_TENANT_ID), _SECRET, True))
    with pytest.raises(WebhookAuthenticationError):
        verify_and_resolve_tenant(
            spy, _REGISTRATION_ID, _signature(_SECRET, b'{"other":true}'), _BODY
        )


def test_verify_rejects_missing_and_malformed_headers():
    for bad_signature in (None, "", "sha1=abc", "sha256"):
        spy = _SpyConn(row=(str(_TENANT_ID), _SECRET, True))
        with pytest.raises(WebhookAuthenticationError):
            verify_and_resolve_tenant(spy, _REGISTRATION_ID, bad_signature, _BODY)


def test_verify_rejects_unknown_and_malformed_ids():
    for webhook_id, row in ((_REGISTRATION_ID, None), ("not-a-uuid", None), ("", None)):
        spy = _SpyConn(row=row)
        with pytest.raises(WebhookAuthenticationError):
            verify_and_resolve_tenant(spy, webhook_id, _signature(_SECRET, _BODY), _BODY)


def test_verify_rejects_inactive_registration():
    spy = _SpyConn(row=(str(_TENANT_ID), _SECRET, False))
    with pytest.raises(WebhookAuthenticationError):
        verify_and_resolve_tenant(spy, _REGISTRATION_ID, _signature(_SECRET, _BODY), _BODY)


# ── dedup ─────────────────────────────────────────────────────────────────────


def test_is_duplicate_true_when_row_inside_window():
    spy = _SpyConn(row=(1,))
    assert is_duplicate(spy, _TENANT_ID, "jira", "REF-1") is True


def test_is_duplicate_false_when_no_recent_row():
    spy = _SpyConn(row=None)
    assert is_duplicate(spy, _TENANT_ID, "jira", "REF-1") is False


def test_is_duplicate_window_start_matches_constant():
    spy = _SpyConn(row=None)
    before = datetime.now(timezone.utc)
    is_duplicate(spy, _TENANT_ID, "jira", "REF-1")
    after = datetime.now(timezone.utc)
    params = next(p for s, p in spy.calls if "FROM webhook_ingest_dedup" in s)
    low = before - timedelta(hours=WEBHOOK_DEDUP_WINDOW_HOURS)
    high = after - timedelta(hours=WEBHOOK_DEDUP_WINDOW_HOURS)
    assert low <= params["window_start"] <= high


def test_record_dedup_upserts_on_the_unique_triple():
    spy = _SpyConn()
    record_dedup(spy, _TENANT_ID, "jira", "REF-1")
    sql, params = next((s, p) for s, p in spy.calls if "webhook_ingest_dedup" in s)
    assert "ON CONFLICT" in sql and "DO UPDATE SET received_at" in sql
    assert params == {
        "tenant_id": str(_TENANT_ID), "source_system": "jira", "external_ref": "REF-1",
    }


def test_dedup_functions_set_tenant_context_first():
    for fn in (lambda c: is_duplicate(c, _TENANT_ID, "jira", "R"),
               lambda c: record_dedup(c, _TENANT_ID, "jira", "R")):
        spy = _SpyConn()
        fn(spy)
        assert "SET LOCAL" in spy.calls[0][0]


# ── normaliser ────────────────────────────────────────────────────────────────


def test_normalise_all_four_event_types():
    payload = {"summary": "Patch firewall", "description": "CVE fixed"}
    expectations = (
        ("jira.issue.updated", "ticket", "Patch firewall"),
        ("jira.issue.closed", "ticket", "Patch firewall (closed)"),
        ("cmdb.asset.updated", "asset", "Patch firewall"),
        ("generic.evidence.submitted", "evidence", "Patch firewall"),
    )
    for event_type, record_type, title in expectations:
        record = normalise_event(event_type, "REF-9", payload)
        assert record["record_type"] == record_type
        assert record["title"] == title
        assert record["body"] == "CVE fixed"
        assert record["external_id"] == "REF-9"
        assert len(record["content_hash"]) == 64


def test_normalise_unknown_event_type_raises():
    with pytest.raises(UnsupportedEventTypeError):
        normalise_event("slack.message.posted", "REF-1", {})


def test_normalise_falls_back_to_external_ref_and_json_body():
    record = normalise_event("cmdb.asset.updated", "ASSET-7", {"os": "debian"})
    assert record["title"] == "ASSET-7"
    assert record["body"] == '{"os":"debian"}'


def test_normalise_identical_payloads_share_content_hash():
    first = normalise_event("generic.evidence.submitted", "A", {"k": 1, "z": 2})
    second = normalise_event("generic.evidence.submitted", "B", {"z": 2, "k": 1})
    assert first["content_hash"] == second["content_hash"]  # canonical ordering


def test_supported_event_types_are_exactly_the_sprint_2b_four():
    assert set(WebhookNormaliser.SUPPORTED_EVENT_TYPES) == {
        "jira.issue.updated", "jira.issue.closed",
        "cmdb.asset.updated", "generic.evidence.submitted",
    }
