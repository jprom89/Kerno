"""FastAPI router for the evidence pack export endpoint mounted at /api/v1/export.
Thin translation layer — pack assembly and serialisation live in export_service (KER-111).

Why:   HTTP concerns stay here so the service layer remains framework-free.
How:   pytest tests/unit/api/test_export.py -v
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from src.api.dependencies import get_conn, get_tenant_id
from src.api.rate_limit import limiter
from src.services.export_service import build_evidence_pack, serialise_pack

router = APIRouter()

# Filename characters outside this set are replaced so a hostile family value
# can never inject header syntax into Content-Disposition.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


class _SessionContext:
    """Adapts the authenticated tenant_id to the session interface the service expects.
    Mirrors the KER-106 overrides router adapter (duplicated because that file must not change)."""

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> str:
        return self._tenant_id


@router.get("/evidence-pack")
@limiter.limit("30/minute")
def export_evidence_pack(
    request: Request,
    control_family: str = Query(..., min_length=1),
    tenant_id: str = Depends(get_tenant_id),
    conn=Depends(get_conn),
) -> Response:
    """Return the control family's evidence pack as a downloadable JSON attachment.

    422 when control_family is missing/blank (FastAPI validation) or names a
    family with no active controls; the pack itself is deterministic JSON
    produced by export_service.serialise_pack.
    """
    try:
        pack = build_evidence_pack(conn, _SessionContext(tenant_id), control_family)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    safe_family = _UNSAFE_FILENAME_CHARS.sub("_", control_family)
    filename = (
        f"kerno-evidence-pack-{safe_family}-"
        f"{pack.metadata.generated_at.date().isoformat()}.json"
    )
    return Response(
        content=serialise_pack(pack),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
