"""KER-201 integration tests — the real nightly bias recalculation against a live database.

Proves the learning loop closes end to end: seed a human override for Tenant A, run
the actual nightly batch through the same connection factory the cron entrypoint uses,
then assert that (1) the retrieval_bias vector moved in the direction of the human's
correction, (2) get_similar_controls now ranks the corrected control above the AI's
original recommendation, and (3) the run appended a "bias_recalculated" entry to the
KER-107 audit ledger. Requires DATABASE_URL with all migrations applied; seeded rows
are removed by the shared conftest teardown.

Run: pytest tests/integration/test_ker201_bias_recalculation.py -m integration -v
"""

from __future__ import annotations

import json
import uuid

import pytest

from config.constants import EMBEDDING_DIMENSION, LEARNING_RATE
# _cron_transaction is imported rather than reimplemented so the batch runs through
# the exact connection factory the production cron entrypoint uses.
from src.scheduler.nightly_bias_recalculation import (
    PLATFORM_SCHEDULER_TENANT_ID,
    _cron_transaction,
    run_nightly_bias_recalculation,
)
from src.services.bias_recalculation_service import coerce_vector
from src.services.retrieval_service import get_similar_controls

_SOURCE_CONTROL = "ker201_source_control"
_TARGET_CONTROL = "ker201_target_control"

# Source and target embeddings are perpendicular unit vectors; the query vector is a
# unit vector deliberately closer to the source (cosine similarity 0.8 vs 0.6), so the
# unbiased ranking prefers the source. One senior override (weight 1.0) from a zero
# starting bias yields bias = LEARNING_RATE * (target - source), which under
# BIAS_INJECTION_COEFFICIENT = 1.0 shifts the calibrated distances by ±LEARNING_RATE —
# more than the 0.2 unbiased gap — flipping the ranking to prefer the target.
_VEC_SOURCE = [1.0, 0.0] + [0.0] * (EMBEDDING_DIMENSION - 2)
_VEC_TARGET = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSION - 2)
_QUERY_VECTOR = [0.8, 0.6] + [0.0] * (EMBEDDING_DIMENSION - 2)


class _TenantScopedSession:
    """Minimal authenticated-session stand-in exposing resolve_tenant_id()."""

    def __init__(self, tenant_id) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self):
        return self._tenant_id


def _fmt(values: list[float]) -> str:
    return "[" + ",".join(str(v) for v in values) + "]"


def _run_real_batch() -> dict:
    """Run the production batch exactly as the cron entrypoint would."""
    admin_session = _TenantScopedSession(PLATFORM_SCHEDULER_TENANT_ID)
    return run_nightly_bias_recalculation(_cron_transaction, admin_session)


def _rank_of(results: list[dict], control_id: str) -> int:
    return next(i for i, r in enumerate(results) if r["control_id"] == control_id)


@pytest.fixture
def ker201_override_seed(db_connection, tenant_a_id):
    """Seed Tenant A with source/target control embeddings and one senior override.

    The override says: the AI recommended _SOURCE_CONTROL, the human corrected it to
    _TARGET_CONTROL. Committed so the batch's own connections can see the rows.
    Tenant A has no retrieval_bias row at this point (the fixture asserts that), so
    the recalculation starts from the zero-vector seed. Cleanup is handled by the
    shared conftest teardown, which deletes all Tenant A/B rows including audit_log.
    """
    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        for control_id, vec in ((_SOURCE_CONTROL, _VEC_SOURCE), (_TARGET_CONTROL, _VEC_TARGET)):
            db_connection.execute(
                "INSERT INTO tenant_embeddings (tenant_id, control_id, embedding) "
                "VALUES (%s, %s, %s::vector)",
                [str(tenant_a_id), control_id, _fmt(vec)],
            )
        db_connection.execute(
            """
            INSERT INTO overrides
                (tenant_id, reviewer_id, reviewer_role, action_type,
                 original_control_id, corrected_control_id, reviewer_confidence_weight)
            VALUES (%s, %s, 'vciso', 'edit', %s, %s, 1.0)
            """,
            [str(tenant_a_id), str(uuid.uuid4()), _SOURCE_CONTROL, _TARGET_CONTROL],
        )
        row = db_connection.execute(
            "SELECT count(*) FROM retrieval_bias WHERE tenant_id = %s",
            [str(tenant_a_id)],
        ).fetchone()
        assert row[0] == 0, "Tenant A must start uncalibrated for this test"
    yield


@pytest.mark.integration
def test_batch_moves_bias_vector_in_expected_direction(
    db_connection, tenant_a_id, ker201_override_seed
):
    summary = _run_real_batch()
    assert summary["failure_count"] == 0, "no tenant may fail during the batch"

    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        row = db_connection.execute(
            "SELECT bias_vector, override_count, last_recalculated_at "
            "FROM retrieval_bias WHERE tenant_id = %s",
            [str(tenant_a_id)],
        ).fetchone()

    assert row is not None, "the batch must create Tenant A's retrieval_bias row"
    bias_vector = coerce_vector(row[0])
    assert len(bias_vector) == EMBEDDING_DIMENSION
    # From a zero seed: bias = LEARNING_RATE * 1.0 * (target - source) — negative along
    # the AI's original direction, positive along the human's correction.
    assert bias_vector[0] == pytest.approx(-LEARNING_RATE)
    assert bias_vector[1] == pytest.approx(LEARNING_RATE)
    assert row[1] == 1, "override_count must record the one processed override"
    assert row[2] is not None, "last_recalculated_at must be stamped"


@pytest.mark.integration
def test_recalculated_ranking_prefers_the_corrected_control(
    db_connection, tenant_a_id, ker201_override_seed
):
    session = _TenantScopedSession(tenant_a_id)

    with db_connection.transaction():
        before = get_similar_controls(session, db_connection, _QUERY_VECTOR)
    assert _rank_of(before, _SOURCE_CONTROL) < _rank_of(before, _TARGET_CONTROL), (
        "before recalculation the AI's original recommendation must rank higher"
    )

    _run_real_batch()

    with db_connection.transaction():
        after = get_similar_controls(session, db_connection, _QUERY_VECTOR)
    assert _rank_of(after, _TARGET_CONTROL) < _rank_of(after, _SOURCE_CONTROL), (
        "after recalculation the human-corrected control must outrank the original"
    )


@pytest.mark.integration
def test_recalculation_appends_bias_recalculated_ledger_entry(
    db_connection, tenant_a_id, ker201_override_seed
):
    _run_real_batch()

    with db_connection.transaction():
        db_connection.execute(
            "SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)]
        )
        rows = db_connection.execute(
            "SELECT actor_role, object_type, object_id, after_state FROM audit_log "
            "WHERE tenant_id = %s AND action_type = 'bias_recalculated'",
            [str(tenant_a_id)],
        ).fetchall()

    assert len(rows) == 1, "exactly one ledger entry per real recalculation"
    actor_role, object_type, object_id, after_state_raw = rows[0]
    assert actor_role == "system"
    assert object_type == "bias_vector"
    assert object_id == str(tenant_a_id)
    # after_state arrives as dict (jsonb) or JSON text depending on column type.
    after_state = (
        after_state_raw if isinstance(after_state_raw, dict) else json.loads(after_state_raw)
    )
    assert after_state["override_count"] == 1
    assert after_state["dimensions"] == EMBEDDING_DIMENSION
    assert after_state["updated_at"]
