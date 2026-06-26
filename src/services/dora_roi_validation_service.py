"""dora_roi_validation_service.py — Deterministic quality-check validation for DORA RoI export rows.

What:  Validates a list of DORAExportRow objects against 20 deterministic rules
       (11 FAIL, 9 WARN) and returns a structured ValidationSummary with per-issue
       detail and an overall pass/warn/fail status.

Why:   KER-106 (Document 15, part 2 of 3) requires that every export package include
       quality-check results before any authority submission is attempted. Validation
       is pure (no DB access) and deterministic — the same input always produces the
       same output — so packages can be re-validated at any point without side effects.

How to run or test:
    pytest tests/unit/services/test_dora_roi_validation_service.py -v
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from config.constants import (
    MAX_BUSINESS_FUNCTION_LENGTH,
    MAX_PROVIDER_NAME_LENGTH,
    MAX_SERVICE_NAME_LENGTH,
    VALIDATION_SEVERITY_FAIL,
    VALIDATION_SEVERITY_PASS,
    VALIDATION_SEVERITY_WARN,
)
from src.models.dora_register_entry import (
    CRITICALITY_CRITICAL,
    CRITICALITY_HIGH,
    CRITICALITY_STANDARD,
    PROVIDER_TYPE_CLOUD,
    PROVIDER_TYPE_MANAGED_SERVICE,
    PROVIDER_TYPE_OTHER,
    PROVIDER_TYPE_SOFTWARE,
    PROVIDER_TYPE_TELECOM,
)

if TYPE_CHECKING:
    from src.services.dora_roi_export_service import DORAExportRow

_VALID_CRITICALITY_LEVELS: frozenset[str] = frozenset({
    CRITICALITY_CRITICAL, CRITICALITY_HIGH, CRITICALITY_STANDARD,
})
_VALID_PROVIDER_TYPES: frozenset[str] = frozenset({
    PROVIDER_TYPE_CLOUD, PROVIDER_TYPE_SOFTWARE, PROVIDER_TYPE_MANAGED_SERVICE,
    PROVIDER_TYPE_TELECOM, PROVIDER_TYPE_OTHER,
})

# Separator used when joining list fields into a single string in DORAExportRow.
# Splitting by this value reconstructs the original list for duplicate detection.
_LIST_SEPARATOR = "; "


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ValidationIssue:
    """One validation finding attached to a specific export row or the package as a whole.

    issue_code is a stable identifier (e.g. ROI_001) callers can match on.
    severity is one of VALIDATION_SEVERITY_PASS / WARN / FAIL from config.constants.
    register_entry_id is None when the issue applies to the package rather than a row.
    """

    issue_code: str
    severity: str
    message: str
    register_entry_id: str | None


@dataclasses.dataclass(frozen=True)
class ValidationSummary:
    """Aggregate outcome of validating all rows in an export package.

    overall_status applies the fail > warn > pass hierarchy from §3.5:
    fail if any fail issue exists, else warn if any warn issue exists, else pass.
    issue_count is the total number of ValidationIssue objects in issues.
    """

    overall_status: str
    issue_count: int
    pass_count: int
    warn_count: int
    fail_count: int
    issues: list[ValidationIssue]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_export_rows(rows: list[DORAExportRow]) -> ValidationSummary:
    """Run all 20 deterministic validation rules over each row and return a summary.

    Returns a single FAIL issue (ROI_000) immediately when rows is empty, because
    a zero-entry export is always a compliance defect. For non-empty input applies
    rules in this order: required fields (1–7), active status (8), date ordering (9),
    allowed constants (10–11), optional fields (12–15), duplicate list values (16–17),
    field length limits (18–20). No database access — safe to call repeatedly.
    """
    if not rows:
        return _empty_export_summary()
    issues: list[ValidationIssue] = []
    for row in rows:
        issues.extend(_check_required_fields(row))
        issues.extend(_check_active_status(row))
        issues.extend(_check_date_order(row))
        issues.extend(_check_allowed_constants(row))
        issues.extend(_check_optional_fields(row))
        issues.extend(_check_duplicate_values(row))
        issues.extend(_check_field_lengths(row))
    return _build_summary(issues)


# ---------------------------------------------------------------------------
# Empty-export guard (rule 0)
# ---------------------------------------------------------------------------


def _empty_export_summary() -> ValidationSummary:
    """Return a FAIL ValidationSummary for a package that contains no rows.

    A zero-entry export is always a compliance defect: the DORA RoI requires at
    least one active ICT third-party relationship. Returning FAIL here prevents
    an empty export from silently propagating to 'ready' submission status.
    """
    issue = ValidationIssue(
        issue_code="ROI_000",
        severity=VALIDATION_SEVERITY_FAIL,
        message=(
            "Export contains no active register entries; "
            "at least one entry is required for a valid DORA RoI filing."
        ),
        register_entry_id=None,
    )
    return ValidationSummary(
        overall_status=VALIDATION_SEVERITY_FAIL,
        issue_count=1,
        pass_count=0,
        warn_count=0,
        fail_count=1,
        issues=[issue],
    )


# ---------------------------------------------------------------------------
# Fail rule helpers (rules 1–11)
# ---------------------------------------------------------------------------


def _check_required_fields(row: DORAExportRow) -> list[ValidationIssue]:
    """Return fail issues for any missing required string or list field (rules 1–7).

    Checks provider_name, service_name, provider_type, criticality_level,
    business_function, data_types_joined, and countries_supported_joined.
    An empty string is treated as missing.
    """
    entry_id = row.register_entry_id
    issues: list[ValidationIssue] = []
    if not row.provider_name:
        issues.append(ValidationIssue("ROI_001", VALIDATION_SEVERITY_FAIL, "provider_name is missing", entry_id))
    if not row.service_name:
        issues.append(ValidationIssue("ROI_002", VALIDATION_SEVERITY_FAIL, "service_name is missing", entry_id))
    if not row.provider_type:
        issues.append(ValidationIssue("ROI_003", VALIDATION_SEVERITY_FAIL, "provider_type is missing", entry_id))
    if not row.criticality_level:
        issues.append(ValidationIssue("ROI_004", VALIDATION_SEVERITY_FAIL, "criticality_level is missing", entry_id))
    if not row.business_function:
        issues.append(ValidationIssue("ROI_005", VALIDATION_SEVERITY_FAIL, "business_function is missing", entry_id))
    if not row.data_types_joined:
        issues.append(ValidationIssue("ROI_006", VALIDATION_SEVERITY_FAIL, "data_types is empty", entry_id))
    if not row.countries_supported_joined:
        issues.append(ValidationIssue("ROI_007", VALIDATION_SEVERITY_FAIL, "countries_supported is empty", entry_id))
    return issues


def _check_active_status(row: DORAExportRow) -> list[ValidationIssue]:
    """Return a fail issue if the row is not active (rule 8).

    build_export_rows enforces the active-only filter in SQL; this is a
    defensive check for rows constructed outside the standard export path.
    """
    if not row.is_active:
        return [ValidationIssue(
            "ROI_008", VALIDATION_SEVERITY_FAIL,
            "Inactive entry must not appear in an export package",
            row.register_entry_id,
        )]
    return []


def _check_date_order(row: DORAExportRow) -> list[ValidationIssue]:
    """Return a fail issue if contract_end_date precedes contract_start_date (rule 9).

    Skips the check when either date is absent. Dates are ISO8601 strings
    (YYYY-MM-DD) and compare correctly using standard string ordering.
    """
    if row.contract_start_date is None or row.contract_end_date is None:
        return []
    # ISO8601 YYYY-MM-DD strings sort lexicographically in the same order as dates
    if row.contract_end_date < row.contract_start_date:
        return [ValidationIssue(
            "ROI_009", VALIDATION_SEVERITY_FAIL,
            "contract_end_date is before contract_start_date",
            row.register_entry_id,
        )]
    return []


def _check_allowed_constants(row: DORAExportRow) -> list[ValidationIssue]:
    """Return fail issues if criticality_level or provider_type are not allowed values (rules 10–11).

    Only fires when the field is non-empty; missing-field failures are
    already captured by _check_required_fields (rules 1–7).
    """
    issues: list[ValidationIssue] = []
    entry_id = row.register_entry_id
    if row.criticality_level and row.criticality_level not in _VALID_CRITICALITY_LEVELS:
        issues.append(ValidationIssue(
            "ROI_010", VALIDATION_SEVERITY_FAIL,
            f"criticality_level '{row.criticality_level}' is not an allowed value",
            entry_id,
        ))
    if row.provider_type and row.provider_type not in _VALID_PROVIDER_TYPES:
        issues.append(ValidationIssue(
            "ROI_011", VALIDATION_SEVERITY_FAIL,
            f"provider_type '{row.provider_type}' is not an allowed value",
            entry_id,
        ))
    return issues


# ---------------------------------------------------------------------------
# Warn rule helpers (rules 12–20)
# ---------------------------------------------------------------------------


def _check_optional_fields(row: DORAExportRow) -> list[ValidationIssue]:
    """Return warn issues for any missing optional field (rules 12–15).

    Checks contract_start_date, contract_end_date, exit_strategy_summary,
    and source_record_id. These are optional but expected for a complete
    ESA submission; missing values reduce the quality of the package.
    """
    entry_id = row.register_entry_id
    issues: list[ValidationIssue] = []
    if row.contract_start_date is None:
        issues.append(ValidationIssue("ROI_012", VALIDATION_SEVERITY_WARN, "contract_start_date is missing", entry_id))
    if row.contract_end_date is None:
        issues.append(ValidationIssue("ROI_013", VALIDATION_SEVERITY_WARN, "contract_end_date is missing", entry_id))
    if row.exit_strategy_summary is None:
        issues.append(ValidationIssue("ROI_014", VALIDATION_SEVERITY_WARN, "exit_strategy_summary is missing", entry_id))
    if row.source_record_id is None:
        issues.append(ValidationIssue("ROI_015", VALIDATION_SEVERITY_WARN, "source_record_id is missing", entry_id))
    return issues


def _check_duplicate_values(row: DORAExportRow) -> list[ValidationIssue]:
    """Return warn issues if countries_supported or data_types contain duplicate values (rules 16–17).

    Splits each joined field by the list separator, normalizes each part to
    lowercase-stripped form, then checks whether the unique count is less than
    the total count. A duplicate here is a data-quality anomaly because the
    export normalizer should have deduplicated before building the row.
    """
    issues: list[ValidationIssue] = []
    entry_id = row.register_entry_id
    if row.countries_supported_joined:
        parts = [p.strip().lower() for p in row.countries_supported_joined.split(_LIST_SEPARATOR)]
        if len(parts) != len(set(parts)):
            issues.append(ValidationIssue(
                "ROI_016", VALIDATION_SEVERITY_WARN,
                "countries_supported contains duplicate values",
                entry_id,
            ))
    if row.data_types_joined:
        parts = [p.strip().lower() for p in row.data_types_joined.split(_LIST_SEPARATOR)]
        if len(parts) != len(set(parts)):
            issues.append(ValidationIssue(
                "ROI_017", VALIDATION_SEVERITY_WARN,
                "data_types contains duplicate values",
                entry_id,
            ))
    return issues


def _check_field_lengths(row: DORAExportRow) -> list[ValidationIssue]:
    """Return warn issues if provider_name, service_name, or business_function exceed their limits (rules 18–20).

    Length limits come from config.constants: MAX_PROVIDER_NAME_LENGTH (255),
    MAX_SERVICE_NAME_LENGTH (255), MAX_BUSINESS_FUNCTION_LENGTH (500).
    Only fires when the field is non-empty.
    """
    issues: list[ValidationIssue] = []
    entry_id = row.register_entry_id
    if row.provider_name and len(row.provider_name) > MAX_PROVIDER_NAME_LENGTH:
        issues.append(ValidationIssue(
            "ROI_018", VALIDATION_SEVERITY_WARN,
            f"provider_name exceeds {MAX_PROVIDER_NAME_LENGTH} characters",
            entry_id,
        ))
    if row.service_name and len(row.service_name) > MAX_SERVICE_NAME_LENGTH:
        issues.append(ValidationIssue(
            "ROI_019", VALIDATION_SEVERITY_WARN,
            f"service_name exceeds {MAX_SERVICE_NAME_LENGTH} characters",
            entry_id,
        ))
    if row.business_function and len(row.business_function) > MAX_BUSINESS_FUNCTION_LENGTH:
        issues.append(ValidationIssue(
            "ROI_020", VALIDATION_SEVERITY_WARN,
            f"business_function exceeds {MAX_BUSINESS_FUNCTION_LENGTH} characters",
            entry_id,
        ))
    return issues


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def _build_summary(issues: list[ValidationIssue]) -> ValidationSummary:
    """Assemble a ValidationSummary from a flat list of issues.

    Counts issues by severity, applies the fail > warn > pass status hierarchy
    defined in §3.5, and wraps everything in a frozen ValidationSummary.
    """
    fail_count = sum(1 for i in issues if i.severity == VALIDATION_SEVERITY_FAIL)
    warn_count = sum(1 for i in issues if i.severity == VALIDATION_SEVERITY_WARN)
    pass_count = sum(1 for i in issues if i.severity == VALIDATION_SEVERITY_PASS)
    if fail_count > 0:
        overall_status = VALIDATION_SEVERITY_FAIL
    elif warn_count > 0:
        overall_status = VALIDATION_SEVERITY_WARN
    else:
        overall_status = VALIDATION_SEVERITY_PASS
    return ValidationSummary(
        overall_status=overall_status,
        issue_count=len(issues),
        pass_count=pass_count,
        warn_count=warn_count,
        fail_count=fail_count,
        issues=list(issues),
    )
