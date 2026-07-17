"""Control-coverage read service (KER-109) — resolves each control's system-of-record status.

Resolution rule: a human override (KER-106) always wins over the AI recommendation —
'approve' confirms the AI's status, 'edit'/'reject' invalidate it (conservatively resolving
to gap until coverage is re-established); with no override the AI recommendation status is
the working status, and a control with neither resolves to gap so coverage is never
over-claimed. Run tests with: pytest tests/unit/services/test_coverage_service.py -v
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError  # noqa: F401  re-exported
from src.models.recommendation import STATUS_GAP, STATUS_MET, STATUS_PARTIAL

# Where a control's resolved status came from — shown to auditors so a
# human-confirmed figure is distinguishable from a machine-only one.
SOURCE_OVERRIDE: str = "override"
SOURCE_RECOMMENDATION: str = "recommendation"
SOURCE_NONE: str = "none"

# One row per active control with its latest non-superseded recommendation and
# latest human override (LATERAL keeps "latest per control" in one pass).
# recommendations.control_id and overrides.original_control_id are TEXT, so the
# catalogue UUID is cast for the comparison. compliance_controls is global
# platform data (no tenant column); the tenant-owned joins filter explicitly on
# tenant_id as defence in depth on top of RLS.
_COVERAGE_QUERY = """
SELECT
    cc.control_id,
    cc.control_ref,
    cc.title,
    cc.category,
    cc.framework,
    rec.status,
    rec.confidence_level,
    rec.confidence_score,
    ov.action_type,
    COALESCE(ev.evidence_count, 0)
