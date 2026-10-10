"""Unit tests for src/services/override_service.py — capture_override and its validation.

Covers justification anonymisation, the raw-connection contract, statement and lock order,
hash-chained audit ledger linkage, created_at read-back, reviewer weighting, input validation,
tenant isolation, the conftest named-parameter regex, and the SEC-REMED-005 binding of every
decision to the recommendation the reviewer saw. Spy connections record every execute() call
and answer the recommendation reads; no database is required. The live proof of the binding is
tests/integration/test_sec_remed_005_review_binding.py.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from config.constants import AUDIT_GENESIS_HASH, JUNIOR_REVIEWER_WEIGHT, SENIOR_REVIEWER_WEIGHT
from src.exceptions import EntryNotFoundError, StaleRecommendationError, TenantContextMissingError
from src.services.override_service import OverrideInput, capture_override

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_REVIEWER_ID = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_RECOMMENDATION_ID = "f0000000-0000-4000-f000-000000000005"
_NEWER_RECOMMENDATION_ID = "f0000000-0000-4000-f000-000000000006"
_CONTROL_ID = "ctrl-001"
_RECOMMENDATION_STATUS = "partial"

# The server-generated created_at the spy returns for the read-back SELECT.
_CREATED_AT = datetime(2025, 6, 1, tzinfo=timezone.utc)

# Default spy answers: the reviewed recommendation exists in this tenant, belongs
# to _CONTROL_ID, is not superseded, and is the control's current row.
_REVIEWED_ROW = (_RECOMMENDATION_ID, _CONTROL_ID, _RECOMMENDATION_STATUS, False)
_CURRENT_ROW = (_RECOMMENDATION_ID,)

# Fragments that tell the two recommendation reads apart.
_REVIEWED_READ = "FOR SHARE"
_CURRENT_READ = "ORDER BY generated_at DESC"

_REVIEW_LOCK_PREFIX = "recommendation-review:"
_ROW_LOCK_CLAUSES = ("FOR UPDATE", "FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE")


# ── Test infrastructure ───────────────────────────────────────────────────────


class _RowResult:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self) -> list:
        if self._row is None:
            return []
        return [self._row]


class _SpyConn:
    """Records execute() calls; raises on SQLAlchemy Session API usage (add/flush).

    Answers the reviewed-recommendation read with ``reviewed_row`` and the
    current-recommendation read with ``current_row``; None means no row.
    """

    def __init__(
        self,
        reviewed_row: tuple | None = _REVIEWED_ROW,
        current_row: tuple | None = _CURRENT_ROW,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self._reviewed_row = reviewed_row
        self._current_row = current_row

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "SELECT created_at" in sql:
            return _RowResult((_CREATED_AT,))
        if _REVIEWED_READ in sql:
            return _RowResult(self._reviewed_row)
        if _CURRENT_READ in sql:
            return _RowResult(self._current_row)
        return _RowResult(None)

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
        "original_control_id": _CONTROL_ID,
        "corrected_control_id": None,
        "justification_text": None,
        "recommendation_id": _RECOMMENDATION_ID,
    }
    defaults.update(kwargs)
    return OverrideInput(**defaults)


def _statements(spy: _SpyConn) -> list[str]:
    return [str(sql) for sql, _ in spy.calls]


def _calls_matching(spy: _SpyConn, fragment: str) -> list[tuple]:
    return [call for call in spy.calls if fragment in str(call[0])]


def _position_of(statements: list[str], fragment: str) -> int:
    for index, statement in enumerate(statements):
        if fragment in statement:
            return index
    raise AssertionError(f"no statement containing {fragment!r} was issued")


def _advisory_locks(spy: _SpyConn) -> list[tuple[int, str]]:
    """Return (statement position, lock_key) for every advisory lock taken, in order."""
    locks = []
    for index, call in enumerate(spy.calls):
        sql, params = call
        if "pg_advisory_xact_lock" in str(sql):
            locks.append((index, params["lock_key"]))
    return locks


def _row_locking_statements(spy: _SpyConn) -> list[str]:
    row_locking = []
    for statement in _statements(spy):
        for clause in _ROW_LOCK_CLAUSES:
            if clause in statement:
                row_locking.append(statement)
                break
    return row_locking


def _override_insert_params(spy: _SpyConn) -> dict:
    return next(p for s, p in spy.calls if "INSERT INTO overrides" in s)


def _audit_insert_params(spy: _SpyConn) -> dict:
    return next(p for s, p in spy.calls if "INSERT INTO audit_log" in s)


def _assert_nothing_written(spy: _SpyConn) -> None:
    assert _calls_matching(spy, "INSERT INTO overrides") == []
    assert _calls_matching(spy, "INSERT INTO audit_log") == []


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_null_justification_text_stored_as_none() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(justification_text=None))
    override_params = _override_insert_params(spy)
    assert override_params["justification_text"] is None


def test_email_in_justification_text_is_anonymised() -> None:
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Reviewed by alice@example.com"),
    )
    override_params = _override_insert_params(spy)
    assert "[INTERNAL_EMAIL]" in override_params["justification_text"]
    assert "alice@example.com" not in override_params["justification_text"]


def test_internal_hostname_in_justification_text_is_anonymised() -> None:
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Control mapped via proxy.internal gateway"),
    )
    override_params = _override_insert_params(spy)
    assert "[INTERNAL_HOST]" in override_params["justification_text"]
    assert "proxy.internal" not in override_params["justification_text"]


def test_anonymised_value_appears_in_both_override_and_audit_log() -> None:
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Contact admin@kerno.io for details"),
    )
    override_params = _override_insert_params(spy)
    audit_after_state = json.loads(_audit_insert_params(spy)["after_state"])
    assert "[INTERNAL_EMAIL]" in override_params["justification_text"]
    assert audit_after_state["justification_text"] == override_params["justification_text"]


def test_no_sqlalchemy_session_api_called() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())


def test_set_local_fires_before_insert_override() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    statements = _statements(spy)
    assert "SET LOCAL" in statements[0]
    assert _position_of(statements, "SET LOCAL") < _position_of(statements, "INSERT INTO overrides")


def test_audit_log_references_correct_override_id() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    override_params = _override_insert_params(spy)
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
    statements = _statements(spy)
    insert_at = _position_of(statements, "INSERT INTO overrides")
    select_at = _position_of(statements, "SELECT created_at")
    assert select_at == insert_at + 1
    select_params = spy.calls[select_at][1]
    assert select_params["id"] == _override_insert_params(spy)["override_id"]


def test_vciso_gets_senior_confidence_weight() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(reviewer_role="vciso"))
    override_params = _override_insert_params(spy)
    assert override_params["reviewer_confidence_weight"] == SENIOR_REVIEWER_WEIGHT


def test_internal_admin_gets_junior_confidence_weight() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(reviewer_role="internal_admin"))
    override_params = _override_insert_params(spy)
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


@pytest.mark.parametrize("action_type", ["edit", "reject"])
@pytest.mark.parametrize("blank", [None, "", "   ", "\n\t "])
def test_edit_and_reject_require_a_justification(action_type: str, blank) -> None:
    spy = _SpyConn()
    with pytest.raises(ValueError, match="justification_text"):
        capture_override(
            _FakeSession(),
            spy,
            _make_input(
                action_type=action_type,
                corrected_control_id="ctrl-002",
                justification_text=blank,
            ),
        )
    assert len(spy.calls) == 0


def test_approve_still_needs_no_justification() -> None:
    # §14 KER-303 AC-4. The Approve button sends justification_text: null, and
    # agreeing with the recommendation adds nothing the reviewer id and
    # timestamp do not already record.
    spy = _SpyConn()
    capture_override(
        _FakeSession(), spy, _make_input(action_type="approve", justification_text=None)
    )
    assert len(spy.calls) > 0


def test_justification_is_stored_stripped() -> None:
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(
            action_type="edit",
            corrected_control_id="ctrl-002",
            justification_text="  the evidence maps to 002  ",
        ),
    )
    insert = _override_insert_params(spy)
    assert insert["justification_text"] == "the evidence maps to 002"


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


# ── SEC-REMED-005: the decision binds to the recommendation the reviewer saw ──


def test_missing_recommendation_id_raises_value_error_before_any_sql() -> None:
    spy = _SpyConn()
    override_input = OverrideInput(
        reviewer_id=_REVIEWER_ID,
        reviewer_role="vciso",
        action_type="approve",
        original_control_id=_CONTROL_ID,
    )
    with pytest.raises(ValueError, match="recommendation_id is required"):
        capture_override(_FakeSession(), spy, override_input)
    assert spy.calls == []


@pytest.mark.parametrize("malformed", ["not-a-uuid", "   ", "f0000000-0000-4000-f000-00000000000g"])
def test_malformed_recommendation_id_raises_value_error_before_any_sql(malformed: str) -> None:
    spy = _SpyConn()
    with pytest.raises(ValueError, match="recommendation_id must be a UUID"):
        capture_override(_FakeSession(), spy, _make_input(recommendation_id=malformed))
    assert spy.calls == []


def test_recommendation_outside_the_tenant_raises_entry_not_found_and_writes_nothing() -> None:
    # The tenant-scoped read finds no row: a nonexistent id and another
    # tenant's id are the same miss.
    spy = _SpyConn(reviewed_row=None)
    with pytest.raises(EntryNotFoundError):
        capture_override(_FakeSession(), spy, _make_input())
    _assert_nothing_written(spy)


def test_recommendation_for_another_control_raises_value_error_and_writes_nothing() -> None:
    spy = _SpyConn(reviewed_row=(_RECOMMENDATION_ID, "ctrl-999", _RECOMMENDATION_STATUS, False))
    with pytest.raises(ValueError, match="does not belong to control"):
        capture_override(_FakeSession(), spy, _make_input())
    _assert_nothing_written(spy)


def test_superseded_recommendation_raises_stale_and_writes_nothing() -> None:
    # The current-id read still names the reviewed row, so the superseded flag
    # alone must be enough to refuse.
    spy = _SpyConn(reviewed_row=(_RECOMMENDATION_ID, _CONTROL_ID, _RECOMMENDATION_STATUS, True))
    with pytest.raises(StaleRecommendationError):
        capture_override(_FakeSession(), spy, _make_input())
    _assert_nothing_written(spy)


def test_recommendation_that_is_no_longer_current_raises_stale_and_writes_nothing() -> None:
    spy = _SpyConn(current_row=(_NEWER_RECOMMENDATION_ID,))
    with pytest.raises(StaleRecommendationError):
        capture_override(_FakeSession(), spy, _make_input())
    _assert_nothing_written(spy)


def test_review_lock_then_recommendation_reads_then_writes_then_ledger_lock() -> None:
    # Lock order is the contract: the control's review lock before the
    # recommendation rows, both before the override write, and the tenant
    # ledger lock last, inside append_audit_entry.
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    statements = _statements(spy)
    locks = _advisory_locks(spy)
    assert [lock_key for _, lock_key in locks] == [
        "recommendation-review:c0000000-0000-4000-a000-000000000003:ctrl-001",
        str(_TENANT_ID),
    ]
    review_lock_at = locks[0][0]
    ledger_lock_at = locks[1][0]
    set_local_at = _position_of(statements, "SET LOCAL")
    reviewed_read_at = _position_of(statements, _REVIEWED_READ)
    current_read_at = _position_of(statements, _CURRENT_READ)
    override_insert_at = _position_of(statements, "INSERT INTO overrides")
    audit_insert_at = _position_of(statements, "INSERT INTO audit_log")
    assert set_local_at < review_lock_at < reviewed_read_at < current_read_at
    assert current_read_at < override_insert_at < ledger_lock_at < audit_insert_at


def test_for_share_on_the_reviewed_read_is_the_only_row_lock() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    row_locking = _row_locking_statements(spy)
    assert len(row_locking) == 1
    assert "FOR SHARE" in row_locking[0]
    assert "FROM recommendations" in row_locking[0]


def test_both_recommendation_reads_are_tenant_scoped() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    [(reviewed_sql, reviewed_params)] = _calls_matching(spy, _REVIEWED_READ)
    [(current_sql, current_params)] = _calls_matching(spy, _CURRENT_READ)
    assert "tenant_id = :tenant_id" in reviewed_sql
    assert reviewed_params == {"tenant_id": str(_TENANT_ID), "recommendation_id": _RECOMMENDATION_ID}
    assert "tenant_id = :tenant_id" in current_sql
    assert current_params == {"tenant_id": str(_TENANT_ID), "control_id": _CONTROL_ID}


def test_the_claim_receives_the_canonical_recommendation_id() -> None:
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(recommendation_id=f"urn:uuid:{_RECOMMENDATION_ID.upper()}"))
    [(_, reviewed_params)] = _calls_matching(spy, _REVIEWED_READ)
    assert reviewed_params["recommendation_id"] == _RECOMMENDATION_ID


def test_override_insert_stores_the_canonical_recommendation_id() -> None:
    spy = _SpyConn()
    override = capture_override(
        _FakeSession(), spy, _make_input(recommendation_id=_RECOMMENDATION_ID.upper())
    )
    assert _override_insert_params(spy)["recommendation_id"] == _RECOMMENDATION_ID
    assert override.recommendation_id == uuid.UUID(_RECOMMENDATION_ID)


def test_ledger_entry_records_which_recommendation_was_decided() -> None:
    spy = _SpyConn(reviewed_row=(_RECOMMENDATION_ID, _CONTROL_ID, "gap", False))
    capture_override(
        _FakeSession(),
        spy,
        _make_input(
            action_type="edit",
            corrected_control_id="ctrl-002",
            justification_text="the evidence maps to 002",
        ),
    )
    audit_params = _audit_insert_params(spy)
    assert json.loads(audit_params["before_state"]) == {
        "control_id": _CONTROL_ID,
        "recommendation_id": _RECOMMENDATION_ID,
        "recommendation_status": "gap",
    }
    assert json.loads(audit_params["after_state"]) == {
        "control_id": "ctrl-002",
        "justification_text": "the evidence maps to 002",
        "recommendation_id": _RECOMMENDATION_ID,
    }
