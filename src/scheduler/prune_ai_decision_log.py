"""Nightly prune: deletes AI-decision log rows past the retention window, per tenant.

Plain-English summary
---------------------
The AI-decision log (KER-203) must RETAIN every record for at least
AI_DECISION_LOG_RETENTION_DAYS (180 days) — and should not keep them forever.
This job walks every active tenant and deletes only that tenant's rows older
than the window. Rows inside the window are never touched: the EU AI Act
Article 19 duty is a retention floor, and the prune respects it by
construction (the cutoff comparison lives in
ai_decision_log_service.prune_old_logs, next to the constant).

One bad tenant does not stop the job — failures are logged and the batch
continues, exactly like the KER-201 nightly bias recalculation this file is
modelled on. Each tenant's prune runs in its own transaction under that
tenant's context (the table is FORCE row-level secured).

Scheduling (KER-203 AC-4)
-------------------------
Cron entrypoint, same mechanism as the KER-201 scheduler (CLAUDE.md §12
KER-201 decision 1 — no new dependency, platform scheduler owns retries):

    python -m src.scheduler.prune_ai_decision_log

Run it nightly via cron (Linux) or Task Scheduler (Windows dev), alongside
the bias recalculation job.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/services/test_ai_decision_log.py -v

Live-database proof (rows outside the window deleted, inside retained):

    pytest tests/integration/test_ker203_ai_decision_log.py -m integration -v
"""

from __future__ import annotations

import logging
import time
import uuid

# The connection factory, active-tenant lookup, session adapter, and platform
# scheduler identity are imported from the KER-201 scheduler rather than
# reimplemented — one home for the batch plumbing, so the two nightly jobs can
# never drift apart on connection handling or tenant discovery.
from src.scheduler.nightly_bias_recalculation import (
    PLATFORM_SCHEDULER_TENANT_ID,
    _cron_transaction,
    _fetch_active_tenants,
    _TenantSession,
)
from src.services.ai_decision_log_service import prune_old_logs
from src.services.tenant_context import resolve_and_set_tenant_context

logger = logging.getLogger(__name__)


def run_nightly_decision_log_prune(db_session_factory, admin_session) -> dict:
    """Prune every active tenant's expired AI-decision rows; log each outcome.

    Iterates all active tenants and deletes each one's rows older than the
    retention window. Per-tenant failures are caught, logged, and skipped so
    the batch continues. Takes the same factory/session contract as the
    KER-201 batch. Returns ``{"success_count": int, "failure_count": int,
    "deleted_count": int}``.
    """
    active_tenants = _fetch_active_tenants(db_session_factory, admin_session)
    logger.info("AI-decision log prune starting for %d tenants.", len(active_tenants))
    success_count = 0
    failure_count = 0
    deleted_count = 0
    for tenant_id in active_tenants:
        outcome = _prune_one_tenant(db_session_factory, tenant_id)
        if outcome["success"]:
            success_count += 1
            deleted_count += outcome["deleted_count"]
        else:
            failure_count += 1
    logger.info(
        "AI-decision log prune complete. success=%d failure=%d deleted=%d.",
        success_count, failure_count, deleted_count,
    )
    return {
        "success_count": success_count,
        "failure_count": failure_count,
        "deleted_count": deleted_count,
    }


def _prune_one_tenant(db_session_factory, tenant_id: uuid.UUID) -> dict:
    """Prune one tenant's expired rows in its own transaction. Never raises.

    Returns a dict with ``success`` (bool), ``deleted_count`` (int), and
    ``error`` (str or None). Catches all exceptions so a single tenant
    failure cannot halt the batch.
    """
    start_ms = time.monotonic()
    try:
        with db_session_factory() as conn:
            resolve_and_set_tenant_context(_TenantSession(tenant_id), conn)
            deleted_count = prune_old_logs(conn, tenant_id)
        duration_ms = int((time.monotonic() - start_ms) * 1000)
        logger.info(
            "Tenant AI-decision log prune succeeded.",
            extra={
                "tenant_id": str(tenant_id),
                "deleted_count": deleted_count,
                "prune_duration_ms": duration_ms,
                "success": True,
            },
        )
        return {"success": True, "deleted_count": deleted_count, "error": None}
    except Exception as exc:
        duration_ms = int((time.monotonic() - start_ms) * 1000)
        logger.error(
            "Tenant AI-decision log prune failed.",
            extra={
                "tenant_id": str(tenant_id),
                "prune_duration_ms": duration_ms,
                "success": False,
                "error": str(exc),
            },
            exc_info=True,
        )
        return {"success": False, "deleted_count": 0, "error": str(exc)}


def main() -> None:
    """Cron entrypoint: prune every active tenant's expired AI-decision rows.

    Invoke nightly from the platform scheduler:

        python -m src.scheduler.prune_ai_decision_log

    Loads .env when python-dotenv is available (matching the app factory and
    the KER-201 entrypoint), then runs the batch. Per-tenant failures are
    logged and skipped inside the batch; the process exits normally so the
    scheduler treats partial failure as a logged condition, not a crash loop.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO)
    admin_session = _TenantSession(PLATFORM_SCHEDULER_TENANT_ID)
    summary = run_nightly_decision_log_prune(_cron_transaction, admin_session)
    logger.info(
        "AI-decision log prune entrypoint finished. success=%d failure=%d deleted=%d.",
        summary["success_count"], summary["failure_count"], summary["deleted_count"],
    )


if __name__ == "__main__":
    main()
