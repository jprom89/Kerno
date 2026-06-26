"""Nightly batch: recalculates the retrieval bias vector for every active tenant.

Plain-English summary
---------------------
This job runs once per night (triggered by the platform's cron scheduler) and
does the following for each active customer company:

  1. Fetches all override decisions the company's compliance engineers have made
     since the previous run.
  2. Fetches the company's current calibration vector from the database.
  3. Passes both to ``recalculate_retrieval_bias`` (the pure-math function in
     bias_recalculation_service.py) to get an updated vector.
  4. Writes the updated vector back to the database.
  5. Logs the outcome — success or failure — for every tenant.

One bad tenant does not stop the job. If a company's recalculation fails, the
error is logged and the batch continues to the next tenant. No tenant should be
stuck with a stale calibration because an unrelated company caused an exception.

This file is intentionally thin: it contains only the orchestration logic. The
math lives in ``bias_recalculation_service.py``; the tenant context guard lives
in ``src/services/tenant_context.py``. (KER-114, LEARNING_PIPELINE_SPEC.md §5.1.)

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/services/test_nightly_bias_recalculation.py -v

In production this file is invoked as a cron job. To run it manually:

    python -m src.scheduler.nightly_bias_recalculation
"""

from __future__ import annotations

import logging
import time
import uuid

from src.services.bias_recalculation_service import (
    persist_retrieval_bias,
    recalculate_retrieval_bias,
)
from src.services.tenant_context import resolve_and_set_tenant_context

logger = logging.getLogger(__name__)


def run_nightly_bias_recalculation(db_session_factory, admin_session) -> None:
    """Run the nightly recalculation for every active tenant and log each outcome.

    Iterates all active tenants, recalculates each one's retrieval bias vector,
    and persists the result. Failures per tenant are caught, logged, and skipped
    so the batch continues. Requires a callable ``db_session_factory`` that
    returns a new database connection/transaction, and an ``admin_session`` that
    resolves the platform's service-account tenant for internal queries.
    """
    active_tenants = _fetch_active_tenants(db_session_factory, admin_session)
    logger.info("Nightly bias recalculation starting for %d tenants.", len(active_tenants))
    success_count = 0
    failure_count = 0
    for tenant_id in active_tenants:
        outcome = _recalculate_one_tenant(db_session_factory, tenant_id)
        if outcome["success"]:
            success_count += 1
        else:
            failure_count += 1
    logger.info(
        "Nightly bias recalculation complete. success=%d failure=%d.",
        success_count,
        failure_count,
    )


def _recalculate_one_tenant(db_session_factory, tenant_id: uuid.UUID) -> dict:
    """Recalculate and persist the bias vector for one tenant. Never raises.

    Returns a dict with ``success`` (bool), ``override_count_processed`` (int),
    ``recalculation_duration_ms`` (int), and ``error`` (str or None). Catches
    all exceptions so a single tenant failure cannot halt the batch.
    """
    start_ms = time.monotonic()
    try:
        override_count = _execute_tenant_recalculation(db_session_factory, tenant_id)
        duration_ms = int((time.monotonic() - start_ms) * 1000)
        logger.info(
            "Tenant bias recalculation succeeded.",
            extra={
                "tenant_id": str(tenant_id),
                "override_count_processed": override_count,
                "recalculation_duration_ms": duration_ms,
                "success": True,
            },
        )
        return {
            "success": True,
            "override_count_processed": override_count,
            "recalculation_duration_ms": duration_ms,
            "error": None,
        }
    except Exception as exc:
        duration_ms = int((time.monotonic() - start_ms) * 1000)
        logger.error(
            "Tenant bias recalculation failed.",
            extra={
                "tenant_id": str(tenant_id),
                "recalculation_duration_ms": duration_ms,
                "success": False,
                "error": str(exc),
            },
            exc_info=True,
        )
        return {
            "success": False,
            "override_count_processed": 0,
            "recalculation_duration_ms": duration_ms,
            "error": str(exc),
        }


