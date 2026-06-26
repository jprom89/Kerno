"""Ten unit tests for dora_roi_export_service covering active-entry querying, field
normalization, validation, tenant guard enforcement, and the explicit tenant_id filter."""

from __future__ import annotations

import pytest

from src.exceptions import TenantContextMissingError
from src.services.dora_roi_export_service import build_export_package, build_export_rows

_TENANT_ID = "c0000000-0000-4000-a000-000000000066"


# ── Test infrastructure ────────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT result — fetchall returns an empty list."""

    def fetchall(self) -> list:
        """Return an empty list."""
        return []

    def fetchone(self):
        """Return None."""
        return None


class _SelectResult:
    """Simulates a SELECT result returning a fixed list of row tuples."""

    def __init__(self, rows: list) -> None:
        """Store the rows to return from fetchall."""
        self._rows = rows

    def fetchall(self) -> list:
        """Return all configured rows."""
        return self._rows

    def fetchone(self):
        """Return the first row, or None."""
        return self._rows[0] if self._rows else None


class _SpyConn:
    """Records execute() calls; raises on SQLAlchemy Session API usage."""

    def __init__(self, responses: list[tuple[str, object]] | None = None) -> None:
        """Initialise with an empty call log and optional response configuration."""
        self.calls: list[tuple] = []
        self._responses = responses or []

    def execute(self, sql, params=None) -> object:
        """Record the call and return the first configured response whose fragment matches."""
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in str(sql):
                return result
        return _NullResult()

    def add(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.add() called — dora_roi_export_service must use conn.execute()")

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.flush() called — dora_roi_export_service must use conn.execute()")


def _make_db_row(
    entry_id: str = "e0000000-0000-4000-e000-000000000001",
    provider_name: str = "AWS",
    service_name: str = "EC2 Compute",
    provider_type: str = "cloud",
    criticality_level: str = "critical",
    business_function: str = "Transaction Processing",
    data_types: list[str] | None = None,
    countries_supported: list[str] | None = None,
    contract_start_date=None,
    contract_end_date=None,
    exit_strategy_summary: str | None = None,
    is_active: bool = True,
    source_record_id: str | None = None,
) -> tuple:
    """Return a 13-column row tuple matching the _SELECT_ACTIVE_ENTRIES column order."""
    return (
        entry_id,
        provider_name,
        service_name,
        provider_type,
        criticality_level,
        business_function,
        data_types if data_types is not None else ["pii", "financial"],
        countries_supported if countries_supported is not None else ["DE", "NL"],
        contract_start_date,
        contract_end_date,
        exit_strategy_summary,
        is_active,
        source_record_id,
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_build_export_rows_only_active_entries() -> None:
    """The SQL query must include is_active = TRUE to exclude inactive entries."""
    spy = _SpyConn()
    build_export_rows(spy, _TENANT_ID)
    active_filter_calls = [
        sql for sql, _ in spy.calls
        if "is_active = TRUE" in str(sql)
    ]
    assert len(active_filter_calls) == 1, "SQL must include is_active = TRUE filter"


def test_build_export_rows_sorted_stably() -> None:
    """Rows are returned sorted by provider_name ASC, service_name ASC, entry_id ASC."""
    row1 = _make_db_row(entry_id="e3", provider_name="Z Corp", service_name="Alpha")
    row2 = _make_db_row(entry_id="e2", provider_name="A Corp", service_name="Beta")
    row3 = _make_db_row(entry_id="e1", provider_name="A Corp", service_name="Alpha")
    spy = _SpyConn(responses=[
        ("FROM dora_register_entries", _SelectResult([row1, row2, row3])),
    ])
    result = build_export_rows(spy, _TENANT_ID)
    assert result[0].provider_name == "A Corp"
    assert result[0].service_name == "Alpha"
    assert result[1].provider_name == "A Corp"
    assert result[1].service_name == "Beta"
    assert result[2].provider_name == "Z Corp"


def test_build_export_rows_joins_list_fields() -> None:
    """data_types and countries_supported are joined with '; ' as the separator."""
    row = _make_db_row(data_types=["pii", "financial"], countries_supported=["DE", "NL"])
    spy = _SpyConn(responses=[
        ("FROM dora_register_entries", _SelectResult([row])),
    ])
    result = build_export_rows(spy, _TENANT_ID)
    assert result[0].data_types_joined == "pii; financial"
    assert result[0].countries_supported_joined == "DE; NL"


def test_build_export_rows_normalizes_duplicates() -> None:
    """Duplicate list values are removed during normalization, preserving first-seen order."""
    row = _make_db_row(
        data_types=["pii", "financial", "pii"],
        countries_supported=["DE", "NL", "DE"],
    )
    spy = _SpyConn(responses=[
        ("FROM dora_register_entries", _SelectResult([row])),
    ])
    result = build_export_rows(spy, _TENANT_ID)
    assert result[0].data_types_joined == "pii; financial"
    assert result[0].countries_supported_joined == "DE; NL"


def test_build_export_package_includes_validation_summary() -> None:
    """The returned DORAExportPackage has a non-None validation_summary with an overall_status."""
    row = _make_db_row()
    spy = _SpyConn(responses=[
        ("FROM dora_register_entries", _SelectResult([row])),
    ])
    package = build_export_package(spy, _TENANT_ID, reporting_year=2024)
    assert package.validation_summary is not None
    assert package.validation_summary.overall_status in {"pass", "warn", "fail"}


def test_build_export_package_counts_rows() -> None:
    """entry_count in the package equals the number of rows returned."""
    row1 = _make_db_row(entry_id="e1", provider_name="AWS")
    row2 = _make_db_row(entry_id="e2", provider_name="Azure")
    spy = _SpyConn(responses=[
        ("FROM dora_register_entries", _SelectResult([row1, row2])),
    ])
    package = build_export_package(spy, _TENANT_ID, reporting_year=2024)
    assert package.entry_count == 2
    assert len(package.rows) == 2


def test_falsey_tenant_raises() -> None:
    """Passing None as tenant_id raises TenantContextMissingError before any DB call."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        build_export_rows(spy, None)
    select_calls = [sql for sql, _ in spy.calls if "FROM dora_register_entries" in str(sql)]
    assert len(select_calls) == 0, "No SELECT must be issued when tenant_id is None"


def test_tenant_context_set_before_query() -> None:
    """SET LOCAL must be the first SQL call — RLS context before the SELECT."""
    spy = _SpyConn()
    build_export_rows(spy, _TENANT_ID)
    assert len(spy.calls) > 0, "At least one SQL call expected"
    assert "SET LOCAL" in str(spy.calls[0][0]), "First SQL call must be SET LOCAL"


def test_no_session_api_used() -> None:
    """conn.add() and conn.flush() must never be called by the export service."""
    row = _make_db_row()
    spy = _SpyConn(responses=[
        ("FROM dora_register_entries", _SelectResult([row])),
    ])
    # _SpyConn.add() and _SpyConn.flush() raise AssertionError if called;
    # reaching this assertion without error proves neither was invoked.
    result = build_export_rows(spy, _TENANT_ID)
    assert result is not None


def test_build_export_rows_passes_tenant_id_to_select() -> None:
    spy = _SpyConn()
    build_export_rows(spy, _TENANT_ID)
    select_calls = [
        (sql, params) for sql, params in spy.calls
        if "FROM dora_register_entries" in str(sql)
    ]
    assert len(select_calls) == 1
    _, params = select_calls[0]
    assert params is not None
    assert params.get("tenant_id") == _TENANT_ID
