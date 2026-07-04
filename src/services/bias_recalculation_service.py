"""Nightly bias recalculation — updates a tenant's personalised search weights.

Plain-English summary
---------------------
This service implements the mathematical heart of Kerno's learning loop.

Each night, for every tenant that has received at least one human override since
the last calculation, this service asks: "Given everything this company's
compliance engineers have corrected so far, in which direction should we nudge
future search results?" The answer is a vector of numbers — the retrieval bias
vector — stored in the database and injected into every future search.

The update formula (LEARNING_PIPELINE_SPEC.md Section 5.2) works like this:

  updated_bias = (decay_factor × current_bias)
               + learning_rate
               × sum(reviewer_confidence_weight × (target_vector − source_vector))
                 for each override

  Where:
    decay_factor                 — how much historical calibration is preserved
                                   (DECAY_FACTOR = 0.85: the system changes its
                                   mind slowly — CLAUDE.md §2.3, spec "alpha")
    learning_rate                — complement of decay_factor; controls how fast
                                   new corrections are absorbed (LEARNING_RATE =
                                   1 - DECAY_FACTOR)
    reviewer_confidence_weight   — how much weight this reviewer's correction gets
                                   (1.0 for vCISO/fCISO, 0.5 for internal admin)
    target_control_vector        — the embedding of the control the human chose
    source_recommendation_vector — the embedding of what the AI had recommended

Two functions live here:
  1. ``recalculate_retrieval_bias`` — pure function, no database. Takes the
     current vector and a list of overrides, returns the new vector. Testable
     without a database connection.
  2. ``persist_retrieval_bias`` — writes the result to the database. Requires
     the caller to have already set the tenant context.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/services/test_bias_recalculation_service.py -v

The test suite has 8 cases including a numerically verified worked example
from LEARNING_PIPELINE_SPEC.md §5.2, the new-tenant zero-vector seed, and
the no-overrides pass-through.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from config.constants import DECAY_FACTOR, LEARNING_RATE

# TODO (post-Sprint 1): replace stub with full bias vector recalculation
# Inputs: all overrides since last_recalculated_at
# Algorithm: see LEARNING_PIPELINE_SPEC.md §5
# Output: updated retrieval_bias row per tenant
# Sprint 1 (KER-114) runs run_recalculation_stub() in
# src/scheduler/nightly_bias_recalculation.py, which logs and audits WITHOUT
# calling the functions below. This module is the ready-made full
# implementation the stub hands over to when the loop is activated.

# Type alias: a vector is a list of floats with a fixed length determined
# by the embedding model. The length is enforced at the model layer (RetrievalBias)
# and checked here at the level of the mathematical operation.
Vector = list[float]


def recalculate_retrieval_bias(
    current_retrieval_bias_vector: Vector,
    overrides: list[dict],
) -> Vector:
    """Return an updated retrieval bias vector that incorporates the latest overrides.

    Pure function: takes the current bias vector and a list of override records,
    applies the weighted-exponential-moving-average formula from the spec, and
    returns the new vector. Never reads or writes the database. Safe to unit-test
    without any database connection. (LEARNING_PIPELINE_SPEC.md Section 5.2.)

    Each override dict must contain:
      ``reviewer_confidence_weight``  — float, weight for this reviewer's correction
      ``target_control_vector``       — list[float], embedding of the human's choice
      ``source_recommendation_vector`` — list[float], embedding of the AI's recommendation
    """
    if not current_retrieval_bias_vector:
        # New tenant: no historical calibration yet. If there are no overrides
        # either, there is nothing to compute — return the empty vector unchanged.
        # If there are overrides, seed a zero vector whose length matches the
        # first override's embedding so the formula has a neutral starting point.
        if not overrides:
            return []
        dimension_count = len(overrides[0]["target_control_vector"])
        current_retrieval_bias_vector = [0.0] * dimension_count
    else:
        _require_matching_dimensions(
            current_retrieval_bias_vector,
            "current_retrieval_bias_vector",
        )
    if not overrides:
        return list(current_retrieval_bias_vector)
    weighted_correction_sum = _sum_weighted_corrections(overrides)
    return _apply_decay_formula(current_retrieval_bias_vector, weighted_correction_sum)


def persist_retrieval_bias(
    conn,
    tenant_id: uuid.UUID,
    updated_retrieval_bias_vector: Vector,
    override_count: int,
) -> None:
    """Write the recalculated bias vector to the database for the given tenant.

    Requires that the caller has already set the tenant context on ``conn``
    (via ``resolve_and_set_tenant_context``). Uses an upsert — one row per
    tenant, refreshed on each nightly run. (KER-114.)
    """
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO retrieval_bias
            (tenant_id, bias_vector, override_count, last_recalculated_at, created_at)
        VALUES (:tenant_id, :bias_vector, :override_count, :now, :now)
        ON CONFLICT (tenant_id)
        DO UPDATE SET
            bias_vector = EXCLUDED.bias_vector,
            override_count = EXCLUDED.override_count,
            last_recalculated_at = EXCLUDED.last_recalculated_at
        """,
        {
            "tenant_id": str(tenant_id),
            "bias_vector": updated_retrieval_bias_vector,
            "override_count": override_count,
            "now": now,
        },
    )


