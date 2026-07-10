"""Nightly batch: recalculates the retrieval bias vector for every active tenant.

Plain-English summary
---------------------
This job runs once per night and does the following for each active customer
company:

  1. Fetches all override decisions the company's compliance engineers have made
     since the previous run.
  2. Fetches the company's current calibration vector from the database.
  3. Passes both to ``recalculate_retrieval_bias`` (the pure-math function in
     bias_recalculation_service.py) to get an updated vector.
  4. Writes the updated vector back to the database and appends a KER-107 audit
     ledger entry recording the recalculation, in the same transaction.
  5. Logs the outcome — success or failure — for every tenant.

One bad tenant does not stop the job. If a company's recalculation fails, the
error is logged and the batch continues to the next tenant. No tenant should be
stuck with a stale calibration because an unrelated company caused an exception.

A tenant with no new overrides since its last run is skipped without writing
anything: there is nothing to recalculate, so no bias row is touched and no
ledger entry is appended.

This file is intentionally thin: it contains only the orchestration logic. The
math lives in ``bias_recalculation_service.py``; the tenant context guard lives
in ``src/services/tenant_context.py``. (KER-201, LEARNING_PIPELINE_SPEC.md §5.1.)

Scheduling (KER-201)
--------------------
The nightly trigger is a cron entrypoint, not an in-process scheduler — no new
dependency, and the platform's scheduler owns retries and alerting:

    python -m src.scheduler.nightly_bias_recalculation

Run it nightly via cron (Linux) or Task Scheduler (Windows dev). A single
tenant can also be recalculated on demand through
POST /api/v1/scheduler/run-recalculation (src/api/routers/scheduler.py), which
calls ``run_tenant_recalculation`` for the authenticated tenant.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/scheduler/test_nightly_recalculation.py -v

End-to-end proof against a live database (seeds overrides, runs the batch,
asserts the bias vector moved and the ranking changed):

    pytest tests/integration/test_ker201_bias_recalculation.py -m integration -v
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import time
import uuid

import psycopg2

from config.constants import MILLISECONDS_PER_SECOND

# _ExecutableConn is imported rather than reimplemented so the cron entrypoint
# converts :name parameters (and vector values) exactly the way every other
# caller does — dependencies.py is the single home for that adaptation logic.
from src.api.dependencies import _ExecutableConn
from src.services.audit_log import append_audit_entry
from src.services.bias_recalculation_service import (
    coerce_vector,
    persist_retrieval_bias,
    recalculate_retrieval_bias,
)
from src.services.tenant_context import resolve_and_set_tenant_context

logger = logging.getLogger(__name__)

# Identity the batch presents to the §3 tenant-context guard for its one
# internal query — listing the active tenants. It is a fixed, valid-v4 UUID
# that deliberately matches no real tenant: the tenants table is readable by
# the batch's database role regardless of context (RLS is not forced on it,
# for the same bootstrap reason as login — see migration 018), and if that
# ever changed the guard would return zero tenants rather than leak any.
PLATFORM_SCHEDULER_TENANT_ID = uuid.UUID("00000000-0000-4000-8000-000000000000")

# Status values reported by a per-tenant run (mirrored in the API response
# schema src/api/schemas/scheduler.py).
STATUS_RECALCULATED = "recalculated"
STATUS_NO_NEW_OVERRIDES = "no_new_overrides"


@dataclasses.dataclass(frozen=True)
class RecalculationRunResult:
    """Outcome of one real per-tenant recalculation (KER-201).

    ``status`` is STATUS_RECALCULATED when the bias vector was updated and the
    ledger entry written, or STATUS_NO_NEW_OVERRIDES when the tenant had nothing
    new to process and no write occurred. ``dimensions`` is the length of the
    tenant's bias vector (0 when the tenant has never been calibrated).
    """

    tenant_id: str
    override_count: int
    dimensions: int
    duration_ms: int
    status: str


def run_tenant_recalculation(conn, session) -> RecalculationRunResult:
    """Recalculate the authenticated tenant's bias vector now (manual trigger path).

    Resolves the tenant from the authenticated session, runs the real
    recalculation on the caller's open connection, and returns what happened.
    The bias upsert and its KER-107 ledger entry share the caller's transaction,
    so they commit or roll back together. Raises TenantContextMissingError on an
    invalid session. (KER-201 — replaces the KER-114 stub.)
    """
    start_ms = time.monotonic()
    tenant_id = resolve_and_set_tenant_context(session, conn)
    logger.info("NIGHTLY_RECALCULATION started tenant=%s", tenant_id)
    override_count, dimensions, status = _recalculate_tenant_bias(conn, tenant_id)
    duration_ms = int((time.monotonic() - start_ms) * MILLISECONDS_PER_SECOND)
    logger.info(
        "NIGHTLY_RECALCULATION completed tenant=%s override_count=%d "
        "dimensions=%d duration_ms=%d status=%s",
        tenant_id, override_count, dimensions, duration_ms, status,
    )
    return RecalculationRunResult(
        tenant_id=str(tenant_id),
        override_count=override_count,
        dimensions=dimensions,
        duration_ms=duration_ms,
        status=status,
    )


def _recalculate_tenant_bias(conn, tenant_id: uuid.UUID) -> tuple[int, int, str]:
    """Fetch, recalculate, persist, and audit one tenant's bias vector.

    The shared core of both trigger paths (manual API call and nightly batch).
    Requires the tenant context to be already set on ``conn``. When the tenant
    has no new overrides since its last run, nothing is written — the bias row
    and the ledger are left untouched. Returns
    ``(override_count, dimensions, status)``.
    """
    overrides = _fetch_overrides_since_last_run(conn, tenant_id)
    current_bias = _fetch_current_bias_vector(conn, tenant_id)
    if not overrides:
        return 0, len(current_bias), STATUS_NO_NEW_OVERRIDES
    updated_bias = recalculate_retrieval_bias(current_bias, overrides)
    recalculated_at = persist_retrieval_bias(conn, tenant_id, updated_bias, len(overrides))
    _record_recalculation_audit_entry(
        conn, tenant_id, len(overrides), len(updated_bias), recalculated_at
    )
    return len(overrides), len(updated_bias), STATUS_RECALCULATED


def _record_recalculation_audit_entry(
    conn,
    tenant_id: uuid.UUID,
    override_count: int,
    dimensions: int,
    recalculated_at,
) -> None:
    """Append the KER-107 ledger entry for one real recalculation (KER-201 AC-4).

    Runs on the same connection and transaction as the bias upsert, so the new
    vector and its ledger entry commit or roll back together. actor_id None
    marks the event as system-generated; ``updated_at`` in after_state is the
    exact timestamp written to the bias row's last_recalculated_at column.
    """
    append_audit_entry(
        conn,
        tenant_id,
        actor_id=None,
        actor_role="system",
        action_type="bias_recalculated",
        object_type="bias_vector",
        object_id=str(tenant_id),
        control_id=None,
        after_state={
            "override_count": override_count,
            "dimensions": dimensions,
            "updated_at": recalculated_at.isoformat(),
        },
    )


def run_nightly_bias_recalculation(db_session_factory, admin_session) -> dict:
    """Run the nightly recalculation for every active tenant and log each outcome.

    Iterates all active tenants, recalculates each one's retrieval bias vector,
    and persists the result. Failures per tenant are caught, logged, and skipped
    so the batch continues. Requires a callable ``db_session_factory`` that
    returns a context manager yielding a database connection, and an
    ``admin_session`` resolving the platform scheduler identity (see
    PLATFORM_SCHEDULER_TENANT_ID) for the internal tenant-list query. Returns
    ``{"success_count": int, "failure_count": int}``.
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
    return {"success_count": success_count, "failure_count": failure_count}


