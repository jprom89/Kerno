"""Unit tests for src/services/dora_roi_validation_service.py.

Plain-English summary
---------------------
Twelve tests verify the deterministic validation service without a live database.
A helper builds a fully-valid DORAExportRow; each test modifies one aspect to
trigger or suppress a specific validation rule. Tests cover: clean rows passing,
empty-list producing a ROI_000 fail issue, missing required fields creating fail
issues, missing optional fields creating warn issues, bad date ordering, invalid
constant values, duplicate list values, overlength fields, and the fail-beats-warn
status hierarchy.

How to run
----------
    pytest tests/unit/services/test_dora_roi_validation_service.py -v
"""

from __future__ import annotations

import pytest

from src.services.dora_roi_export_service import DORAExportRow
from src.services.dora_roi_validation_service import validate_export_rows

_ENTRY_ID = "e0000000-0000-4000-e000-000000000001"


def _make_clean_row(**overrides) -> DORAExportRow:
    """Return a fully-valid DORAExportRow with all required and optional fields set."""
    defaults: dict = {
        "register_entry_id": _ENTRY_ID,
        "provider_name": "AWS",
        "service_name": "EC2 Compute",
        "provider_type": "cloud",
        "criticality_level": "critical",
        "business_function": "Transaction Processing",
        "data_types_joined": "pii; financial",
        "countries_supported_joined": "DE; NL",
        "contract_start_date": "2024-01-01",
        "contract_end_date": "2027-01-01",
        "exit_strategy_summary": "Documented migration path to alternative vendor.",
        "is_active": True,
        "source_record_id": "SRC-001",
    }
    defaults.update(overrides)
    return DORAExportRow(**defaults)


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_validate_export_rows_fail_on_empty_list() -> None:
    """validate_export_rows([]) returns overall_status 'fail' with exactly one issue."""
    summary = validate_export_rows([])
    assert summary.overall_status == "fail", "Empty export must produce fail, not pass"
    assert summary.fail_count == 1
    assert summary.issue_count == 1


def test_validate_export_rows_empty_list_issue_code_is_roi_000() -> None:
    """The single issue from an empty export has issue_code ROI_000 and no entry ID."""
    summary = validate_export_rows([])
    assert len(summary.issues) == 1
    issue = summary.issues[0]
    assert issue.issue_code == "ROI_000", "Empty-export issue must use code ROI_000"
    assert issue.register_entry_id is None, "Empty-export issue has no entry ID"


def test_validate_export_rows_pass_when_clean() -> None:
    """A fully-populated row with no anomalies produces overall_status == 'pass'."""
    summary = validate_export_rows([_make_clean_row()])
    assert summary.overall_status == "pass"
    assert summary.fail_count == 0
    assert summary.warn_count == 0
    assert summary.issue_count == 0


def test_validate_export_rows_fail_on_missing_required() -> None:
    """Empty required fields each produce a fail-severity ValidationIssue."""
    row = _make_clean_row(provider_name="", service_name="", criticality_level="")
    summary = validate_export_rows([row])
    assert summary.overall_status == "fail"
    issue_codes = {i.issue_code for i in summary.issues}
    assert "ROI_001" in issue_codes, "provider_name should fire ROI_001"
    assert "ROI_002" in issue_codes, "service_name should fire ROI_002"
    assert "ROI_004" in issue_codes, "criticality_level should fire ROI_004"
    assert all(
        i.severity == "fail" for i in summary.issues if i.issue_code.startswith("ROI_00")
    )


def test_validate_export_rows_warn_on_missing_optional() -> None:
    """Rows with all four optional fields absent produce exactly four warn issues and no fails."""
    row = _make_clean_row(
        contract_start_date=None,
        contract_end_date=None,
        exit_strategy_summary=None,
        source_record_id=None,
    )
    summary = validate_export_rows([row])
    assert summary.overall_status == "warn"
    assert summary.fail_count == 0
    assert summary.warn_count == 4
    issue_codes = {i.issue_code for i in summary.issues}
    assert {"ROI_012", "ROI_013", "ROI_014", "ROI_015"} == issue_codes


def test_validate_export_rows_fail_on_invalid_dates() -> None:
    """contract_end_date before contract_start_date produces a fail issue (ROI_009)."""
    row = _make_clean_row(
        contract_start_date="2027-01-01",
        contract_end_date="2024-01-01",
    )
    summary = validate_export_rows([row])
    assert summary.overall_status == "fail"
    assert any(i.issue_code == "ROI_009" for i in summary.issues)


def test_validate_export_rows_fail_on_invalid_constants() -> None:
    """Unrecognised criticality_level and provider_type each produce a fail issue."""
    row_bad_criticality = _make_clean_row(criticality_level="mega_critical")
    summary_c = validate_export_rows([row_bad_criticality])
    assert summary_c.overall_status == "fail"
    assert any(i.issue_code == "ROI_010" for i in summary_c.issues)

    row_bad_type = _make_clean_row(provider_type="blockchain")
    summary_t = validate_export_rows([row_bad_type])
    assert summary_t.overall_status == "fail"
    assert any(i.issue_code == "ROI_011" for i in summary_t.issues)


def test_validate_export_rows_warn_on_duplicate_country_values() -> None:
    """Duplicate country entries in countries_supported_joined produce a warn issue (ROI_016)."""
    row = _make_clean_row(countries_supported_joined="DE; NL; DE")
    summary = validate_export_rows([row])
    assert summary.overall_status == "warn"
    assert any(i.issue_code == "ROI_016" for i in summary.issues)


def test_validate_export_rows_warn_on_duplicate_data_types() -> None:
    """Duplicate data type entries in data_types_joined produce a warn issue (ROI_017)."""
    row = _make_clean_row(data_types_joined="pii; financial; pii")
    summary = validate_export_rows([row])
    assert summary.overall_status == "warn"
    assert any(i.issue_code == "ROI_017" for i in summary.issues)


def test_validate_export_rows_warn_on_overlength_fields() -> None:
    """provider_name exceeding MAX_PROVIDER_NAME_LENGTH (255) produces a warn issue (ROI_018)."""
    row = _make_clean_row(provider_name="x" * 256)
    summary = validate_export_rows([row])
    assert summary.overall_status == "warn"
    assert any(i.issue_code == "ROI_018" for i in summary.issues)


def test_overall_status_fail_beats_warn() -> None:
    """When both fail and warn issues exist, overall_status is 'fail', not 'warn'."""
    row = _make_clean_row(
        provider_name="",       # triggers ROI_001 fail
        contract_start_date=None,  # triggers ROI_012 warn
    )
    summary = validate_export_rows([row])
    assert summary.overall_status == "fail"
    assert summary.fail_count > 0
    assert summary.warn_count > 0


def test_overall_status_warn_when_no_fail() -> None:
    """When only warn issues exist and no fail issues, overall_status is 'warn'."""
    row = _make_clean_row(contract_start_date=None)
    summary = validate_export_rows([row])
    assert summary.overall_status == "warn"
    assert summary.fail_count == 0
    assert summary.warn_count >= 1
