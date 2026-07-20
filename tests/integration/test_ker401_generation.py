"""KER-401 integration tests — the hybrid generation path against a live database.

Proves the same-transaction invariant the story's AC-5/AC-6 demand, in BOTH
directions: a successful generation commits the recommendation row, its KER-203
decision-log row, and its KER-107 ledger entry together; a failure after the
recommendation INSERT rolls all of them back together. Also proves the
template fallback works end-to-end on a live connection. The LLM client is
mocked at the service module (network-free); every database write is real.
Requires DATABASE_URL with migration 020 applied. The fixture cleans up its
own rows — recommendations and control_evidence_links are not covered by the
shared conftest teardown.

Run: pytest tests/integration/test_ker401_generation.py -m integration -v
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.services.recommendation_service import generate_recommendation

_CONTROL_UUID = str(uuid.UUID("c4010000-0000-4000-c000-000000000001"))
_RECORD_UUID = str(uuid.UUID("c4010000-0000-4000-c000-000000000002"))
_LINK_UUID = str(uuid.UUID("c4010000-0000-4000-c000-000000000003"))
_TRIGGER_USER = "d0000000-0000-4000-d000-000000000004"
_LLM_RATIONALE = "The IR runbook record directly evidences the control's requirement."


def _mock_llm_client() -> MagicMock:
    client = MagicMock()
    choice = MagicMock()
    choice.message.content = json.dumps({
        "rationale": _LLM_RATIONALE,
        "own_status": "met",
        "own_confidence": 0.8,
    })
    client.chat.complete.return_value = MagicMock(choices=[choice])
    return client


def _generate(conn, tenant_id):
    return generate_recommendation(
        conn, tenant_id, _CONTROL_UUID,
        triggered_by_user_id=_TRIGGER_USER, triggered_by_role="compliance_lead",
    )


@pytest.fixture
def ker401_seed(db_connection, tenant_a_id):
    """Seed one catalogue control, one context record, and one scored evidence link.

    The control is platform-global (no tenant column); the record and link are
    inserted under Tenant A's context (both tables are FORCE row-level
    secured). relevance_score 0.9 makes the deterministic verdict met/high, so
    assertions are unambiguous. Cleans up everything it created plus any
    recommendations generated for the control.
    """
    with db_connection.transaction():
        db_connection.execute(
            """INSERT INTO compliance_controls
               (control_id, framework, control_ref, category, title,
                obligation_text, entity_types, is_active)
               VALUES (%s, 'NIS2', 'KER401-TEST', 'governance',
                       'Incident response runbook', 'Test obligation.',
                       %s, TRUE)
               ON CONFLICT (control_id) DO NOTHING""",
            [_CONTROL_UUID, ["essential"]],
        )
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        db_connection.execute(
            """INSERT INTO context_records
               (record_id, tenant_id, source_system, record_type, title, body)
               VALUES (%s, %s, 'confluence', 'policy',
                       'IR runbook', 'Formal incident response runbook, v4.')""",
            [_RECORD_UUID, str(tenant_a_id)],
        )
        db_connection.execute(
            """INSERT INTO control_evidence_links
               (link_id, control_id, record_id, linked_by, linked_at, relevance_score)
               VALUES (%s, %s, %s, 'integration-test', now(), 0.9)""",
            [_LINK_UUID, _CONTROL_UUID, _RECORD_UUID],
        )

    yield

    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        db_connection.execute(
            "DELETE FROM control_evidence_links WHERE link_id = %s", [_LINK_UUID]
        )
        db_connection.execute(
            "DELETE FROM recommendations WHERE control_id = %s", [_CONTROL_UUID]
        )
    with db_connection.transaction():
        db_connection.execute(
            "DELETE FROM compliance_controls WHERE control_id = %s", [_CONTROL_UUID]
        )


def _count(conn, tenant_id, sql: str, params: list) -> int:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        row = conn.execute(sql, params).fetchone()
    return int(row[0])


@pytest.mark.integration
def test_generation_commits_recommendation_log_and_ledger_together(
    db_connection, tenant_a_id, ker401_seed, monkeypatch
):
    monkeypatch.setenv("KERNO_LLM_MODEL", "mistral-large-latest")
    with patch(
        "src.services.recommendation_service.get_llm_client",
        return_value=_mock_llm_client(),
    ):
        with db_connection.transaction():
            result = _generate(db_connection, tenant_a_id)

    assert result.status == "met"
    assert result.rationale == _LLM_RATIONALE
    assert result.input_snapshot["rationale_source"] == "llm"
    assert result.input_snapshot["llm_opinion"] == {"status": "met", "confidence": 0.8}

    assert _count(
        db_connection, tenant_a_id,
        "SELECT count(*) FROM recommendations WHERE control_id = %s AND is_superseded = FALSE",
        [_CONTROL_UUID],
    ) == 1
    assert _count(
        db_connection, tenant_a_id,
        "SELECT count(*) FROM ai_decision_log WHERE control_id = %s "
        "AND model_version LIKE 'evidence-rules-v1+%%'",
        [_CONTROL_UUID],
    ) == 1
    assert _count(
        db_connection, tenant_a_id,
        "SELECT count(*) FROM audit_log WHERE action_type = 'recommendation_generated' "
        "AND actor_id = %s",
        [_TRIGGER_USER],
    ) == 1


@pytest.mark.integration
def test_failed_generation_rolls_back_all_three_records(
    db_connection, tenant_a_id, ker401_seed
):
    with patch(
        "src.services.recommendation_service.emit_decision_log",
        side_effect=RuntimeError("simulated decision-log failure"),
    ):
        with pytest.raises(RuntimeError):
            with db_connection.transaction():
                _generate(db_connection, tenant_a_id)

    # The recommendation INSERT ran before the failure — the rollback must
    # take it down with the never-written log and ledger rows (AC-5).
    assert _count(
        db_connection, tenant_a_id,
        "SELECT count(*) FROM recommendations WHERE control_id = %s",
        [_CONTROL_UUID],
    ) == 0
    assert _count(
        db_connection, tenant_a_id,
        "SELECT count(*) FROM ai_decision_log WHERE control_id = %s",
        [_CONTROL_UUID],
    ) == 0


@pytest.mark.integration
def test_template_fallback_persists_on_live_connection(
    db_connection, tenant_a_id, ker401_seed
):
    def _llm_down():
        raise RuntimeError("LLM unreachable")

    with patch(
        "src.services.recommendation_service.get_llm_client", side_effect=_llm_down
    ):
        with db_connection.transaction():
            result = _generate(db_connection, tenant_a_id)

    assert result.input_snapshot["rationale_source"] == "template"
    assert result.rationale  # deterministic prose, never empty
    assert _count(
        db_connection, tenant_a_id,
        "SELECT count(*) FROM ai_decision_log WHERE control_id = %s "
        "AND model_version = 'evidence-rules-v1+template'",
        [_CONTROL_UUID],
    ) == 1