def _execute_tenant_recalculation(db_session_factory, tenant_id: uuid.UUID) -> int:
    """Open a transaction, recalculate, persist, and return the number of overrides used.

    Separated from ``_recalculate_one_tenant`` to keep both functions under the
    40-line limit (CLAUDE.md §2.5). Raises on any error so the caller's
    except block can handle logging and failure recording.
    """
    with db_session_factory() as conn:
        tenant_session = _TenantSession(tenant_id)
        resolve_and_set_tenant_context(tenant_session, conn)
        overrides = _fetch_overrides_since_last_run(conn, tenant_id)
        current_bias = _fetch_current_bias_vector(conn, tenant_id)
        updated_bias = recalculate_retrieval_bias(current_bias, overrides)
        persist_retrieval_bias(conn, tenant_id, updated_bias, len(overrides))
    return len(overrides)


def _fetch_active_tenants(db_session_factory, admin_session) -> list[uuid.UUID]:
    """Return a list of tenant IDs for all companies with active accounts.

    Uses the admin session to set tenant context for the internal lookup.
    Returns an empty list (never raises) so a failed lookup stops the batch
    gracefully with a log entry rather than an unhandled exception.
    """
    try:
        with db_session_factory() as conn:
            resolve_and_set_tenant_context(admin_session, conn)
            rows = conn.execute(
                "SELECT tenant_id FROM tenants WHERE is_active = true"
            ).fetchall()
            return [uuid.UUID(str(row[0])) for row in rows]
    except Exception as exc:
        logger.error("Failed to fetch active tenants: %s", exc, exc_info=True)
        return []


def _fetch_overrides_since_last_run(conn, tenant_id: uuid.UUID) -> list[dict]:
    """Return overrides written since the last bias recalculation for this tenant.

    Queries overrides that were created after the tenant's most recent
    ``last_recalculated_at`` timestamp. Returns an empty list if there are none,
    in which case ``recalculate_retrieval_bias`` will leave the current bias
    vector unchanged.
    """
    rows = conn.execute(
        """
        SELECT o.reviewer_confidence_weight,
               e_target.embedding  AS target_control_vector,
               e_source.embedding  AS source_recommendation_vector
        FROM overrides o
        JOIN tenant_embeddings e_target ON e_target.control_id = o.corrected_control_id
        JOIN tenant_embeddings e_source ON e_source.control_id = o.original_control_id
        LEFT JOIN retrieval_bias rb ON rb.tenant_id = o.tenant_id
        WHERE o.tenant_id = :tenant_id
          AND o.created_at > COALESCE(rb.last_recalculated_at, '1970-01-01'::timestamptz)
        """,
        {"tenant_id": str(tenant_id)},
    ).fetchall()
    return [
        {
            "reviewer_confidence_weight": row[0],
            "target_control_vector": row[1],
            "source_recommendation_vector": row[2],
        }
        for row in rows
    ]


def _fetch_current_bias_vector(conn, tenant_id: uuid.UUID) -> list[float]:
    """Return the tenant's current retrieval bias vector, or an all-zero vector.

    A brand-new tenant that has never had a bias vector calculated gets a zero
    vector as the starting point. The zero vector means "no learned preference
    yet" and is a safe neutral starting position. The dimension count is read
    from the database row when available; the first-time zero vector's dimension
    is inferred from the first override's embedding length at recalculation time.
    """
    row = conn.execute(
        "SELECT bias_vector FROM retrieval_bias WHERE tenant_id = :tenant_id",
        {"tenant_id": str(tenant_id)},
    ).fetchone()
    if row is None:
        return []
    return list(row[0])


class _TenantSession:
    """Minimal session adapter that provides a fixed tenant id to the context resolver.

    Used internally by the batch so each per-tenant transaction can call
    ``resolve_and_set_tenant_context`` without needing a real HTTP session object.
    """

    def __init__(self, tenant_id: uuid.UUID) -> None:
        """Store the tenant id this session will report."""
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> uuid.UUID:
        """Return the fixed tenant id for this batch session."""
        return self._tenant_id
