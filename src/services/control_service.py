"""control_service.py — Read and load the NIS2 compliance control catalogue.

What:  Provides functions to bulk-load controls, retrieve individual controls,
       filter the catalogue, and persist cross-framework crosswalk links.
       Controls are platform-wide data; no tenant context is required or set.

Why:   KER-103 requires a service that the seed script and the Decision layer
       can call to populate and query the control catalogue. Keeping this logic
       here (rather than inline in callers) gives a single auditable path for
       every write to compliance_controls and control_crosswalks.

How to run or test:
    pytest tests/unit/services/test_control_service.py -v

Important:
    resolve_and_set_tenant_context is NOT called in this file.
    Controls belong to the platform, not to any tenant. Calling it here
    would be incorrect and would break the global-data design.
"""

from __future__ import annotations

import dataclasses
import uuid


# ---------------------------------------------------------------------------
# Input dataclass — the shape callers provide to load_controls.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ControlInput:
    """All required fields for inserting one ComplianceControl row.

    control_id is NOT included — the service generates it via uuid.uuid4().
    All string fields must be non-empty. entity_types must contain at least
    one value from the ENTITY_* constants in compliance_control.py.
    """

    framework: str
    control_ref: str
    category: str
    title: str
    obligation_text: str
    entity_types: list[str]


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SELECT_BY_REF = """
SELECT control_id
FROM compliance_controls
WHERE framework = :framework AND control_ref = :control_ref
"""

_INSERT_CONTROL = """
INSERT INTO compliance_controls
    (control_id, framework, control_ref, category, title,
     obligation_text, entity_types, is_active)
VALUES
    (:control_id, :framework, :control_ref, :category, :title,
     :obligation_text, :entity_types, :is_active)
ON CONFLICT (framework, control_ref) DO NOTHING
"""

_SELECT_CONTROL_BY_ID = """
SELECT control_id, framework, control_ref, category, title,
       obligation_text, entity_types, is_active, created_at
FROM compliance_controls
WHERE control_id = :control_id
"""

_SELECT_CROSSWALKS = """
SELECT crosswalk_id, source_control_id, target_control_id,
       relationship, note, created_at
FROM control_crosswalks
WHERE source_control_id = :source_control_id
"""

