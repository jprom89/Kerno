"""Queries active dora_register_entries rows for a tenant, normalizes and validates them,
and returns a frozen DORAExportPackage ready for DORA authority filing.

Why:   the filing package must be validated and frozen before it leaves Kerno,
       so an authority never receives a half-formed register.
How:   pytest tests/unit/services/test_dora_roi_export_service.py -v
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone

from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError
from src.services.dora_roi_validation_service import (
    ValidationSummary,
    validate_export_rows,
)

# Separator used when joining list fields into a single export string.
# The validation service uses the same value to split them back for duplicate checks.
_LIST_SEPARATOR = "; "

# Compiled pattern for collapsing repeated internal whitespace to a single space.
_WHITESPACE_RE = re.compile(r"\s+")

# Explicit tenant_id filter alongside RLS — defense in depth for a regulatory submission.
_SELECT_ACTIVE_ENTRIES = """
SELECT register_entry_id, provider_name, service_name, provider_type,
       criticality_level, business_function, data_types, countries_supported,
       contract_start_date, contract_end_date, exit_strategy_summary, is_active,
       source_record_id
FROM dora_register_entries
WHERE is_active = TRUE
  AND tenant_id = :tenant_id
"""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DORAExportRow:
    """One active register entry prepared for export, with list fields pre-joined.

    dates are ISO8601 strings (YYYY-MM-DD) or None.
    data_types_joined and countries_supported_joined join the original list
    values using '; ' as the separator.
    """

    register_entry_id: str
    provider_name: str
    service_name: str
    provider_type: str
    criticality_level: str
    business_function: str
    data_types_joined: str
    countries_supported_joined: str
    contract_start_date: str | None
    contract_end_date: str | None
    exit_strategy_summary: str | None
    is_active: bool
    source_record_id: str | None


@dataclasses.dataclass(frozen=True)
class DORAExportPackage:
    """Complete, validated export package for one tenant and reporting year.

    rows are sorted by provider_name ASC, service_name ASC, register_entry_id ASC.
    validation_summary contains the outcome of running all 20 deterministic rules.
    entry_count equals len(rows).
    """

    tenant_id: str
    generated_at: datetime
    reporting_year: int
    entry_count: int
    rows: list[DORAExportRow]
    validation_summary: ValidationSummary


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_export_rows(conn, tenant_id: str) -> list[DORAExportRow]:
    """Return normalized, sorted DORAExportRows for all active entries of the tenant.
    Raises TenantContextMissingError if tenant_id is falsey or invalid."""
    _guard_tenant(tenant_id)
    set_tenant_context(conn, tenant_id)
    raw_rows = conn.execute(_SELECT_ACTIVE_ENTRIES, {"tenant_id": str(tenant_id)}).fetchall()
    export_rows = [_row_to_export_row(row) for row in raw_rows]
    return _sort_export_rows(export_rows)


def build_export_package(conn, tenant_id: str, reporting_year: int) -> DORAExportPackage:
    """Build and return a complete validated DORAExportPackage for the tenant and year.

    Calls build_export_rows (which handles tenant guard, RLS, and normalization),
    then validates the rows via validate_export_rows and assembles the package.
    generated_at is stamped at call time in UTC.
    Raises TenantContextMissingError if tenant_id is falsey or invalid.
    """
    rows = build_export_rows(conn, tenant_id)
    validation = validate_export_rows(rows)
    now = datetime.now(timezone.utc)
    return DORAExportPackage(
        tenant_id=str(tenant_id),
        generated_at=now,
        reporting_year=reporting_year,
        entry_count=len(rows),
        rows=rows,
        validation_summary=validation,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _guard_tenant(tenant_id: str) -> None:
    """Raise TenantContextMissingError immediately if tenant_id is falsey.

    Provides a fast, clear failure before any DB call is attempted.
    set_tenant_context provides additional UUID-format validation for non-empty values.
    """
    if not tenant_id:
        raise TenantContextMissingError("tenant_id is required for DORA RoI export")


def _normalize_str(value: str | None) -> str | None:
    """Strip leading/trailing whitespace, collapse internal whitespace, return None for blanks.

    Returns None if value is None or becomes empty after stripping. This converts
    empty optional string fields to None per §3.6 normalization rules.
    """
    if value is None:
        return None
    stripped = _WHITESPACE_RE.sub(" ", value.strip())
    return stripped if stripped else None


def _dedup_preserve_order(items: list[str]) -> list[str]:
    """Return a copy of items with duplicates removed, keeping first-seen insertion order.

    Normalization rule from §3.6: deduplicate data_types and countries_supported
    while preserving the order in which unique values first appear.
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _row_to_export_row(row) -> DORAExportRow:
    """Convert one raw dora_register_entries SELECT result row to a normalized DORAExportRow.

    Column positions (0-indexed) match the _SELECT_ACTIVE_ENTRIES query:
    0=register_entry_id, 1=provider_name, 2=service_name, 3=provider_type,
    4=criticality_level, 5=business_function, 6=data_types, 7=countries_supported,
    8=contract_start_date, 9=contract_end_date, 10=exit_strategy_summary,
    11=is_active, 12=source_record_id.
    """
    data_types = _dedup_preserve_order(list(row[6]) if row[6] else [])
    countries = _dedup_preserve_order(list(row[7]) if row[7] else [])
    contract_start = row[8].isoformat() if row[8] is not None else None
    contract_end = row[9].isoformat() if row[9] is not None else None
    return DORAExportRow(
        register_entry_id=str(row[0]),
        provider_name=_normalize_str(row[1]) or "",
        service_name=_normalize_str(row[2]) or "",
        provider_type=_normalize_str(row[3]) or "",
        criticality_level=_normalize_str(row[4]) or "",
        business_function=_normalize_str(row[5]) or "",
        data_types_joined=_LIST_SEPARATOR.join(data_types),
        countries_supported_joined=_LIST_SEPARATOR.join(countries),
        contract_start_date=contract_start,
        contract_end_date=contract_end,
        exit_strategy_summary=_normalize_str(row[10]),
        is_active=row[11],
        source_record_id=_normalize_str(row[12]),
    )


def _sort_export_rows(rows: list[DORAExportRow]) -> list[DORAExportRow]:
    """Sort export rows by provider_name ASC, service_name ASC, register_entry_id ASC.

    Deterministic ordering is required by §3.2 so the same input set always
    produces the same row sequence, enabling reliable diff-based change detection.
    """
    return sorted(rows, key=lambda r: (r.provider_name, r.service_name, r.register_entry_id))
