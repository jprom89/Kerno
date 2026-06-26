# PROMPT_doc15_dora_roi_export_validation.md
# Document 15 — DORA Register of Information Export + Validation
# Spec version: 1.0 | Status: Authoritative
# Covers: KER-106 (part 2 of 3)
# Supersedes: any inline description of DORA RoI export or validation in Claude prompts

---

## 1. Purpose

This document specifies Document 15 of the Kerno compliance copilot codebase.

Document 15 is the second part of the DORA Register of Information sequence.
It builds on the live register foundation created in Document 14 and adds:

- deterministic export structures for RoI data,
- tenant-scoped export generation,
- and quality-check validation that prepares the register for later authority submission.

This document deliberately stops short of transmission to regulators.
The design principle is:

**Document 14 stores the live register. Document 15 turns that register into a
portable, validated reporting package. Document 16 will handle actual filing.**

---

## 2. Scope — KER-106 (authoritative for this document)

This document is complete when all of the following are true:

- A tenant can export current active DORA RoI entries into a deterministic package.
- The export includes structured rows ready for later xBRL-CSV mapping.
- The package includes validation results with pass/warn/fail status.
- Validation is deterministic and rule-based; no LLM logic.
- Export and validation can be run repeatedly and yield stable results for the same input set.

Out of scope for this document:
- authority API submission
- scheduling/report window orchestration
- file upload/download endpoints
- actual xBRL taxonomy generation
- zipped archive packaging
- background jobs

---

## 3. Export and validation model

### 3.1 Export design philosophy

Sprint 2 should not implement the full official ESA xBRL-CSV taxonomy stack.
That is too heavy for this phase. Instead, Document 15 creates an intermediate,
deterministic export package that is **xBRL-CSV-ready**.

This means:
- field naming must be stable,
- row ordering must be deterministic,
- validations must identify missing/invalid data now,
- and Document 16 can later wrap this output into the final authority-facing format.

### 3.2 Package structure

The export service returns a frozen dataclass:

#### DORAExportPackage
Fields:
- `tenant_id: str`
- `generated_at: datetime`
- `reporting_year: int`
- `entry_count: int`
- `rows: list[DORAExportRow]`
- `validation_summary: ValidationSummary`

#### DORAExportRow
Each row represents one active register entry prepared for export.

Fields:
- `register_entry_id: str`
- `provider_name: str`
- `service_name: str`
- `provider_type: str`
- `criticality_level: str`
- `business_function: str`
- `data_types_joined: str`
- `countries_supported_joined: str`
- `contract_start_date: str | None`
- `contract_end_date: str | None`
- `exit_strategy_summary: str | None`
- `is_active: bool`
- `source_record_id: str | None`

Rules:
- `data_types_joined` joins list items using `"; "`
- `countries_supported_joined` joins list items using `"; "`
- dates are ISO8601 strings (`YYYY-MM-DD`) or `None`
- rows must be sorted by `provider_name ASC`, then `service_name ASC`, then `register_entry_id ASC`

### 3.3 Validation structures

Create these frozen dataclasses in `src/services/dora_roi_validation_service.py`:

#### ValidationIssue
Fields:
- `issue_code: str`
- `severity: str`  (`pass`, `warn`, `fail`)
- `message: str`
- `register_entry_id: str | None`

#### ValidationSummary
Fields:
- `overall_status: str` (`pass`, `warn`, `fail`)
- `issue_count: int`
- `pass_count: int`
- `warn_count: int`
- `fail_count: int`
- `issues: list[ValidationIssue]`

### 3.4 Validation rules

Document 15 does **not** implement all 116 ESA rules. It implements the
**Sprint 2 validation core** that catches the most expensive preventable failures.

Required deterministic rules:

#### Fail rules
1. Missing provider_name
2. Missing service_name
3. Missing provider_type
4. Missing criticality_level
5. Missing business_function
6. Empty data_types
7. Empty countries_supported
8. Inactive entry included in export (should never happen; defensive fail)
9. contract_end_date before contract_start_date
10. criticality_level not in allowed constants
11. provider_type not in allowed constants

#### Warn rules
12. contract_start_date missing
13. contract_end_date missing
14. exit_strategy_summary missing
15. source_record_id missing
16. countries_supported contains duplicate values after normalization
17. data_types contains duplicate values after normalization
18. provider_name length exceeds `MAX_PROVIDER_NAME_LENGTH`
19. service_name length exceeds `MAX_SERVICE_NAME_LENGTH`
20. business_function length exceeds `MAX_BUSINESS_FUNCTION_LENGTH`

### 3.5 Overall status logic

- `overall_status = "fail"` if any fail issue exists
- else `overall_status = "warn"` if any warn issue exists
- else `overall_status = "pass"`

### 3.6 Normalization rules for export

Before validation and export:
- trim strings
- collapse repeated internal whitespace to single spaces
- deduplicate `data_types` while preserving first-seen order
- deduplicate `countries_supported` while preserving first-seen order
- treat empty optional strings as `None`