FROM compliance_controls cc
LEFT JOIN LATERAL (
    SELECT r.status, r.confidence_level, r.confidence_score
    FROM recommendations r
    WHERE r.tenant_id = :tenant_id
      AND r.control_id = cc.control_id::text
      AND r.is_superseded = FALSE
    ORDER BY r.generated_at DESC
    LIMIT 1
) rec ON TRUE
LEFT JOIN LATERAL (
    SELECT o.action_type
    FROM overrides o
    WHERE o.tenant_id = :tenant_id
      AND o.original_control_id = cc.control_id::text
    ORDER BY o.created_at DESC
    LIMIT 1
) ov ON TRUE
LEFT JOIN LATERAL (
    SELECT count(*) AS evidence_count
    FROM control_evidence_links cel
    WHERE cel.control_id = cc.control_id
      AND cel.removed_at IS NULL
) ev ON TRUE
WHERE cc.is_active = TRUE
"""

_CATEGORY_FILTER = " AND cc.category = :category"
_COVERAGE_ORDER = " ORDER BY cc.category, cc.control_ref"


@dataclasses.dataclass(frozen=True)
class CoverageControl:
    """One control with its resolved system-of-record status for the dashboard."""

    control_id: str
    control_ref: str
    title: str
    category: str
    framework: str
    status: str
    status_source: str
    human_confirmed: bool
    confidence_level: str | None
    confidence_score: float | None
    evidence_count: int


@dataclasses.dataclass(frozen=True)
class CategoryCoverage:
    """Status counts for one control category."""

    category: str
    met: int
    partial: int
    gap: int
    total: int


@dataclasses.dataclass(frozen=True)
class CoverageSummary:
    """Tenant-wide coverage totals plus the per-category breakdown.

    last_recalculated_at is when the tenant's retrieval bias vector was last
    recalculated (KER-302 AC-3), or None for a never-calibrated tenant. It
    defaults to None so pure aggregation callers (summarise_coverage, the
    Trust Center's NIS2 filter) are unaffected.
    """

    total_controls: int
    met: int
    partial: int
    gap: int
    categories: list[CategoryCoverage]
    last_recalculated_at: datetime | None = None


def resolve_system_of_record_status(
    override_action: str | None,
    recommendation_status: str | None,
) -> tuple[str, str, bool]:
    """Return (status, source, human_confirmed) for one control.

    The KER-106 human decision is the system of record: 'approve' confirms the
    AI's status, while 'edit' and 'reject' invalidate the AI's asserted coverage
    for this control — both resolve to gap until a new recommendation is
    confirmed, because a compliance dashboard must never over-claim. Without an
    override the AI recommendation is the (unconfirmed) working status; with
    neither, the control has nothing demonstrating coverage and is a gap.
    """
    if override_action == "approve":
        return (recommendation_status or STATUS_GAP), SOURCE_OVERRIDE, True
    if override_action in ("edit", "reject"):
        return STATUS_GAP, SOURCE_OVERRIDE, True
    if recommendation_status is not None:
        return recommendation_status, SOURCE_RECOMMENDATION, False
    return STATUS_GAP, SOURCE_NONE, False


def get_coverage_controls(conn, tenant_id, category: str | None = None) -> list[CoverageControl]:
    """Return every active control with its resolved status, optionally filtered by category.

    Sets tenant context before querying. Ordered by category then control_ref.
    Raises TenantContextMissingError if tenant_id is missing or invalid.
    """
    set_tenant_context(conn, tenant_id)
    sql = _COVERAGE_QUERY
    params: dict = {"tenant_id": str(tenant_id)}
    if category is not None:
        sql += _CATEGORY_FILTER
        params["category"] = category
    rows = conn.execute(sql + _COVERAGE_ORDER, params).fetchall()
    return [_row_to_coverage_control(row) for row in rows]


def get_coverage_summary(conn, tenant_id) -> CoverageSummary:
    """Return tenant-wide status counts, per-category breakdown, and calibration age.

    Derived from the same rows as get_coverage_controls so the summary always
    reconciles exactly with the drill-down figures. Also carries when the
    tenant's bias vector was last recalculated (KER-302 AC-3).
    """
    controls = get_coverage_controls(conn, tenant_id)
    summary = summarise_coverage(controls)
    return dataclasses.replace(
        summary, last_recalculated_at=_fetch_last_recalculated_at(conn, tenant_id)
    )


def _fetch_last_recalculated_at(conn, tenant_id) -> datetime | None:
    """Return when this tenant's bias vector was last recalculated, or None.

    Verified source (KER-302 AC-3): retrieval_bias.last_recalculated_at is the
    only recalculation-completion timestamp in the schema — it is written by
    persist_retrieval_bias (KER-201) on every real recalculation, for both the
    manual POST /api/v1/scheduler/run-recalculation path and the nightly cron.
    None means the tenant has never been calibrated (no retrieval_bias row).
    Runs under the tenant context set by get_coverage_controls.
    """
    row = conn.execute(
        "SELECT last_recalculated_at FROM retrieval_bias WHERE tenant_id = :tenant_id",
        {"tenant_id": str(tenant_id)},
    ).fetchone()
    return row[0] if row is not None else None


def summarise_coverage(controls: list[CoverageControl]) -> CoverageSummary:
    """Aggregate resolved controls into totals and per-category counts."""
    per_category: dict[str, dict[str, int]] = {}
    for control in controls:
        bucket = per_category.setdefault(
            control.category, {STATUS_MET: 0, STATUS_PARTIAL: 0, STATUS_GAP: 0}
        )
        bucket[control.status] += 1
    categories = [
        CategoryCoverage(
            category=name,
            met=bucket[STATUS_MET],
            partial=bucket[STATUS_PARTIAL],
            gap=bucket[STATUS_GAP],
            total=bucket[STATUS_MET] + bucket[STATUS_PARTIAL] + bucket[STATUS_GAP],
        )
        for name, bucket in sorted(per_category.items())
    ]
    return CoverageSummary(
        total_controls=len(controls),
        met=sum(c.met for c in categories),
        partial=sum(c.partial for c in categories),
        gap=sum(c.gap for c in categories),
        categories=categories,
    )


def _row_to_coverage_control(row) -> CoverageControl:
    status, source, human_confirmed = resolve_system_of_record_status(row[8], row[5])
    return CoverageControl(
        control_id=str(row[0]),
        control_ref=row[1],
        title=row[2],
        category=row[3],
        framework=row[4],
        status=status,
        status_source=source,
        human_confirmed=human_confirmed,
        confidence_level=row[6],
        confidence_score=row[7],
        evidence_count=row[9],
    )