def _sum_weighted_corrections(overrides: list[dict]) -> Vector:
    """Add up each override's weighted correction vector.

    For each override, the correction direction is (target − source), scaled by
    the reviewer's confidence weight. This function accumulates those scaled
    direction vectors into a single sum. (LEARNING_PIPELINE_SPEC.md Section 5.2.)
    """
    first_override = overrides[0]
    target_control_vector = first_override["target_control_vector"]
    _require_matching_dimensions(target_control_vector, "target_control_vector")
    dimension_count = len(target_control_vector)
    accumulated_correction = [0.0] * dimension_count
    for override in overrides:
        target = override["target_control_vector"]
        source_recommendation_vector = override["source_recommendation_vector"]
        reviewer_confidence_weight = override["reviewer_confidence_weight"]
        override_error_vector = _subtract_vectors(target, source_recommendation_vector)
        accumulated_correction = _add_scaled_vector(
            accumulated_correction,
            override_error_vector,
            reviewer_confidence_weight,
        )
    return accumulated_correction


def _apply_decay_formula(
    current_retrieval_bias_vector: Vector,
    weighted_correction_sum: Vector,
) -> Vector:
    """Blend the existing calibration with the new corrections using the decay factor.

    Implements: updated = (decay × current) + (1 − decay) × correction_sum
    The decay factor controls how quickly the system forgets old calibration.
    (LEARNING_PIPELINE_SPEC.md Section 5.2.)
    """
    return [
        (DECAY_FACTOR * current) + (LEARNING_RATE * correction)
        for current, correction in zip(
            current_retrieval_bias_vector, weighted_correction_sum
        )
    ]


def _subtract_vectors(a: Vector, b: Vector) -> Vector:
    """Return the element-wise difference (a − b) of two equal-length vectors."""
    return [x - y for x, y in zip(a, b)]


def _add_scaled_vector(
    accumulator: Vector, vector: Vector, scale: float
) -> Vector:
    """Return accumulator + (scale × vector), element-wise."""
    return [acc + scale * v for acc, v in zip(accumulator, vector)]


def _require_matching_dimensions(vector: Vector, field_name: str) -> None:
    """Raise ValueError if the vector is empty or not a list of numbers.

    Protects the formula from silently producing a zero-length or mixed-type
    result. Does not enforce a specific dimension count here — that is the
    database model's job — but refuses to proceed with an obviously malformed
    input.
    """
    if not isinstance(vector, list) or not vector:
        raise ValueError(
            f"{field_name} must be a non-empty list of floats; "
            f"received {type(vector).__name__}."
        )
    if not all(isinstance(v, (int, float)) for v in vector):
        raise ValueError(
            f"All elements of {field_name} must be numeric."
        )