def _recalculate_one_tenant(db_session_factory, tenant_id: uuid.UUID) -> dict:
    """Recalculate and persist the bias vector for one tenant. Never raises.

    Returns a dict with ``success`` (bool), ``override_count_processed`` (int),
    ``recalculation_duration_ms`` (int), and ``error`` (str or None). Catches
    all exceptions so a single tenant failure cannot halt the batch.
    """
    start_ms = time.monotonic()
    try:
        override_count = _execute_tenant_recalculation(db_session_factory, tenant_id)
        duration_ms = int((time.monotonic() - start_ms) * MILLISECONDS_PER_SECOND)
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
        duration_ms = int((time.monotonic() - start_ms) * MILLISECONDS_PER_SECOND)
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
    """Open a transaction, run the shared recalculation core, and return the override count.

    Separated from ``_recalculate_one_tenant`` to keep both functions under the
    40-line limit (CLAUDE.md §2.5). Raises on any error so the caller's
    except block can handle logging and failure recording.
    """
    with db_session_factory() as conn:
        tenant_session = _TenantSession(tenant_id)
        resolve_and_set_tenant_context(tenant_session, conn)
        override_count, _, _ = _recalculate_tenant_bias(conn, tenant_id)
    return override_count


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
    ``last_recalculated_at`` timestamp. Embedding columns arrive from the
    database in pgvector's text form and are coerced to float lists here, so
    the math layer only ever sees numbers. Returns an empty list if there are
    no new overrides.
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
            "reviewer_confidence_weight": float(row[0]),
            "target_control_vector": coerce_vector(row[1]),
            "source_recommendation_vector": coerce_vector(row[2]),
        }
        for row in rows
    ]


