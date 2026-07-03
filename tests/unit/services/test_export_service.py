"""Unit tests for src/services/export_service.py — deterministic evidence pack assembly (KER-111).

Ten tests cover full-field assembly, deterministic ordering (controls by control_ref,
evidence by linked_at, audit entries by created_at), byte-stable serialisation with schema
round-trip, empty-list preservation, the export_generated ledger entry, ValueError on an
empty family, and tenant isolation. Spy connections only; no database required.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.api.schemas.export import EvidencePack
from src.exceptions import TenantContextMissingError
from src.services.export_service import (
    DECIDED_BY_AI,
    DECIDED_BY_HUMAN,
    build_evidence_pack,
    serialise_pack,
)

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_CONTROL_A = "e1000000-0000-4000-a000-00000000000a"
_CONTROL_B = "e1000000-0000-4000-a000-00000000000b"
_BASE_TIME = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ── Test infrastructure ───────────────────────────────────────────────────────


class _RowsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _ExportSpyConn:
    """Serves canned rows for every query the pack assembly issues."""

    def __init__(
        self,
        coverage_rows: list | None = None,
        recommendation_row: tuple | None = None,
        evidence_rows: list | None = None,
        decision_rows: list | None = None,
        audit_rows: list | None = None,
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self._coverage_rows = coverage_rows or []
        self._recommendation_row = recommendation_row
        self._evidence_rows = evidence_rows or []
        self._decision_rows = decision_rows or []
        self._audit_rows = audit_rows or []

    def execute(self, sql: str, params=None):
        self.calls.append((sql, params))
        if "FROM compliance_controls" in sql:
            return _RowsResult(self._coverage_rows)
        if "SELECT entry_hash" in sql:
            return _RowsResult([])
        if "FROM audit_log" in sql:
            return _RowsResult(self._audit_rows)
        if "FROM control_evidence_links" in sql:
            return _RowsResult(self._evidence_rows)
        if "FROM recommendations" in sql:
            return _RowsResult([self._recommendation_row] if self._recommendation_row else [])
        if "FROM overrides" in sql:
            return _RowsResult(self._decision_rows)
        return _RowsResult([])

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _FakeSession:
    def __init__(self, tenant_id=_TENANT_ID) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self):
        return self._tenant_id


def _coverage_row(control_id: str, ref: str, rec_status: str | None, override_action: str | None) -> tuple:
    return (control_id, ref, f"Title {ref}", "governance", "nis2",
            rec_status, "high" if rec_status else None, 0.9 if rec_status else None,
            override_action, 1)


def _recommendation_row() -> tuple:
    return ("r0000000-0000-4000-a000-000000000001", str(_TENANT_ID), _CONTROL_A,
            "met", "high", 0.9, "Strong evidence coverage.", None,
            [], False, {}, _BASE_TIME, False)


def _evidence_row(record_id: str, linked_at: datetime) -> tuple:
    return (f"link-{record_id}", _CONTROL_A, record_id, "alice", linked_at, 0.8, None,
            False, "confluence", f"CONF-{record_id}", "policy", "Doc", "body",
            _BASE_TIME, "hash")


def _decision_row(override_id: str, created_at: datetime) -> tuple:
    return (override_id, "approve", "vciso", created_at, "Confirmed.")


def _audit_row(entry_id: str, created_at: datetime) -> tuple:
    return (entry_id, str(_TENANT_ID), None, "vciso", "approve", "override",
            "ov-1", _CONTROL_A, None, None, created_at, "0" * 64, "a" * 64, 1)


def _populated_spy(**kwargs) -> _ExportSpyConn:
    defaults = {
        "coverage_rows": [_coverage_row(_CONTROL_A, "NIS2-1.1", "met", "approve")],
        "recommendation_row": _recommendation_row(),
        "evidence_rows": [
            _evidence_row("rec-2", _BASE_TIME + timedelta(hours=2)),
            _evidence_row("rec-1", _BASE_TIME + timedelta(hours=1)),
        ],
        "decision_rows": [
            _decision_row("ov-1", _BASE_TIME + timedelta(hours=1)),
            _decision_row("ov-2", _BASE_TIME + timedelta(hours=3)),
        ],
        "audit_rows": [
            _audit_row("au-2", _BASE_TIME + timedelta(hours=4)),
            _audit_row("au-1", _BASE_TIME + timedelta(hours=2)),
        ],
    }
    defaults.update(kwargs)
    return _ExportSpyConn(**defaults)


# ── Assembly ──────────────────────────────────────────────────────────────────


def test_pack_contains_all_expected_fields():
    pack = build_evidence_pack(_populated_spy(), _FakeSession(), "governance")
    assert pack.metadata.control_family == "governance"
    assert pack.metadata.tenant_id == str(_TENANT_ID)
    control = pack.controls[0]
    assert control.control_ref == "NIS2-1.1"
    assert control.system_of_record_status == "met"
    assert control.decided_by == DECIDED_BY_HUMAN
    assert control.decided_at == _BASE_TIME + timedelta(hours=3)
    assert control.rationale == "Strong evidence coverage."
    assert control.evidence[0].source_system == "confluence"
    assert control.decisions[0].action_type == "approve"
    assert control.audit_extract[0].entry_hash == "a" * 64


def test_controls_sorted_by_control_ref():
    spy = _populated_spy(coverage_rows=[
        _coverage_row(_CONTROL_B, "NIS2-1.2", "met", "approve"),
        _coverage_row(_CONTROL_A, "NIS2-1.1", "met", "approve"),
    ])
    pack = build_evidence_pack(spy, _FakeSession(), "governance")
    assert [c.control_ref for c in pack.controls] == ["NIS2-1.1", "NIS2-1.2"]


def test_evidence_and_audit_sorted_ascending():
    pack = build_evidence_pack(_populated_spy(), _FakeSession(), "governance")
    control = pack.controls[0]
    assert [e.evidence_id for e in control.evidence] == ["rec-1", "rec-2"]
    assert [a.entry_id for a in control.audit_extract] == ["au-1", "au-2"]


def test_control_without_records_kept_with_empty_lists():
    spy = _populated_spy(
        coverage_rows=[_coverage_row(_CONTROL_A, "NIS2-1.1", None, None)],
        recommendation_row=None, evidence_rows=[], decision_rows=[], audit_rows=[],
    )
    pack = build_evidence_pack(spy, _FakeSession(), "governance")
    control = pack.controls[0]
    assert control.system_of_record_status == "gap"
    assert control.decided_by == DECIDED_BY_AI
    assert control.decided_at is None
    assert control.rationale is None
    assert (control.evidence, control.decisions, control.audit_extract) == ([], [], [])


def test_decided_at_for_unconfirmed_uses_recommendation_time():
    spy = _populated_spy(
        coverage_rows=[_coverage_row(_CONTROL_A, "NIS2-1.1", "partial", None)],
        decision_rows=[],
    )
    pack = build_evidence_pack(spy, _FakeSession(), "governance")
    assert pack.controls[0].decided_by == DECIDED_BY_AI
    assert pack.controls[0].decided_at == _BASE_TIME


# ── Determinism ───────────────────────────────────────────────────────────────


def test_building_twice_produces_identical_content():
    pack_one = build_evidence_pack(_populated_spy(), _FakeSession(), "governance")
    pack_two = build_evidence_pack(_populated_spy(), _FakeSession(), "governance")
    # export_id and generated_at identify each generation; the content is stable.
    assert pack_one.controls == pack_two.controls
    assert pack_one.metadata.control_family == pack_two.metadata.control_family


def test_serialise_pack_is_byte_stable_and_schema_valid():
    pack = build_evidence_pack(_populated_spy(), _FakeSession(), "governance")
    first = serialise_pack(pack)
    second = serialise_pack(pack)
    assert first == second
    assert not first.decode("utf-8").endswith((" ", "\n"))
    round_tripped = EvidencePack.model_validate_json(first)
    assert round_tripped == pack


# ── Audit, validation, isolation ──────────────────────────────────────────────


def test_export_generation_recorded_in_ledger():
    spy = _populated_spy()
    pack = build_evidence_pack(spy, _FakeSession(), "governance")
    params = next(p for s, p in spy.calls if "INSERT INTO audit_log" in s)
    assert params["action_type"] == "export_generated"
    assert params["object_type"] == "evidence_pack"
    assert params["object_id"] == pack.metadata.export_id
    assert params["control_id"] is None
    after_state = json.loads(params["after_state"])
    assert after_state["control_family"] == "governance"
    assert after_state["control_count"] == 1


def test_empty_family_raises_value_error():
    spy = _ExportSpyConn(coverage_rows=[])
    with pytest.raises(ValueError, match="No active controls"):
        build_evidence_pack(spy, _FakeSession(), "nonexistent")
    assert not any("INSERT INTO audit_log" in s for s, _ in spy.calls)


def test_none_session_raises_before_sql():
    spy = _ExportSpyConn()
    with pytest.raises(TenantContextMissingError):
        build_evidence_pack(spy, None, "governance")
    assert len(spy.calls) == 0
