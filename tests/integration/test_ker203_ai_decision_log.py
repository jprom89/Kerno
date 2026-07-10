"""KER-203 integration tests — the AI-decision log against a live database.

Proves the retention loop end to end: a real map_control run (LLM mocked at the
client factory, everything else live) writes its ai_decision_log row in the
same transaction as the recommendation; the row is queryable through
query_decision_logs with filters; the input_snapshot_hash matches a SHA-256
re-derivation from the stored recommendation snapshot; and prune_old_logs
deletes rows outside the retention window while retaining rows inside it.
Requires DATABASE_URL with migration 020 applied; seeded rows for tenants A/B
are removed by the shared conftest teardown, and this file's fixture clears
its own ai_decision_log rows.

Run: pytest tests/integration/test_ker203_ai_decision_log.py -m integration -v
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from config.constants import AI_DECISION_LOG_RETENTION_DAYS
from src.services.ai_decision_log_service import prune_old_logs, query_decision_logs
from src.services.mapping_service import ControlInput, EvidenceInput, map_control

_MODEL_ID = "mistral-large-latest"
_CONTROL = ControlInput(
    control_id="ker203-control-001",
    framework="NIS2",
    control_ref="Art.21(2)(a)",
    title="Risk analysis policy",
    description="The entity maintains a documented risk analysis policy.",
)
_EVIDENCE = [
    EvidenceInput(
        record_id="ker203-rec-001",
        title="Risk policy document",
        body="Policy v3 approved by the board.",
        source_system="confluence",
    )
]
_LLM_RESPONSE = json.dumps(
    {
        "status": "met",
        "confidence": 0.9,
        "evidence_ids": ["ker203-rec-001"],
        "reasoning": "The policy document directly evidences the control.",
        "gaps": [],
    }
)

# Rows planted either side of the retention boundary for the prune test.
_DAYS_OUTSIDE_WINDOW = AI_DECISION_LOG_RETENTION_DAYS + 10
_DAYS_INSIDE_WINDOW = AI_DECISION_LOG_RETENTION_DAYS - 10


def _mock_llm_client() -> MagicMock:
    client = MagicMock()
    client.chat.complete.return_value.choices = [
        MagicMock(message=MagicMock(content=_LLM_RESPONSE))
    ]
    return client


def _run_mapping(conn, tenant_id):
    """Run map_control with the LLM mocked; all database writes are real."""
    with patch("src.services.mapping_service.get_llm_client", return_value=_mock_llm_client()), \
         patch.dict("os.environ", {"KERNO_LLM_MODEL": _MODEL_ID}):
        return map_control(conn, tenant_id, _CONTROL, _EVIDENCE)


def _insert_decision_row(conn, tenant_id, control_id: str, created_at: datetime) -> str:
    """Plant one ai_decision_log row with an explicit created_at (for prune tests)."""
    correlation_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO ai_decision_log
            (correlation_id, tenant_id, control_id, evidence_ids,
             input_snapshot_hash, output_status, confidence_score,
             rationale_extract, model_version, created_at)
        VALUES (%s, %s, %s, %s, %s, 'met', 0.9, 'planted', %s, %s)
        """,
        [correlation_id, str(tenant_id), control_id, ["rec-x"],
         "c" * 64, _MODEL_ID, created_at],
    )
    return correlation_id


@pytest.fixture
def ker203_clean_log(db_connection, tenant_a_id):
    """Ensure Tenant A starts and ends with no ai_decision_log rows.

    The shared conftest teardown predates migration 020 and does not know this
    table, so this fixture owns the cleanup for both sides of the test.
    """
    def _wipe():
        with db_connection.transaction():
            db_connection.execute(
                "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
            )
            db_connection.execute(
                "DELETE FROM ai_decision_log WHERE tenant_id = %s", [str(tenant_a_id)]
            )

    _wipe()
    yield
    _wipe()


@pytest.mark.integration
def test_recommendation_write_produces_queryable_decision_log_row(
    db_connection, tenant_a_id, ker203_clean_log
):
    with db_connection.transaction():
        result = _run_mapping(db_connection, tenant_a_id)

    with db_connection.transaction():
        entries = query_decision_logs(
            db_connection, tenant_a_id, control_id=_CONTROL.control_id
        )
    assert len(entries) == 1, "exactly one decision record per recommendation"
    entry = entries[0]
    assert entry.output_status == "met"
    assert entry.confidence_score == pytest.approx(0.9)
    assert entry.evidence_ids == ["ker203-rec-001"]
    assert entry.model_version == _MODEL_ID
    assert entry.created_at is not None
    assert result.recommendation_id  # the recommendation itself was written

    # The stored hash re-derives from the recommendation's stored snapshot —
    # the regulator verification procedure from the runbook, mechanised.
    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        row = db_connection.execute(
            "SELECT input_snapshot FROM recommendations WHERE recommendation_id = %s",
            [result.recommendation_id],
        ).fetchone()
    snapshot = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    rederived = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert entry.input_snapshot_hash == rederived


@pytest.mark.integration
def test_query_filters_apply_on_live_rows(db_connection, tenant_a_id, ker203_clean_log):
    now = datetime.now(timezone.utc)
    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        _insert_decision_row(db_connection, tenant_a_id, "ker203-filter-a", now)
        _insert_decision_row(
            db_connection, tenant_a_id, "ker203-filter-b", now - timedelta(days=30)
        )

    with db_connection.transaction():
        by_control = query_decision_logs(
            db_connection, tenant_a_id, control_id="ker203-filter-a"
        )
        recent_only = query_decision_logs(
            db_connection, tenant_a_id, after=now - timedelta(days=1)
        )
    assert [e.control_id for e in by_control] == ["ker203-filter-a"]
    assert [e.control_id for e in recent_only] == ["ker203-filter-a"]


@pytest.mark.integration
def test_prune_deletes_outside_window_and_retains_inside(
    db_connection, tenant_a_id, ker203_clean_log
):
    now = datetime.now(timezone.utc)
    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        expired_id = _insert_decision_row(
            db_connection, tenant_a_id, "ker203-expired",
            now - timedelta(days=_DAYS_OUTSIDE_WINDOW),
        )
        retained_id = _insert_decision_row(
            db_connection, tenant_a_id, "ker203-retained",
            now - timedelta(days=_DAYS_INSIDE_WINDOW),
        )

    with db_connection.transaction():
        deleted_count = prune_old_logs(db_connection, tenant_a_id)

    assert deleted_count == 1, "exactly the expired row is deleted"
    with db_connection.transaction():
        remaining = query_decision_logs(db_connection, tenant_a_id)
    remaining_ids = {e.correlation_id for e in remaining}
    assert expired_id not in remaining_ids, "row outside the window must be gone"
    assert retained_id in remaining_ids, "row inside the window must be retained"
