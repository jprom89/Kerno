"""Unit tests for src/services/dora_roi_service.py.

Plain-English summary
---------------------
Fifteen tests verify the DORA RoI service without a live database. A spy
connection records every execute() call and returns configurable rows for
SELECT queries. Tests cover: successful create and update, missing-row None
return, ordering and criticality-filter SQL, active-only filter, all six
validation rules, tenant context ordering (SET LOCAL first), the global
windows query bypassing tenant context, and exit-strategy trimming/capping.

How to run
----------
    pytest tests/unit/services/test_dora_roi_service.py -v
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from config.constants import MAX_EXIT_SUMMARY_LENGTH
from src.exceptions import TenantContextMissingError
from src.models.dora_register_entry import CRITICALITY_CRITICAL, CRITICALITY_HIGH
from src.services.dora_roi_service import (
    RegisterEntryInput,
    RegisterEntryOutput,
    create_register_entry,
    get_register_entry,
    list_active_register_entries,
    list_register_entries,
    list_reporting_windows,
    update_register_entry,
)

_TENANT_ID = "c0000000-0000-4000-c000-000000000066"
_ENTRY_ID = "e0000000-0000-4000-e000-000000000001"
_NOW = datetime.now(timezone.utc)
_DATE = date(2024, 1, 1)


# ── Test infrastructure ────────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT result — fetchone/fetchall return empty."""

    def fetchone(self):
        """Return None."""
        return None

    def fetchall(self) -> list:
        """Return an empty list."""
        return []


