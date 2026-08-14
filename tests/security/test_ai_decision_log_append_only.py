"""Migration 023 — database-level guards on the ai_decision_log retention window.

What:  proves the database itself rejects UPDATE, rejects TRUNCATE, rejects
       DELETE of a record still inside the retention window, and permits DELETE
       of a record past it — the last being the nightly prune's path, exercised
       through the real service function rather than a spy.
Why:   §15 KER-405 #2. Retention was a promise made by the prune job's Python,
       and nothing stopped an application-path UPDATE or an early DELETE.
       §17 Ticket D moves the promise into the schema.
How:   pytest tests/security/test_ai_decision_log_append_only.py -m integration -v

What these tests do NOT prove: tamper-evidence. The application connects as the
table owner (§17 ticket C2 is held) and can disable every trigger asserted here
in one statement, and unlike audit_log there is no hash chain behind them. These
guards stop accidental and application-path mutation. They do not stop an
operator, and nothing detects one afterwards.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from config.constants import AI_DECISION_LOG_RETENTION_DAYS
from src.services.ai_decision_log_service import prune_old_logs

_MODEL_ID = "test-model/1"


def _plant(conn, tenant_id, control_id: str, age_days: int) -> str:
    """Insert one decision record aged by the given number of days; return its id.

    The insert trigger stamps created_at = now() precisely to stop a caller
    backdating a row past the floor and deleting it next statement, so producing
    an aged row means switching that trigger off on purpose.
    """
    correlation_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    # Required, not defensive: the table is FORCE RLS and the policy casts
    # current_setting('app.current_tenant_id') to uuid. A pooled connection that
    # has previously run SET LOCAL comes back with the setting as '' rather than
    # unset, so an insert without a context raises on the cast.
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
    conn.execute(
        "ALTER TABLE ai_decision_log DISABLE TRIGGER ai_decision_log_server_timestamp"
    )
    try:
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
    finally:
        conn.execute(
            "ALTER TABLE ai_decision_log ENABLE TRIGGER ai_decision_log_server_timestamp"
        )
    return correlation_id


@pytest.mark.integration
def test_update_is_rejected(db_connection, tenant_a_id):
    with db_connection.transaction():
        correlation_id = _plant(db_connection, tenant_a_id, "ker023-update", age_days=1)
    # FORCE RLS hides rows from a context-less statement, which would match zero
    # rows and never fire the row trigger — the test would pass for the wrong
    # reason. Set the context so the row is actually visible to the UPDATE.
    with pytest.raises(Exception, match="append-only"):
        with db_connection.transaction():
            db_connection.execute(
                "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
            )
            db_connection.execute(
                "UPDATE ai_decision_log SET output_status = 'gap' WHERE correlation_id = %s",
                [correlation_id],
            )


@pytest.mark.integration
def test_update_is_rejected_even_for_an_expired_row(db_connection, tenant_a_id):
    # The window governs deletion only. A record's content is never editable,
    # at any age.
    with db_connection.transaction():
        correlation_id = _plant(
            db_connection, tenant_a_id, "ker023-update-old",
            age_days=AI_DECISION_LOG_RETENTION_DAYS + 30,
        )
    with pytest.raises(Exception, match="append-only"):
        with db_connection.transaction():
            db_connection.execute(
                "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
            )
            db_connection.execute(
                "UPDATE ai_decision_log SET confidence_score = 0.1 WHERE correlation_id = %s",
                [correlation_id],
            )


@pytest.mark.integration
def test_delete_inside_the_retention_window_is_rejected(db_connection, tenant_a_id):
    with db_connection.transaction():
        correlation_id = _plant(db_connection, tenant_a_id, "ker023-fresh", age_days=1)
    with pytest.raises(Exception, match="append-only"):
        with db_connection.transaction():
            db_connection.execute(
                "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
            )
            db_connection.execute(
                "DELETE FROM ai_decision_log WHERE correlation_id = %s", [correlation_id]
            )


@pytest.mark.integration
def test_truncate_is_rejected(db_connection, tenant_a_id):
    # Row triggers never fire for TRUNCATE, so without the statement-level guard
    # one statement would empty the table and leave it looking append-only.
    with pytest.raises(Exception, match="append-only"):
        with db_connection.transaction():
            db_connection.execute("TRUNCATE ai_decision_log")


@pytest.mark.integration
def test_backdating_a_row_on_insert_does_not_buy_an_early_delete(db_connection, tenant_a_id):
    # The two-statement walk-around the insert stamp exists to close: claim a
    # created_at past the floor, then delete the row as "expired".
    with db_connection.transaction():
        correlation_id = str(uuid.uuid4())
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        db_connection.execute(
            """
            INSERT INTO ai_decision_log
                (correlation_id, tenant_id, control_id, evidence_ids,
                 input_snapshot_hash, output_status, confidence_score,
                 rationale_extract, model_version, created_at)
            VALUES (%s, %s, %s, %s, %s, 'met', 0.9, 'planted', %s, %s)
            """,
            [correlation_id, str(tenant_a_id), "ker023-backdated", ["rec-x"], "c" * 64,
             _MODEL_ID, datetime.now(timezone.utc) - timedelta(days=3650)],
        )
    with pytest.raises(Exception, match="append-only"):
        with db_connection.transaction():
            db_connection.execute(
                "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
            )
            db_connection.execute(
                "DELETE FROM ai_decision_log WHERE correlation_id = %s", [correlation_id]
            )


@pytest.mark.integration
def test_the_real_prune_deletes_expired_rows_and_spares_the_rest(db_connection, tenant_a_id):
    # The permitted-delete half, driven through the actual service function. If
    # someone later "hardens" the WHEN clause into an unconditional block, this
    # is the test that fails — and it fails as the nightly job would.
    with db_connection.transaction():
        expired = _plant(
            db_connection, tenant_a_id, "ker023-expired",
            age_days=AI_DECISION_LOG_RETENTION_DAYS + 10,
        )
        retained = _plant(
            db_connection, tenant_a_id, "ker023-retained",
            age_days=AI_DECISION_LOG_RETENTION_DAYS - 10,
        )

    with db_connection.transaction():
        deleted_count = prune_old_logs(db_connection, tenant_a_id)

    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        surviving = {
            row[0]
            for row in db_connection.execute(
                "SELECT correlation_id::text FROM ai_decision_log WHERE control_id IN "
                "('ker023-expired', 'ker023-retained')"
            ).fetchall()
        }
    assert deleted_count == 1
    assert expired not in surviving
    assert retained in surviving


@pytest.mark.integration
def test_sql_retention_window_matches_the_python_constant(db_connection):
    # The window is hardcoded in migration 023 and cannot import the constant.
    # Editing AI_DECISION_LOG_RETENTION_DAYS without a matching migration would
    # desync the prune from the trigger, so the drift fails here instead.
    with db_connection.transaction():
        row = db_connection.execute(
            """
            SELECT pg_get_triggerdef(oid)
            FROM pg_trigger
            WHERE tgrelid = 'ai_decision_log'::regclass
              AND tgname = 'ai_decision_log_retain_window'
            """
        ).fetchone()
    assert row is not None, "the retention trigger is missing — is migration 023 applied?"
    assert f"'{AI_DECISION_LOG_RETENTION_DAYS} days'" in row[0]
