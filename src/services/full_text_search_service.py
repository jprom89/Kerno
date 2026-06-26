"""full_text_search_service.py — Full-text search over ingested context records.

What:  Searches the context_records table using PostgreSQL's built-in full-text
       search (to_tsvector / plainto_tsquery) on the title and body columns.
       Returns matching records ranked by relevance, with optional filters for
       source_system and record_type.

Why:   KER-104 AC-3 requires that evidence retrieval supports full-text filters.
       Keeping this logic in a dedicated service (rather than inline in
       evidence_service) keeps each module under the 40-line function limit and
       makes the search behaviour testable in isolation.

How to run or test:
    pytest tests/unit/services/test_full_text_search_service.py -v
"""

from __future__ import annotations

from config.constants import FULL_TEXT_SEARCH_LIMIT
from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError  # noqa: F401  re-exported for callers

# ---------------------------------------------------------------------------
# SQL fragment constants
# ---------------------------------------------------------------------------

# The to_tsvector expression is repeated (once in WHERE, once in ts_rank).
# Naming it avoids the duplication and makes the intent clear.
_TSVECTOR_EXPR = (
    "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))"
)


def search_records(
    conn,
    tenant_id,
    query: str,
    source_system: str | None = None,
    record_type: str | None = None,
    limit: int = FULL_TEXT_SEARCH_LIMIT,
) -> list[dict]:
    """Search context_records by full-text query, returning ranked results.

    Uses PostgreSQL's plainto_tsquery against the combined title+body tsvector.
    Only returns records where is_deleted = FALSE. Applies optional source_system
    and record_type filters. Results are ordered by ts_rank DESC so the most
    relevant records appear first. At most limit rows are returned.
    Raises TenantContextMissingError if tenant_id is None or empty.
    """
    set_tenant_context(conn, tenant_id)
    sql, params = _build_search_query(query, source_system, record_type, limit)
    rows = conn.execute(sql, params).fetchall()
    return [_record_row_to_dict(row) for row in rows]


def _build_search_query(
    query: str,
    source_system: str | None,
    record_type: str | None,
    limit: int,
) -> tuple[str, dict]:
    """Build the full-text search SELECT with any active filters.

    Always filters is_deleted = FALSE and applies the plainto_tsquery match.
    Optional source_system and record_type filters are added when provided.
    Returns (sql_string, params_dict) ready for conn.execute().
    """
    tsquery_expr = f"plainto_tsquery('english', :query)"
    base = (
        "SELECT record_id, source_system, external_id, record_type, "
        "title, body, fetched_at, content_hash "
        "FROM context_records "
        f"WHERE is_deleted = FALSE "
        f"AND {_TSVECTOR_EXPR} @@ {tsquery_expr}"
    )
    clauses: list[str] = []
    params: dict = {"query": query, "limit": limit}

    if source_system is not None:
        clauses.append("source_system = :source_system")
        params["source_system"] = source_system
    if record_type is not None:
        clauses.append("record_type = :record_type")
        params["record_type"] = record_type

    filter_sql = (" AND " + " AND ".join(clauses)) if clauses else ""
    order_sql = f" ORDER BY ts_rank({_TSVECTOR_EXPR}, {tsquery_expr}) DESC LIMIT :limit"
    return base + filter_sql + order_sql, params


def _record_row_to_dict(row) -> dict:
    """Map a context_records result row (by position) to a plain dict."""
    return {
        "record_id": str(row[0]),
        "source_system": row[1],
        "external_id": row[2],
        "record_type": row[3],
        "title": row[4],
        "body": row[5],
        "fetched_at": row[6],
        "content_hash": row[7],
    }
