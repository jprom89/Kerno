"""Retrieval service — similarity search over tenant_embeddings (controls) and context_records (evidence).

Control search (get_similar_controls) applies a per-tenant bias vector via pgvector's negative
inner product operator; evidence record search (retrieve_similar_records) uses cosine distance
against the context_records.embedding column added by migration 015.

Why:   this is where each tenant's learned calibration actually changes what the
       product recommends (query pattern: LEARNING_PIPELINE_SPEC.md Section 5.3).
How:   pytest tests/unit/services/test_retrieval_service.py -v
       (live ranking proof: tests/integration/test_ker201_bias_recalculation.py)
"""

from __future__ import annotations

import logging
import uuid

from config.constants import (
    BIAS_INJECTION_COEFFICIENT,
    MAX_SIMILAR_CONTROLS_RETURNED,
    MAX_SIMILAR_RECORDS_RETURNED,
)
from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError
from src.services.bias_recalculation_service import coerce_vector
from src.services.tenant_context import resolve_and_set_tenant_context

logger = logging.getLogger(__name__)

__all__ = ["get_similar_controls", "retrieve_similar_records", "TenantContextMissingError"]

# ---------------------------------------------------------------------------
# SQL for control retrieval (tenant_embeddings)
#
# The <#> operator returns the negative inner product, so
#   cosine_distance + BIAS * (bias <#> embedding)
# is equivalent to
#   cosine_distance - BIAS * dot_product(bias, embedding)
# which lowers calibrated_distance for embeddings well-aligned with the
# tenant's bias direction, raising their rank. (LEARNING_PIPELINE_SPEC §4.1)
# ---------------------------------------------------------------------------

_BIASED_SIMILARITY_QUERY = """
SELECT
    control_id,
    (embedding <=> :query_vector) + :bias_coefficient * (:bias_vector <#> embedding)
    AS calibrated_distance
FROM tenant_embeddings
WHERE tenant_id = :tenant_id
ORDER BY calibrated_distance ASC
LIMIT :result_limit
"""

_UNBIASED_SIMILARITY_QUERY = """
SELECT
    control_id,
    (embedding <=> :query_vector) AS calibrated_distance
FROM tenant_embeddings
WHERE tenant_id = :tenant_id
ORDER BY calibrated_distance ASC
LIMIT :result_limit
"""

# ---------------------------------------------------------------------------
# SQL for evidence record retrieval (context_records)
#
# embedding IS NOT NULL skips rows that have not yet been embedded; is_deleted
# skips soft-deleted records. Both rows are filtered before the ORDER BY so
# the LIMIT applies only to embeddable, active records.
# ---------------------------------------------------------------------------

_CONTEXT_SIMILARITY_QUERY = """
SELECT
    record_id,
    title,
    body,
    embedding <=> :query_vector AS distance
FROM context_records
WHERE tenant_id = :tenant_id
  AND embedding IS NOT NULL
  AND is_deleted = FALSE
ORDER BY embedding <=> :query_vector
LIMIT :result_limit
"""


def get_similar_controls(session, conn, query_vector: list[float]) -> list[dict]:
    """Return top controls most similar to query_vector with per-tenant bias applied when available.

    Resolves tenant from session, activates RLS context, then runs either the biased or unbiased
    similarity query depending on whether a bias vector row exists for this tenant.
    Falls back silently to unbiased search if the bias row is absent or empty.
    Raises TenantContextMissingError if the session cannot supply a valid UUIDv4.
    """
    tenant_id = resolve_and_set_tenant_context(session, conn)
    bias_vector = _fetch_tenant_bias_vector(conn, tenant_id)
    if not bias_vector:
        logger.info(
            "No bias vector found for tenant %s; using unbiased similarity search.",
            tenant_id,
        )
        return _run_unbiased_query(conn, tenant_id, query_vector)
    return _run_biased_query(conn, tenant_id, query_vector, bias_vector)


def retrieve_similar_records(
    conn,
    tenant_id,
    query_vector: list[float],
    limit: int = MAX_SIMILAR_RECORDS_RETURNED,
) -> list[dict]:
    """Return the closest limit context_records to query_vector, ordered by cosine distance ascending.

    Sets tenant context before querying; skips rows where embedding IS NULL or is_deleted = TRUE.
    Each result dict contains record_id, title, body, and distance.
    Raises TenantContextMissingError if tenant_id is None or not a valid UUIDv4.
    """
    set_tenant_context(conn, tenant_id)
    rows = conn.execute(
        _CONTEXT_SIMILARITY_QUERY,
        {
            "query_vector": query_vector,
            "tenant_id": str(tenant_id),
            "result_limit": limit,
        },
    ).fetchall()
    return [_row_to_record_dict(row) for row in rows]


def _fetch_tenant_bias_vector(conn, tenant_id: uuid.UUID) -> list[float] | None:
    """Return the tenant's bias vector from retrieval_bias, or None if absent.

    Returns None (not an empty list) when no row exists, so callers can distinguish
    "not yet calibrated" from a genuine zero vector. Treat empty list as uncalibrated too (§4.2).
    The stored pgvector value arrives as text and is coerced to a float list (KER-201).
    """
    row = conn.execute(
        "SELECT bias_vector FROM retrieval_bias WHERE tenant_id = :tenant_id",
        {"tenant_id": str(tenant_id)},
    ).fetchone()
    if row is None:
        return None
    return coerce_vector(row[0])


def _run_biased_query(
    conn,
    tenant_id: uuid.UUID,
    query_vector: list[float],
    bias_vector: list[float],
) -> list[dict]:
    rows = conn.execute(
        _BIASED_SIMILARITY_QUERY,
        {
            "query_vector": query_vector,
            "bias_vector": bias_vector,
            "bias_coefficient": BIAS_INJECTION_COEFFICIENT,
            "tenant_id": str(tenant_id),
            "result_limit": MAX_SIMILAR_CONTROLS_RETURNED,
        },
    ).fetchall()
    return [{"control_id": row[0], "calibrated_distance": row[1]} for row in rows]


def _run_unbiased_query(
    conn,
    tenant_id: uuid.UUID,
    query_vector: list[float],
) -> list[dict]:
    rows = conn.execute(
        _UNBIASED_SIMILARITY_QUERY,
        {
            "query_vector": query_vector,
            "tenant_id": str(tenant_id),
            "result_limit": MAX_SIMILAR_CONTROLS_RETURNED,
        },
    ).fetchall()
    return [{"control_id": row[0], "calibrated_distance": row[1]} for row in rows]


def _row_to_record_dict(row) -> dict:
    return {
        "record_id": str(row[0]),
        "title": row[1],
        "body": row[2],
        "distance": row[3],
    }
