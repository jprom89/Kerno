"""Unit tests for src/services/mapping_service.py — map_control and its helper functions.

Fifteen tests cover tenant isolation, LLM and environment errors, JSON validation,
happy-path output, requires_human_review logic, and audit event emission.
All tests use spy connections and mocked Mistral clients; no live DB or network required.
"""

from __future__ import annotations

import json
import os
import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest

from config.constants import LOW_CONFIDENCE_THRESHOLD
from src.exceptions import MappingError, TenantContextMissingError
from src.services.mapping_service import (
    ControlInput,
    EvidenceInput,
    MappingRecommendation,
    _parse_llm_response,
    map_control,
)

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

_TENANT_ID = uuid.UUID("c0000000-0000-4000-a000-000000000003")
_MODEL_ID = "mistral-large-latest"
_NON_V4_UUID = uuid.UUID("a0000000-0000-1000-8000-000000000001")

_CONTROL = ControlInput(
    control_id="ctrl-001",
    framework="NIS2",
    control_ref="NIS2-4.2",
    title="Incident response",
    description="Must have a documented incident response plan.",
)

_EVIDENCE = [
    EvidenceInput(
        record_id="rec-001",
        title="IR Policy 2024",
        body="We maintain a formal incident response procedure approved by the CISO.",
        source_system="confluence",
    )
]

_VALID_LLM_PAYLOAD = {
    "status": "met",
    "confidence": 0.85,
    "evidence_ids": ["rec-001"],
    "reasoning": "The IR Policy document covers all required elements.",
    "gaps": [],
}

_VALID_LLM_RESPONSE = json.dumps(_VALID_LLM_PAYLOAD)


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class _NullResult:
    def fetchone(self):
        return None

    def fetchall(self) -> list:
        return []


class _SpyConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params=None) -> object:
        self.calls.append((sql, params))
        return _NullResult()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _mock_llm_client(response_text: str) -> MagicMock:
    mock_client = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = response_text
    mock_choice.finish_reason = "stop"
    mock_client.chat.complete.return_value = MagicMock(choices=[mock_choice])
    return mock_client


# ---------------------------------------------------------------------------
# Tenant isolation — four tests
# ---------------------------------------------------------------------------


def test_none_tenant_raises_tenant_context_missing_error():
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        map_control(spy, None, _CONTROL, _EVIDENCE)
    assert len(spy.calls) == 0


def test_empty_string_tenant_raises_tenant_context_missing_error():
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        map_control(spy, "", _CONTROL, _EVIDENCE)
    assert len(spy.calls) == 0


def test_non_v4_uuid_raises_tenant_context_missing_error():
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        map_control(spy, _NON_V4_UUID, _CONTROL, _EVIDENCE)
    assert len(spy.calls) == 0


def test_set_tenant_context_is_first_db_call():
    spy = _SpyConn()
    mock_client = _mock_llm_client(_VALID_LLM_RESPONSE)
    with patch("src.services.mapping_service.get_llm_client", return_value=mock_client), \
         patch("src.services.mapping_service.write_audit_event"), \
         patch.dict(os.environ, {"KERNO_LLM_MODEL": _MODEL_ID}):
        map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)
    assert len(spy.calls) > 0
    assert "SET LOCAL" in spy.calls[0][0]
    # Sub-services (KER-203 decision log) may re-set the SAME tenant context —
    # what must never happen is a SET LOCAL pointing at a different tenant.
    for sql, params in spy.calls:
        if "SET LOCAL" in sql:
            assert params == [str(_TENANT_ID)]


def test_recommendation_writes_ai_decision_log_in_same_transaction():
    # KER-203 AC-2: the decision-log INSERT rides the same connection as the
    # recommendation INSERT — no recommendation without its retained record.
    spy = _SpyConn()
    mock_client = _mock_llm_client(_VALID_LLM_RESPONSE)
    with patch("src.services.mapping_service.get_llm_client", return_value=mock_client), \
         patch("src.services.mapping_service.write_audit_event"), \
         patch.dict(os.environ, {"KERNO_LLM_MODEL": _MODEL_ID}):
        map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)
    statements = [sql for sql, _ in spy.calls]
    assert any("INSERT INTO recommendations" in sql for sql in statements)
    log_params = next(p for s, p in spy.calls if "INSERT INTO ai_decision_log" in s)
    assert log_params["control_id"] == _CONTROL.control_id
    assert log_params["output_status"] == "met"
    assert log_params["evidence_ids"] == ["rec-001"]
    assert log_params["model_version"] == _MODEL_ID
    assert len(log_params["input_snapshot_hash"]) == 64  # SHA-256 hex digest
    assert 0.0 <= log_params["confidence_score"] <= 1.0


# ---------------------------------------------------------------------------
# Environment and LLM errors — three tests
# ---------------------------------------------------------------------------


