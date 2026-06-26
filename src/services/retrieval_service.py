"""Retrieval service — finds the most relevant compliance controls for a query.

What this file does
-------------------
Runs the similarity search that maps a piece of evidence to the compliance
controls most likely to cover it. When a tenant has a calibrated bias vector
(produced nightly by ``bias_recalculation_service.py``), that vector is injected
into the scoring at query time so controls the company historically prefers appear
higher. When no bias row exists yet (new tenant, or first day before any nightly
run), the service falls back silently to standard cosine similarity.

Why this file exists
--------------------
Document 9 closes the learning-pipeline feedback loop identified in the Document 8
review. The previous implementation computed cosine distance between the bias
vector and the query vector — a value with no bearing on per-embedding ranking.
This rewrite uses pgvector's inner-product operator (``<#>``) to compute the dot
product between the bias vector and each stored embedding, which is what the spec
formula requires.

Biased scoring formula (PROMPT_doc9_retrieval_scoring.md §4.1):

    adjusted_score = cosine_similarity(query, embedding)
                   + BIAS_INJECTION_COEFFICIENT × dot_product(bias_vector, embedding)

For ascending-distance ranking (lower calibrated_distance = better rank):

    calibrated_distance = (embedding <=> query)
                        + BIAS_INJECTION_COEFFICIENT × (bias_vector <#> embedding)

The ``<#>`` operator returns the *negative* inner product, so a high positive
dot product makes the contribution negative, which lowers calibrated_distance
and raises the embedding's rank. This is consistent with the spec note that
"the bias term is subtracted (not added) consistently with the ranking direction."

Bias vector storage (§3.1 normalised approach):
    The bias vector is stored once per tenant in the ``retrieval_bias`` table.
    It is fetched at query time and passed as a bound SQL parameter.
    It is NOT written into ``tenant_embeddings`` rows.

Connection contract
-------------------
``conn`` must be a raw database connection supporting ``conn.execute(sql, params)``
with ``:name``-style parameters. It must not be a SQLAlchemy Session. The
conftest ``_DbConnection`` wrapper converts ``:name`` to ``%(name)s`` and
automatically adds ``::vector`` casts for ``list[float]`` parameters.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/services/test_retrieval_service.py -v

The test suite has 10 cases covering the biased path, unbiased fallback,
tenant isolation guards, limit and coefficient values, table-name correctness,
and the raw-connection contract.
"""

from __future__ import annotations

import logging
import uuid

from config.constants import BIAS_INJECTION_COEFFICIENT, MAX_SIMILAR_CONTROLS_RETURNED
from src.exceptions import TenantContextMissingError
from src.services.tenant_context import resolve_and_set_tenant_context

logger = logging.getLogger(__name__)

# Re-exported: callers that need to catch TenantContextMissingError can import
# it from this module rather than reaching into src.exceptions directly,
# consistent with the pattern established in src.services.tenant_context.
__all__ = ["get_similar_controls", "TenantContextMissingError"]

# ---------------------------------------------------------------------------
# Module-level SQL constants.
#
# Named placeholders (:name) are used throughout; the connection layer converts
# them to %(name)s and adds ::vector casts for list[float] parameters.
#
# The <#> operator in the biased query computes the negative inner product of
# two vectors. Because it returns -dot_product, the formula
#   cosine_distance + BIAS * (bias <#> embedding)
# is equivalent to
#   cosine_distance - BIAS * dot_product(bias, embedding)
# which lowers calibrated_distance (and raises rank) for embeddings that are
# well-aligned with the tenant's bias direction. (§4.1, §3.1)
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


def get_similar_controls(session, conn, query_vector: list[float]) -> list[dict]:
    """Return the top controls most similar to the query, applying tenant calibration.

    Resolves the tenant from the authenticated session, activates the RLS tenant
    context on ``conn``, then runs either the biased or unbiased similarity query
    depending on whether a bias vector row exists for this tenant. Returns at most
    ``MAX_SIMILAR_CONTROLS_RETURNED`` controls, each as a dict with ``control_id``
    and ``calibrated_distance``. Falls back to unbiased search if the tenant has
    no bias row yet (e.g., first day before any nightly recalculation run) or if
    the stored bias vector is empty — no error is raised in either case.

    Raises ``TenantContextMissingError`` (from ``src.exceptions``) if the session
    cannot supply a valid UUIDv4 tenant. No SQL is issued in that case.
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


def _fetch_tenant_bias_vector(conn, tenant_id: uuid.UUID) -> list[float] | None:
    """Return the tenant's retrieval bias vector, or None if it does not exist.

    Returns ``None`` (not an empty list) when no row is present, so the caller
    can distinguish "not yet calibrated" from a genuine zero vector. The caller
    must also treat an empty list as uncalibrated (§4.2).
    """
    row = conn.execute(
        "SELECT bias_vector FROM retrieval_bias WHERE tenant_id = :tenant_id",
        {"tenant_id": str(tenant_id)},
    ).fetchone()
    if row is None:
        return None
    return list(row[0])


def _run_biased_query(
    conn,
    tenant_id: uuid.UUID,
    query_vector: list[float],
    bias_vector: list[float],
) -> list[dict]:
    """Execute the calibrated similarity query with the tenant's bias vector.

    Passes ``bias_vector`` as a bound SQL parameter (not a column reference).
    The ``<#>`` operator in the SQL computes the negative inner product of the
    bias vector and each stored embedding; combined with ``BIAS_INJECTION_COEFFICIENT``,
    this nudges results toward controls the tenant historically prefers.
    (PROMPT_doc9_retrieval_scoring.md §4.1, §3.1.)
    """
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
    """Execute standard cosine-distance search for tenants without a bias vector.

    Returns results in the same dict shape as ``_run_biased_query`` so callers
    need not distinguish which path was taken.
    """
    rows = conn.execute(
        _UNBIASED_SIMILARITY_QUERY,
        {
            "query_vector": query_vector,
            "tenant_id": str(tenant_id),
            "result_limit": MAX_SIMILAR_CONTROLS_RETURNED,
        },
    ).fetchall()
    return [{"control_id": row[0], "calibrated_distance": row[1]} for row in rows]
