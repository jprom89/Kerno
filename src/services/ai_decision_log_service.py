"""AI-decision log service — writes, queries, and prunes retained AI mapping decisions.

Plain-English summary
---------------------
Three things happen to an AI decision record over its life, and all three live
here (KER-203):

  1. ``emit_decision_log`` — called by the mapping engine in the SAME
     transaction as the recommendation write, so a recommendation can never
     exist without its retained decision record (and vice versa).
  2. ``query_decision_logs`` — the tenant-scoped read behind
     GET /api/v1/ai-decisions, with optional filters on control, date, and
     confidence.
  3. ``prune_old_logs`` — deletes a tenant's rows older than the retention
     window (AI_DECISION_LOG_RETENTION_DAYS = 180 days), run nightly by
     src/scheduler/prune_ai_decision_log.py.

Tenant isolation applies exactly as everywhere else (CLAUDE.md §3): every
function sets the tenant context before touching the table, and the table is
FORCE row-level secured (migration 020), so even a bug in this file could not
read another tenant's decisions. GDPR alignment: this service stores only the
input snapshot's SHA-256 hash — hashing happens in the mapping engine before
the value reaches this file.

The ``conn`` parameter throughout must be a raw database connection supporting
``conn.execute(sql, params_dict)`` — not a SQLAlchemy Session — matching the
contract used across the service layer.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/services/test_ai_decision_log.py -v

Live-database proof (emit inside map_control, query, prune window):

    pytest tests/integration/test_ker203_ai_decision_log.py -m integration -v
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

from config.constants import AI_DECISION_LOG_RETENTION_DAYS
from src.db.rls import set_tenant_context

_INSERT_DECISION_LOG = """
INSERT INTO ai_decision_log (
    correlation_id, tenant_id, control_id, evidence_ids, input_snapshot_hash,
    output_status, confidence_score, rationale_extract, model_version
) VALUES (
    :correlation_id, :tenant_id, :control_id, :evidence_ids, :input_snapshot_hash,
    :output_status, :confidence_score, :rationale_extract, :model_version
)
"""

_SELECT_DECISION_LOGS = """
SELECT correlation_id, control_id, evidence_ids, input_snapshot_hash,
       output_status, confidence_score, rationale_extract, model_version,
       created_at
FROM ai_decision_log
WHERE tenant_id = :tenant_id
"""

_ORDER_NEWEST_FIRST = " ORDER BY created_at DESC"

# RETURNING lets the caller count deletions through the codebase's cursor
# wrappers, which expose fetchall() but not rowcount.
_DELETE_EXPIRED_LOGS = """
DELETE FROM ai_decision_log
WHERE tenant_id = :tenant_id
  AND created_at < :retention_cutoff
RETURNING correlation_id
"""


@dataclasses.dataclass(frozen=True)
class DecisionLogEntry:
    """One retained AI decision as returned by query_decision_logs."""

    correlation_id: str
    control_id: str
    evidence_ids: list[str]
    input_snapshot_hash: str
    output_status: str
    confidence_score: float
    rationale_extract: str
    model_version: str
    created_at: datetime


def emit_decision_log(
    conn,
    tenant_id,
    *,
    control_id: str,
    evidence_ids: list[str],
    input_snapshot_hash: str,
    output_status: str,
    confidence_score: float,
    rationale_extract: str,
    model_version: str,
) -> str:
    """Write one AI decision record and return its correlation_id.

    Runs on the caller's open connection and transaction — the mapping engine
    calls this right after the recommendation INSERT so both rows commit or
    roll back together (KER-203 AC-2). Sets tenant context first and raises
    ``TenantContextMissingError`` if tenant_id is missing or invalid. The
    correlation_id is generated here so the caller can reference the record
    without a RETURNING round-trip.
    """
    set_tenant_context(conn, tenant_id)
    correlation_id = str(uuid.uuid4())
    conn.execute(
        _INSERT_DECISION_LOG,
        {
            "correlation_id": correlation_id,
            "tenant_id": str(tenant_id),
            "control_id": control_id,
            "evidence_ids": evidence_ids,
            "input_snapshot_hash": input_snapshot_hash,
            "output_status": output_status,
            "confidence_score": confidence_score,
            "rationale_extract": rationale_extract,
            "model_version": model_version,
        },
    )
    return correlation_id


def query_decision_logs(
    conn,
    tenant_id,
    control_id: str | None = None,
    after: datetime | None = None,
    confidence_gte: float | None = None,
) -> list[DecisionLogEntry]:
    """Return the tenant's retained AI decisions, newest first, with optional filters.

    Filters compose with AND: ``control_id`` restricts to one control ref,
    ``after`` keeps decisions created at or after that moment, and
    ``confidence_gte`` keeps decisions at or above that confidence. Sets tenant
    context first; the tenant_id must come from the authenticated session,
    never from request input. Raises ``TenantContextMissingError`` on a
    missing or invalid tenant.
    """
    set_tenant_context(conn, tenant_id)
    sql, params = _build_query_filters(tenant_id, control_id, after, confidence_gte)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_entry(row) for row in rows]


def prune_old_logs(conn, tenant_id) -> int:
    """Delete the tenant's decision records older than the retention window; return the count.

    The window is AI_DECISION_LOG_RETENTION_DAYS (config/constants.py). Rows
    inside the window are untouched — the EU AI Act Article 19 duty is to
    RETAIN at least the window, so pruning must never reach into it. Sets
    tenant context first; raises ``TenantContextMissingError`` on a missing or
    invalid tenant. Runs on the caller's transaction.
    """
    set_tenant_context(conn, tenant_id)
    retention_cutoff = datetime.now(timezone.utc) - timedelta(
        days=AI_DECISION_LOG_RETENTION_DAYS
    )
    deleted_rows = conn.execute(
        _DELETE_EXPIRED_LOGS,
        {"tenant_id": str(tenant_id), "retention_cutoff": retention_cutoff},
    ).fetchall()
    return len(deleted_rows)


def _build_query_filters(
    tenant_id,
    control_id: str | None,
    after: datetime | None,
    confidence_gte: float | None,
) -> tuple[str, dict]:
    """Compose the SELECT with only the filters the caller supplied.

    Every filter is a bound parameter — values are never interpolated into the
    SQL string. Returns the finished SQL and its params dict.
    """
    sql = _SELECT_DECISION_LOGS
    params: dict = {"tenant_id": str(tenant_id)}
    if control_id is not None:
        sql += " AND control_id = :control_id"
        params["control_id"] = control_id
    if after is not None:
        sql += " AND created_at >= :after"
        params["after"] = after
    if confidence_gte is not None:
        sql += " AND confidence_score >= :confidence_gte"
        params["confidence_gte"] = confidence_gte
    return sql + _ORDER_NEWEST_FIRST, params


def _row_to_entry(row) -> DecisionLogEntry:
    """Convert one database row into a DecisionLogEntry."""
    return DecisionLogEntry(
        correlation_id=str(row[0]),
        control_id=row[1],
        evidence_ids=list(row[2]),
        input_snapshot_hash=row[3],
        output_status=row[4],
        confidence_score=float(row[5]),
        rationale_extract=row[6],
        model_version=row[7],
        created_at=row[8],
    )
