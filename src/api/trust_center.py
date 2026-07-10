"""Trust Center endpoints (KER-204) — a tenant's public NIS2 status page and its opt-in toggle.

Plain-English summary
---------------------
Enterprise buyers want to see a vendor's compliance posture before signing.
The Trust Center gives each Kerno customer a public page —
GET /trust-center/{tenant_slug}/status — showing ONLY summary numbers: how
many NIS2 controls are met, partially met, or gapped, by category, derived
from the KER-109 system-of-record statuses (human overrides win over AI).

Security posture, in order of importance:
  * The page is PRIVATE BY DEFAULT. A tenant opts in through the
    authenticated PUT /api/v1/trust-center/visibility toggle, gated to the
    compliance_lead, vciso, and platform_engineer roles (KER-202).
  * A private tenant and a nonexistent slug return the IDENTICAL 404 — same
    body, same code path length — so an unauthenticated caller can never
    confirm a company is a Kerno customer.
  * The tenant_id never appears in the URL or any response body; the slug is
    the only public identifier.
  * The slug lookup is the auth-bootstrap read (§13 KER-204 decision 2): it
    reads only the tenants table, which migration 018 leaves unforced for
    exactly this kind of pre-context resolution. All coverage reads then run
    under the resolved tenant's context as usual.

The coverage summary is a fan-out query, so public hits are served from an
in-process cache with a TRUST_CENTER_CACHE_TTL_SECONDS (5-minute) TTL.
Only a cache FILL (a real recomputation) writes the KER-107 ledger entry
(action="trust_center_snapshot") — cache hits change nothing and log nothing.
Visibility is re-checked on EVERY request, before the cache, so flipping a
page private takes effect immediately even while a snapshot is cached.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/api/test_trust_center.py -v
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from config.constants import RbacRole, TRUST_CENTER_CACHE_TTL_SECONDS
from src.api.dependencies import get_conn, get_tenant_id, require_role
from src.api.schemas.trust_center import (
    TrustCenterCategoryStatus,
    TrustCenterStatusResponse,
    TrustCenterVisibilityRequest,
    TrustCenterVisibilityResponse,
)
from src.db.rls import set_tenant_context
from src.services.audit_log import append_audit_entry
from src.services.coverage_service import get_coverage_controls, summarise_coverage

public_router = APIRouter()
admin_router = APIRouter()

# The Trust Center shows NIS2 posture only (KER-204 reg tie: NIS2 Art. 21/23).
_NIS2_FRAMEWORK = "NIS2"

# The one body every 404 on the public route carries — private tenant and
# nonexistent slug must be indistinguishable (KER-204 AC-2/AC-7).
_NOT_FOUND_DETAIL = "not found"

# tenant_slug -> (cache_expiry_monotonic_seconds, response). In-process by
# design (§13 KER-204 decision 4): no new dependency; the documented
# limitation is that each worker process warms its own cache.
_snapshot_cache: dict[str, tuple[float, TrustCenterStatusResponse]] = {}

_SELECT_TENANT_BY_SLUG = """
SELECT tenant_id, trust_center_public
FROM tenants
WHERE tenant_slug = :tenant_slug
"""

_UPDATE_VISIBILITY = """
UPDATE tenants
SET trust_center_public = :trust_center_public
WHERE tenant_id = :tenant_id
RETURNING tenant_slug
"""


@public_router.get("/trust-center/{tenant_slug}/status")
def public_trust_center_status(
    tenant_slug: str,
    conn=Depends(get_conn),
) -> TrustCenterStatusResponse:
    """Return the public NIS2 coverage summary for an opted-in tenant.

    Unauthenticated by design. Resolves the slug server-side, re-checks the
    visibility flag on every request (before the cache), and serves the
    cached snapshot when it is fresh. A private tenant and an unknown slug
    both raise the identical 404 so tenant existence is never confirmed.
    """
    row = conn.execute(_SELECT_TENANT_BY_SLUG, {"tenant_slug": tenant_slug}).fetchone()
    if row is None or not row[1]:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)
    tenant_id = str(row[0])
    return _get_or_build_snapshot(conn, tenant_id, tenant_slug)


@admin_router.put("/visibility")
def update_trust_center_visibility(
    body: TrustCenterVisibilityRequest,
    tenant_id: str = Depends(get_tenant_id),
    rbac_role: str = Depends(
        require_role(
            RbacRole.COMPLIANCE_LEAD, RbacRole.VCISO, RbacRole.PLATFORM_ENGINEER
        )
    ),
    conn=Depends(get_conn),
) -> TrustCenterVisibilityResponse:
    """Switch the authenticated tenant's Trust Center page public or private.

    Gated to compliance_lead, vciso, and platform_engineer (403 otherwise —
    auditors are read-only). The tenant comes from the verified JWT, never the
    request body, and the response confirms the slug the public page lives at.
    """
    set_tenant_context(conn, tenant_id)
    row = conn.execute(
        _UPDATE_VISIBILITY,
        {"trust_center_public": body.public, "tenant_id": tenant_id},
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL)
    return TrustCenterVisibilityResponse(
        tenant_slug=row[0], trust_center_public=body.public
    )


def _get_or_build_snapshot(
    conn, tenant_id: str, tenant_slug: str
) -> TrustCenterStatusResponse:
    """Serve the cached snapshot when fresh; otherwise recompute, log, and cache it.

    Only the recompute path (cache fill) writes the KER-107 ledger entry —
    a cache hit performs no writes at all. The coverage read runs under the
    resolved tenant's context inside get_coverage_controls.
    """
    cached = _snapshot_cache.get(tenant_slug)
    now_monotonic = time.monotonic()
    if cached is not None and cached[0] > now_monotonic:
        return cached[1]
    snapshot = _build_public_snapshot(conn, tenant_id, tenant_slug)
    _record_snapshot_ledger_entry(conn, tenant_id, tenant_slug, snapshot)
    _snapshot_cache[tenant_slug] = (
        now_monotonic + TRUST_CENTER_CACHE_TTL_SECONDS,
        snapshot,
    )
    return snapshot


def _build_public_snapshot(
    conn, tenant_id: str, tenant_slug: str
) -> TrustCenterStatusResponse:
    """Compute the NIS2-only coverage summary for the public page.

    Reuses the KER-109 resolution pass (overrides win over recommendations),
    keeps only NIS2-framework controls, and reduces them to counts — no
    control-level detail survives into the response.
    """
    controls = get_coverage_controls(conn, tenant_id)
    nis2_controls = [c for c in controls if c.framework == _NIS2_FRAMEWORK]
    summary = summarise_coverage(nis2_controls)
    return TrustCenterStatusResponse(
        tenant_slug=tenant_slug,
        total_controls=summary.total_controls,
        met=summary.met,
        partial=summary.partial,
        gap=summary.gap,
        categories=[
            TrustCenterCategoryStatus(
                category=c.category, met=c.met, partial=c.partial,
                gap=c.gap, total=c.total,
            )
            for c in summary.categories
        ],
        generated_at=datetime.now(timezone.utc),
    )


def _record_snapshot_ledger_entry(
    conn, tenant_id: str, tenant_slug: str, snapshot: TrustCenterStatusResponse
) -> None:
    """Append the KER-107 ledger entry for one real snapshot computation (AC-5).

    Written only on cache fill, in the same transaction as the request's
    connection. actor_id None marks it system-generated; the after_state
    records what the public page showed at that moment.
    """
    append_audit_entry(
        conn,
        tenant_id,
        actor_id=None,
        actor_role="system",
        action_type="trust_center_snapshot",
        object_type="trust_center",
        object_id=tenant_slug,
        control_id=None,
        after_state={
            "tenant_slug": tenant_slug,
            "total_controls": snapshot.total_controls,
            "met": snapshot.met,
            "partial": snapshot.partial,
            "gap": snapshot.gap,
        },
    )


def _clear_snapshot_cache() -> None:
    """Empty the in-process snapshot cache (test isolation helper)."""
    _snapshot_cache.clear()