def test_missing_model_env_var_raises_mapping_error(monkeypatch):
    monkeypatch.delenv("KERNO_LLM_MODEL", raising=False)
    spy = _SpyConn()
    with pytest.raises(MappingError, match="KERNO_LLM_MODEL"):
        map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)


def test_llm_api_error_raises_mapping_error():
    spy = _SpyConn()
    mock_client = MagicMock()
    mock_client.chat.complete.side_effect = httpx.TimeoutException("timeout")
    with patch("src.services.mapping_service.get_llm_client", return_value=mock_client), \
         patch.dict(os.environ, {"KERNO_LLM_MODEL": _MODEL_ID}), \
         pytest.raises(MappingError, match="LLM API call failed"):
        map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)


def test_invalid_json_from_llm_raises_mapping_error():
    spy = _SpyConn()
    mock_client = _mock_llm_client("not valid json{{{")
    with patch("src.services.mapping_service.get_llm_client", return_value=mock_client), \
         patch.dict(os.environ, {"KERNO_LLM_MODEL": _MODEL_ID}), \
         pytest.raises(MappingError, match="invalid JSON"):
        map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)


# ---------------------------------------------------------------------------
# JSON validation — four tests (via _parse_llm_response directly)
# ---------------------------------------------------------------------------


def test_invalid_status_raises_mapping_error():
    bad = {**_VALID_LLM_PAYLOAD, "status": "unknown"}
    with pytest.raises(MappingError, match="Invalid status"):
        _parse_llm_response(json.dumps(bad))


def test_confidence_above_one_raises_mapping_error():
    bad = {**_VALID_LLM_PAYLOAD, "confidence": 1.5}
    with pytest.raises(MappingError, match="between 0.0 and 1.0"):
        _parse_llm_response(json.dumps(bad))


def test_missing_reasoning_raises_mapping_error():
    bad = {k: v for k, v in _VALID_LLM_PAYLOAD.items() if k != "reasoning"}
    with pytest.raises(MappingError, match="reasoning"):
        _parse_llm_response(json.dumps(bad))


def test_gaps_not_a_list_raises_mapping_error():
    bad = {**_VALID_LLM_PAYLOAD, "gaps": "should be a list"}
    with pytest.raises(MappingError, match="gaps must be a list"):
        _parse_llm_response(json.dumps(bad))


# ---------------------------------------------------------------------------
# Happy path and field mapping — three tests
# ---------------------------------------------------------------------------


def test_map_control_returns_mapping_recommendation_with_correct_fields():
    spy = _SpyConn()
    mock_client = _mock_llm_client(_VALID_LLM_RESPONSE)
    with patch("src.services.mapping_service.get_llm_client", return_value=mock_client), \
         patch("src.services.mapping_service.write_audit_event"), \
         patch.dict(os.environ, {"KERNO_LLM_MODEL": _MODEL_ID}):
        result = map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)
    assert isinstance(result, MappingRecommendation)
    assert result.control_id == "ctrl-001"
    assert result.status == "met"
    assert result.confidence == pytest.approx(0.85)
    assert result.evidence_ids == ["rec-001"]
    assert result.gaps == []
    assert result.requires_human_review is False


def test_low_confidence_sets_requires_human_review_true():
    low_payload = {**_VALID_LLM_PAYLOAD, "confidence": 0.3, "status": "gap"}
    spy = _SpyConn()
    mock_client = _mock_llm_client(json.dumps(low_payload))
    with patch("src.services.mapping_service.get_llm_client", return_value=mock_client), \
         patch("src.services.mapping_service.write_audit_event"), \
         patch.dict(os.environ, {"KERNO_LLM_MODEL": _MODEL_ID}):
        result = map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)
    assert result.requires_human_review is True


def test_confidence_exactly_at_threshold_does_not_require_human_review():
    threshold_payload = {**_VALID_LLM_PAYLOAD, "confidence": LOW_CONFIDENCE_THRESHOLD}
    spy = _SpyConn()
    mock_client = _mock_llm_client(json.dumps(threshold_payload))
    with patch("src.services.mapping_service.get_llm_client", return_value=mock_client), \
         patch("src.services.mapping_service.write_audit_event"), \
         patch.dict(os.environ, {"KERNO_LLM_MODEL": _MODEL_ID}):
        result = map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)
    assert result.requires_human_review is False


# ---------------------------------------------------------------------------
# Audit event emission — one test
# ---------------------------------------------------------------------------


def test_audit_event_emitted_with_correct_event_type():
    spy = _SpyConn()
    mock_client = _mock_llm_client(_VALID_LLM_RESPONSE)
    with patch("src.services.mapping_service.get_llm_client", return_value=mock_client), \
         patch("src.services.mapping_service.write_audit_event") as mock_audit, \
         patch.dict(os.environ, {"KERNO_LLM_MODEL": _MODEL_ID}):
        map_control(spy, _TENANT_ID, _CONTROL, _EVIDENCE)
    mock_audit.assert_called_once()
    event_type_arg = mock_audit.call_args[0][2]
    assert event_type_arg == "recommendation_generated"