_INSERT_CROSSWALK = """
INSERT INTO control_crosswalks
    (crosswalk_id, source_control_id, target_control_id, relationship, note)
VALUES
    (:crosswalk_id, :source_control_id, :target_control_id, :relationship, :note)
ON CONFLICT (source_control_id, target_control_id) DO NOTHING
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_controls(conn, controls: list[ControlInput]) -> list[str]:
    """Insert new controls from the provided list; skip any that already exist.

    For each ControlInput, checks whether (framework, control_ref) already
    exists. If not, inserts a new row with a Python-generated UUID. If yes,
    skips the row silently. Returns the list of control_ids that were actually
    inserted in this call (not previously existing rows).
    """
    inserted_ids: list[str] = []
    for control in controls:
        existing_id = _find_existing_control(conn, control.framework, control.control_ref)
        if existing_id is not None:
            continue
        new_id = _insert_control(conn, control)
        inserted_ids.append(new_id)
    return inserted_ids


def get_control(conn, control_id: str) -> dict | None:
    """Return a single control row as a dict, or None if not found.

    Does not require tenant context — controls are global platform data.
    Returns all columns from compliance_controls for the given UUID string.
    """
    row = conn.execute(
        _SELECT_CONTROL_BY_ID,
        {"control_id": control_id},
    ).fetchone()
    if row is None:
        return None
    return _control_row_to_dict(row)


def list_controls(
    conn,
    framework: str | None = None,
    category: str | None = None,
    entity_type: str | None = None,
) -> list[dict]:
    """Return active controls, optionally filtered by framework, category, or entity type.

    Always applies is_active = True so retired controls never appear in
    coverage calculations. entity_type filter uses the PostgreSQL ANY() operator
    to match controls whose entity_types array contains the given value.
    All three filters are combinable; omitting all returns the full active catalogue.
    """
    sql, params = _build_list_query(framework, category, entity_type)
    rows = conn.execute(sql, params).fetchall()
    return [_control_row_to_dict(row) for row in rows]


def add_crosswalk(
    conn,
    source_control_id: str,
    target_control_id: str,
    relationship: str,
    note: str | None = None,
) -> str:
    """Insert a crosswalk link between two controls; skip if the pair already exists.

    Returns the crosswalk_id that was pre-generated for this call. If the
    (source_control_id, target_control_id) pair already exists (ON CONFLICT),
    the INSERT is silently skipped and the returned ID belongs to the attempted
    row, not the existing one — callers that need the existing ID should query
    get_crosswalks() separately.
    """
    crosswalk_id = str(uuid.uuid4())
    conn.execute(
        _INSERT_CROSSWALK,
        {
            "crosswalk_id": crosswalk_id,
            "source_control_id": source_control_id,
            "target_control_id": target_control_id,
            "relationship": relationship,
            "note": note,
        },
    )
    return crosswalk_id


def get_crosswalks(conn, control_id: str) -> list[dict]:
    """Return all crosswalk rows where source_control_id equals the given ID.

    Returns an empty list when no crosswalks exist for this control. Each dict
    contains crosswalk_id, source_control_id, target_control_id, relationship,
    note, and created_at.
    """
    rows = conn.execute(
        _SELECT_CROSSWALKS,
        {"source_control_id": control_id},
    ).fetchall()
    return [_crosswalk_row_to_dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _find_existing_control(conn, framework: str, control_ref: str) -> str | None:
    """Return the control_id string if (framework, control_ref) exists, else None."""
    row = conn.execute(
        _SELECT_BY_REF,
        {"framework": framework, "control_ref": control_ref},
    ).fetchone()
    return str(row[0]) if row is not None else None


def _insert_control(conn, control: ControlInput) -> str:
    """Insert one control row and return its Python-generated UUID string."""
    new_id = str(uuid.uuid4())
    conn.execute(
        _INSERT_CONTROL,
        {
            "control_id": new_id,
            "framework": control.framework,
            "control_ref": control.control_ref,
            "category": control.category,
            "title": control.title,
            "obligation_text": control.obligation_text,
            "entity_types": control.entity_types,
            "is_active": True,
        },
    )
    return new_id


def _build_list_query(
    framework: str | None,
    category: str | None,
    entity_type: str | None,
) -> tuple[str, dict]:
    """Build the SELECT and params dict for list_controls based on active filters.

    Starts with a base query that always filters is_active = True, then
    appends optional WHERE clauses for framework, category, and entity_type.
    Returns (sql_string, params_dict) ready for conn.execute().
    """
    clauses = ["is_active = :is_active"]
    params: dict = {"is_active": True}

    if framework is not None:
        clauses.append("framework = :framework")
        params["framework"] = framework
    if category is not None:
        clauses.append("category = :category")
        params["category"] = category
    if entity_type is not None:
        clauses.append(":entity_type = ANY(entity_types)")
        params["entity_type"] = entity_type

    where = " AND ".join(clauses)
    sql = (
        "SELECT control_id, framework, control_ref, category, title, "
        "obligation_text, entity_types, is_active, created_at "
        f"FROM compliance_controls WHERE {where}"
    )
    return sql, params


def _control_row_to_dict(row) -> dict:
    """Map a compliance_controls result row (by position) to a plain dict."""
    return {
        "control_id": row[0],
        "framework": row[1],
        "control_ref": row[2],
        "category": row[3],
        "title": row[4],
        "obligation_text": row[5],
        "entity_types": row[6],
        "is_active": row[7],
        "created_at": row[8],
    }


def _crosswalk_row_to_dict(row) -> dict:
    """Map a control_crosswalks result row (by position) to a plain dict."""
    return {
        "crosswalk_id": row[0],
        "source_control_id": row[1],
        "target_control_id": row[2],
        "relationship": row[3],
        "note": row[4],
        "created_at": row[5],
    }