---

## 4. Service behavior

### 4.1 `dora_roi_validation_service.py`

Public method:
- `validate_export_rows(rows: list[DORAExportRow]) -> ValidationSummary`

Requirements:
- no DB access
- pure deterministic validation
- use private helper functions for each rule cluster
- all functions under 40 lines
- module docstring and full docstrings for all functions

### 4.2 `dora_roi_export_service.py`

Public methods:
- `build_export_package(conn, tenant_id: str, reporting_year: int) -> DORAExportPackage`
- `build_export_rows(conn, tenant_id: str) -> list[DORAExportRow]`

Requirements:
- tenant-scoped DB access only
- call `set_tenant_context` before DB access
- if tenant_id is falsey, raise `TenantContextMissingError` from `src.exceptions`
- only active register entries are exported
- use `dora_roi_validation_service.validate_export_rows()` internally
- stable row ordering
- all functions under 40 lines
- use private helpers for:
  - tenant guard
  - row normalization
  - row conversion
  - sorting

### 4.3 Query rules

`build_export_rows()` must query only the fields needed for export from
`dora_register_entries` where `is_active = true`.

No joins in this document.

---

## 5. Files in scope

### 5.1 `src/services/dora_roi_export_service.py` — NEW

Requirements:
- module docstring
- define `DORAExportRow` and `DORAExportPackage`
- implement the public methods from §4.2
- all DB operations via `conn.execute(sql, dict)`
- no Session API
- all functions under 40 lines

### 5.2 `src/services/dora_roi_validation_service.py` — NEW

Requirements:
- module docstring
- define `ValidationIssue` and `ValidationSummary`
- implement deterministic validation from §3.4 and §3.5
- no DB access
- all functions under 40 lines

### 5.3 `config/constants.py` — extend only

Add if not already present:
- `MAX_PROVIDER_NAME_LENGTH = 255`
- `MAX_SERVICE_NAME_LENGTH = 255`
- `MAX_BUSINESS_FUNCTION_LENGTH = 500`
- `VALIDATION_SEVERITY_PASS = "pass"`
- `VALIDATION_SEVERITY_WARN = "warn"`
- `VALIDATION_SEVERITY_FAIL = "fail"`

Do not remove or rename existing constants.

### 5.4 `tests/unit/services/test_dora_roi_export_service.py` — NEW

No live database.

Required tests:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_build_export_rows_only_active_entries | inactive rows excluded |
| 2 | test_build_export_rows_sorted_stably | provider/service/id ordering enforced |
| 3 | test_build_export_rows_joins_list_fields | joined strings use `; ` |
| 4 | test_build_export_rows_normalizes_duplicates | duplicate list values deduped preserving order |
| 5 | test_build_export_package_includes_validation_summary | validation summary included |
| 6 | test_build_export_package_counts_rows | entry_count matches rows |
| 7 | test_falsey_tenant_raises | tenant guard enforced |
| 8 | test_tenant_context_set_before_query | first DB call sets tenant context |
| 9 | test_no_session_api_used | conn.add and conn.flush never called |

### 5.5 `tests/unit/services/test_dora_roi_validation_service.py` — NEW

Required tests:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_validate_export_rows_pass_when_clean | clean rows -> overall pass |
| 2 | test_validate_export_rows_fail_on_missing_required | required fields create fail issues |
| 3 | test_validate_export_rows_warn_on_missing_optional | optional fields create warn issues |
| 4 | test_validate_export_rows_fail_on_invalid_dates | bad date ordering fails |
| 5 | test_validate_export_rows_fail_on_invalid_constants | invalid provider/criticality fails |
| 6 | test_validate_export_rows_warn_on_duplicate_country_values | duplicate countries warn |
| 7 | test_validate_export_rows_warn_on_duplicate_data_types | duplicate data types warn |
| 8 | test_validate_export_rows_warn_on_overlength_fields | long strings warn |
| 9 | test_overall_status_fail_beats_warn | fail dominates overall status |
| 10 | test_overall_status_warn_when_no_fail | warn returned when only warnings exist |

---

## 6. Gate checks (apply to every file produced)

| Check | Rule |
|---|---|
| Module docstring present | Answers What, Why, and How to run or test |
| All functions have docstrings | No exceptions |
| No spec notation in variable names | No Greek letters, subscripts, raw spec symbols |
| No magic numbers | Numeric literals named in constants.py where appropriate |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule followed | `set_tenant_context` before tenant DB access |
| TenantContextMissingError from src.exceptions | Not from any other module |

---

## 7. Out of scope for Document 15

- Actual xBRL taxonomy packaging
- ESA submission files
- Competent authority API calls
- reporting-window orchestration
- background scheduling
- archive/zip generation
- persistence of export artifacts

---

## 8. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md | Highest |
| This file | Authoritative for Document 15 scope |
| PROMPT_doc14_dora_roi_live_register.md | Upstream live register foundation |
| KERNO_STRATEGY.md | Context only |
