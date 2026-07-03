"""Unit tests for src/services/override_service.py — capture_override and its validation.

Fifteen tests cover justification anonymisation, the raw-connection contract, SET LOCAL
ordering, hash-chained audit ledger linkage, created_at read-back, reviewer weighting,
input validation, tenant isolation, and the conftest named-parameter regex.
Spy connections record every execute() call; no database is required.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from config.constants import AUDIT_GENESIS_HASH, JUNIOR_REVIEWER_WEIGHT, SENIOR_REVIEWER_WEIGHT
from src.exceptions import TenantContextMissingError
from src.services.override_service import OverrideInput, capture_override

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_REVIEWER_ID = uuid.UUID("d0000000-0000-4000-d000-000000000004")

# The server-generated created_at the spy returns for the read-back SELECT.
_CREATED_AT = datetime(2025, 6, 1, tzinfo=timezone.utc)


# ── Test infrastructure ───────────────────────────────────────────────────────


class _NullResult:
    def fetchone(self):
        return None

    def fetchall(self) -> list:
        return []


class _CreatedAtResult:
    def fetchone(self):
        return (_CREATED_AT,)

    def fetchall(self) -> list:
        return [(_CREATED_AT,)]


class _SpyConn:
    """Records execute() calls; raises on SQLAlchemy Session API usage (add/flush)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "SELECT created_at" in sql:
            return _CreatedAtResult()
        return _NullResult()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def add(self, *args, **kwargs) -> None:
        raise AssertionError(
            "conn.add() was called. The override service must use "
            "conn.execute(sql, params) — not the SQLAlchemy Session API."
        )

    def flush(self, *args, **kwargs) -> None:
        raise AssertionError(
            "conn.flush() was called. The override service must use "
            "conn.execute(sql, params) — not the SQLAlchemy Session API."
        )


class _FakeSession:
    def __init__(self, tenant_id: uuid.UUID = _TENANT_ID) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> uuid.UUID:
        return self._tenant_id


def _make_input(**kwargs) -> OverrideInput:
    defaults: dict = {
        "reviewer_id": _REVIEWER_ID,
        "reviewer_role": "vciso",
        "action_type": "approve",
        "original_control_id": "ctrl-001",
        "corrected_control_id": None,
        "justification_text": None,
    }
    defaults.update(kwargs)
    return OverrideInput(**defaults)


def _audit_insert_params(spy: _SpyConn) -> dict:
    return next(p for s, p in spy.calls if "INSERT INTO audit_log" in s)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_null_justification_text_stored_as_none() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(justification_text=None))
    override_params = spy.calls[1][1]
    assert override_params["justification_text"] is None


def test_email_in_justification_text_is_anonymised() -> None:
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Reviewed by alice@example.com"),
    )
    override_params = spy.calls[1][1]
    assert "[INTERNAL_EMAIL]" in override_params["justification_text"]
    assert "alice@example.com" not in override_params["justification_text"]


def test_internal_hostname_in_justification_text_is_anonymised() -> None:
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Control mapped via proxy.internal gateway"),
    )
    override_params = spy.calls[1][1]
    assert "[INTERNAL_HOST]" in override_params["justification_text"]
    assert "proxy.internal" not in override_params["justification_text"]


def test_anonymised_value_appears_in_both_override_and_audit_log() -> None:
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Contact admin@kerno.io for details"),
    )
    override_params = spy.calls[1][1]
    audit_after_state = json.loads(_audit_insert_params(spy)["after_state"])
    assert "[INTERNAL_EMAIL]" in override_params["justification_text"]
    assert audit_after_state["justification_text"] == override_params["justification_text"]


def test_no_sqlalchemy_session_api_called() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())


def test_set_local_fires_before_insert_override() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    first_sql, _ = spy.calls[0]
    assert "SET LOCAL" in first_sql


def test_audit_log_references_correct_override_id() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    override_params = spy.calls[1][1]
    audit_params = _audit_insert_params(spy)
    assert audit_params["object_id"] == override_params["override_id"]
    assert audit_params["object_type"] == "override"


def test_override_audit_entry_is_hash_chained() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    audit_params = _audit_insert_params(spy)
    assert audit_params["previous_hash"] == AUDIT_GENESIS_HASH
    assert audit_params["entry_hash"] != AUDIT_GENESIS_HASH
    assert len(audit_params["entry_hash"]) == len(AUDIT_GENESIS_HASH)


def test_created_at_is_read_back_from_database() -> None:
    spy = _SpyConn()
    override = capture_override(_FakeSession(), spy, _make_input())
    assert override.created_at == _CREATED_AT
    select_sql, select_params = spy.calls[2]
    assert "SELECT created_at" in select_sql
    assert select_params["id"] == spy.calls[1][1]["override_id"]


def test_vciso_gets_senior_confidence_weight() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(reviewer_role="vciso"))
    override_params = spy.calls[1][1]
    assert override_params["reviewer_confidence_weight"] == SENIOR_REVIEWER_WEIGHT


def test_internal_admin_gets_junior_confidence_weight() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(reviewer_role="internal_admin"))
    override_params = spy.calls[1][1]
    assert override_params["reviewer_confidence_weight"] == JUNIOR_REVIEWER_WEIGHT


def test_invalid_action_type_raises_value_error() -> None:
    spy = _SpyConn()
    with pytest.raises(ValueError, match="action_type"):
        capture_override(_FakeSession(), spy, _make_input(action_type="approve_all"))
    assert len(spy.calls) == 0


def test_edit_without_corrected_control_id_raises_value_error() -> None:
    spy = _SpyConn()
    with pytest.raises(ValueError, match="corrected_control_id"):
        capture_override(
            _FakeSession(),
            spy,
            _make_input(action_type="edit", corrected_control_id=None),
        )
    assert len(spy.calls) == 0


def test_none_session_raises_tenant_context_missing_error() -> None:
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        capture_override(None, spy, _make_input())
    assert len(spy.calls) == 0


def test_named_param_regex_does_not_match_postgresql_type_casts() -> None:
    # PostgreSQL ::typename casts must not be mistaken for :name parameters.
    from tests.conftest import _NAMED_PARAM_RE

    sql = "WHERE tenant_id = :tenant_id AND ts > '1970-01-01'::timestamptz"
    matches = _NAMED_PARAM_RE.findall(sql)
    assert matches == ["tenant_id"]
    assert "timestamptz" not in matches