class _SelectResult:
    """Simulates a SELECT result returning a fixed list of row tuples."""

    def __init__(self, rows: list) -> None:
        """Store the rows to return."""
        self._rows = rows

    def fetchone(self):
        """Return the first row, or None."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        """Return all rows."""
        return self._rows


class _SpyConn:
    """Records execute() calls; raises on SQLAlchemy Session API usage."""

    def __init__(self, responses: list[tuple[str, object]] | None = None) -> None:
        """Initialise with an empty call log and optional response configuration."""
        self.calls: list[tuple] = []
        self._responses = responses or []

    def execute(self, sql, params=None) -> object:
        """Record the call and return the first matching configured response."""
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in str(sql):
                return result
        return _NullResult()

    def add(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.add() called — dora_roi_service must use conn.execute()")

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.flush() called — dora_roi_service must use conn.execute()")


def _make_entry_row(
    entry_id: str = _ENTRY_ID,
    provider_name: str = "AWS",
    criticality_level: str = CRITICALITY_CRITICAL,
    is_active: bool = True,
) -> tuple:
    """Return a 16-column row tuple matching the dora_register_entries column order."""
    return (
        entry_id,               # 0: register_entry_id
        _TENANT_ID,             # 1: tenant_id
        provider_name,          # 2: provider_name
        "EC2 Compute",          # 3: service_name
        "cloud",                # 4: provider_type
        criticality_level,      # 5: criticality_level
        "Transaction Processing",  # 6: business_function
        ["pii", "financial"],   # 7: data_types
        ["DE", "NL"],           # 8: countries_supported
        _DATE,                  # 9: contract_start_date
        None,                   # 10: contract_end_date
        None,                   # 11: exit_strategy_summary
        is_active,              # 12: is_active
        None,                   # 13: source_record_id
        _NOW,                   # 14: created_at
        _NOW,                   # 15: updated_at
    )


def _valid_input(**overrides) -> RegisterEntryInput:
    """Return a RegisterEntryInput with valid defaults, optionally overridden."""
    defaults = {
        "provider_name": "AWS",
        "service_name": "EC2 Compute",
        "provider_type": "cloud",
        "criticality_level": CRITICALITY_CRITICAL,
        "business_function": "Transaction Processing",
        "data_types": ["pii", "financial"],
        "countries_supported": ["DE", "NL"],
        "contract_start_date": _DATE,
        "contract_end_date": None,
        "exit_strategy_summary": None,
        "is_active": True,
        "source_record_id": None,
    }
    defaults.update(overrides)
    return RegisterEntryInput(**defaults)


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_create_register_entry_success() -> None:
    """Valid input persists an INSERT and returns a RegisterEntryOutput."""
    spy = _SpyConn()
    result = create_register_entry(spy, _TENANT_ID, _valid_input())
    assert isinstance(result, RegisterEntryOutput)
    assert result.provider_name == "AWS"
    insert_calls = [sql for sql, _ in spy.calls if "INSERT INTO dora_register_entries" in str(sql)]
    assert len(insert_calls) == 1, "Exactly one INSERT expected"


def test_update_register_entry_success() -> None:
    """Update returns changed fields and issues an UPDATE SQL call."""
    row = _make_entry_row()
    spy = _SpyConn(responses=[("FROM dora_register_entries", _SelectResult([row]))])
    new_input = _valid_input(provider_name="Azure")
    result = update_register_entry(spy, _TENANT_ID, _ENTRY_ID, new_input)
    assert result is not None
    assert result.provider_name == "Azure"
    update_calls = [sql for sql, _ in spy.calls if "UPDATE dora_register_entries" in str(sql)]
    assert len(update_calls) == 1, "Exactly one UPDATE expected"


def test_get_register_entry_missing_returns_none() -> None:
    """get_register_entry returns None when the entry does not exist."""
    spy = _SpyConn()
    result = get_register_entry(spy, _TENANT_ID, "nonexistent-id")
    assert result is None


def test_list_register_entries_orders_by_updated_at_desc() -> None:
    """list_register_entries SQL must include ORDER BY updated_at DESC."""
    spy = _SpyConn()
    list_register_entries(spy, _TENANT_ID)
    list_calls = [
        (sql, params) for sql, params in spy.calls
        if "ORDER BY updated_at DESC" in str(sql)
    ]
    assert len(list_calls) == 1, "Exactly one list query expected"


def test_list_register_entries_filters_by_centrality() -> None:
    """criticality_level filter appears in SQL and params when specified."""
    spy = _SpyConn()
    list_register_entries(spy, _TENANT_ID, criticality_level=CRITICALITY_HIGH)
    filter_calls = [
        (sql, params) for sql, params in spy.calls
        if "criticality_level" in str(sql) and params is not None
    ]
    assert len(filter_calls) == 1
    sql, params = filter_calls[0]
    assert "criticality_level = :criticality_level" in str(sql)
    assert params.get("criticality_level") == CRITICALITY_HIGH


def test_list_active_register_entries_only_returns_active() -> None:
    """list_active_register_entries SQL includes is_active = TRUE filter."""
    spy = _SpyConn()
    list_active_register_entries(spy, _TENANT_ID)
    active_calls = [
        sql for sql, _ in spy.calls
        if "is_active = TRUE" in str(sql)
    ]
    assert len(active_calls) == 1, "SQL must include is_active = TRUE filter"


def test_invalid_provider_type_raises() -> None:
    """provider_type not in allowed values raises ValueError before any SQL."""
    spy = _SpyConn()
    with pytest.raises(ValueError, match="provider_type"):
        create_register_entry(spy, _TENANT_ID, _valid_input(provider_type="blockchain"))
    insert_calls = [sql for sql, _ in spy.calls if "INSERT" in str(sql)]
    assert len(insert_calls) == 0, "No INSERT must be issued on validation failure"


def test_invalid_criticality_level_raises() -> None:
    """criticality_level not in allowed values raises ValueError before any SQL."""
    spy = _SpyConn()
    with pytest.raises(ValueError, match="criticality_level"):
        create_register_entry(spy, _TENANT_ID, _valid_input(criticality_level="very_high"))
    insert_calls = [sql for sql, _ in spy.calls if "INSERT" in str(sql)]
    assert len(insert_calls) == 0


def test_empty_data_types_raises() -> None:
    """Empty data_types list raises ValueError before any SQL is issued."""
    spy = _SpyConn()
    with pytest.raises(ValueError, match="data_types"):
        create_register_entry(spy, _TENANT_ID, _valid_input(data_types=[]))


def test_empty_countries_supported_raises() -> None:
    """Empty countries_supported list raises ValueError before any SQL is issued."""
    spy = _SpyConn()
    with pytest.raises(ValueError, match="countries_supported"):
        create_register_entry(spy, _TENANT_ID, _valid_input(countries_supported=[]))


def test_contract_end_before_start_raises() -> None:
    """contract_end_date before contract_start_date raises ValueError."""
    spy = _SpyConn()
    with pytest.raises(ValueError, match="contract_end_date"):
        create_register_entry(
            spy,
            _TENANT_ID,
            _valid_input(
                contract_start_date=date(2025, 6, 1),
                contract_end_date=date(2024, 1, 1),
            ),
        )


def test_none_tenant_raises() -> None:
    """Falsey tenant_id raises TenantContextMissingError before any SQL or input validation."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        create_register_entry(spy, None, _valid_input())
    assert spy.calls == [], "No SQL must be issued when tenant_id is None"


def test_tenant_context_set_before_query() -> None:
    """SET LOCAL must be the first SQL call — tenant context before any INSERT."""
    spy = _SpyConn()
    create_register_entry(spy, _TENANT_ID, _valid_input())
    assert len(spy.calls) > 0, "At least one SQL call expected"
    assert "SET LOCAL" in str(spy.calls[0][0]), "First SQL call must be SET LOCAL"


def test_list_reporting_windows_does_not_set_tenant_context() -> None:
    """list_reporting_windows is global data and must not call SET LOCAL."""
    spy = _SpyConn()
    list_reporting_windows(spy)
    set_local_calls = [sql for sql, _ in spy.calls if "SET LOCAL" in str(sql)]
    assert len(set_local_calls) == 0, "SET LOCAL must never appear for global reference data"


def test_exit_strategy_trimmed_and_capped() -> None:
    """exit_strategy_summary is trimmed of whitespace and capped to MAX_EXIT_SUMMARY_LENGTH."""
    spy = _SpyConn()
    long_summary = "  " + "x" * 2000 + "  "
    result = create_register_entry(spy, _TENANT_ID, _valid_input(exit_strategy_summary=long_summary))
    assert result.exit_strategy_summary is not None
    assert not result.exit_strategy_summary.startswith(" ")
    assert not result.exit_strategy_summary.endswith(" ")
    assert len(result.exit_strategy_summary) <= MAX_EXIT_SUMMARY_LENGTH