def _fetch_current_bias_vector(conn, tenant_id: uuid.UUID) -> list[float]:
    """Return the tenant's current retrieval bias vector, or an empty list.

    An empty list means "no learned preference yet" — either no bias row exists
    or the tenant has never been calibrated. ``recalculate_retrieval_bias``
    treats that as the signal to seed a zero vector sized from the first
    override's embedding. The stored pgvector value is coerced from its text
    form to a float list before it reaches the math layer.
    """
    row = conn.execute(
        "SELECT bias_vector FROM retrieval_bias WHERE tenant_id = :tenant_id",
        {"tenant_id": str(tenant_id)},
    ).fetchone()
    if row is None:
        return []
    return coerce_vector(row[0])


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


@contextlib.contextmanager
def _cron_transaction():
    """Open one database transaction for the cron batch; commit on success, roll back on error.

    Connects with DATABASE_URL and wraps the raw psycopg2 connection in
    ``_ExecutableConn`` so service-layer ``:name`` parameters (and vector
    values) are adapted exactly as they are inside the API. Each call opens a
    fresh connection because the batch isolates tenants per transaction.
    """
    raw_conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        yield _ExecutableConn(raw_conn)
        raw_conn.commit()
    except Exception:
        raw_conn.rollback()
        raise
    finally:
        raw_conn.close()


def main() -> None:
    """Cron entrypoint: recalculate every active tenant's bias vector (KER-201).

    Invoke nightly from the platform scheduler:

        python -m src.scheduler.nightly_bias_recalculation

    Loads .env when python-dotenv is available (matching the app factory), then
    runs the full batch. Per-tenant failures are logged and skipped inside the
    batch; the process exits normally so the scheduler treats partial failure
    as a logged condition, not a crash loop.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO)
    admin_session = _TenantSession(PLATFORM_SCHEDULER_TENANT_ID)
    summary = run_nightly_bias_recalculation(_cron_transaction, admin_session)
    logger.info(
        "Nightly bias recalculation entrypoint finished. success=%d failure=%d.",
        summary["success_count"],
        summary["failure_count"],
    )


if __name__ == "__main__":
    main()
