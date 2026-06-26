# PROMPT_doc14_dora_roi_live_register.md
# Document 14 — DORA Register of Information Live Register Foundation
# Spec version: 1.0 | Status: Authoritative
# Covers: KER-106 (part 1 of 3)
# Supersedes: any inline description of DORA RoI in Claude prompts

---

## 1. Purpose

This document specifies Document 14 of the Kerno compliance copilot codebase.

Document 14 begins Sprint 2 by implementing the live data foundation for the
DORA Register of Information (RoI). The design principle is critical:

**The RoI is not an annual export artifact. It is a continuously maintained,
tenant-scoped live register that can later be exported into the ESA xBRL-CSV
submission format.**

This document intentionally does **not** implement export or authority filing.
It only establishes:

- the persistent RoI register entry model,
- the authority reporting window model,
- and the tenant-scoped service layer for creating, updating, and reading live
  DORA register entries.

This is part 1 of a 3-document sequence:

- **Document 14** — Live register foundation (**this document**)
- **Document 15** — xBRL-CSV export + validation
- **Document 16** — authority submission workflow + reporting calendars

---

## 2. Scope — KER-106 (authoritative for this document)

This document is complete when all of the following are true:

- A tenant can create and maintain RoI entries as live records.
- RoI entries are persisted with tenant isolation and auditable timestamps.
- A tenant can retrieve all current RoI records and filter by criticality.
- Reporting windows for competent authorities can be stored and retrieved.
- The service layer is ready for later xBRL-CSV export, without implementing it yet.

Out of scope for this document:
- xBRL-CSV generation
- ESA quality-check validation
- authority submission APIs
- upload/download flows
- multi-authority filing orchestration

---

## 3. Domain model

### 3.1 DORARegisterEntry

Each row represents one live RoI record for a tenant's ICT third-party or ICT service relationship.

| Field | Type | Required | Notes |
|---|---|---|---|
| register_entry_id | UUID (v4) | Yes | Generated in Python |
| tenant_id | UUID (v4) | Yes | FK -> tenants |
| provider_name | str | Yes | ICT third-party provider name |
| service_name | str | Yes | Name of ICT service or service family |
| provider_type | str | Yes | e.g. cloud, software, managed_service, telecom |
| criticality_level | str | Yes | critical / high / standard |
| business_function | str | Yes | Business function supported by the service |
| data_types | list[str] | Yes | Types of data processed or supported |
| countries_supported | list[str] | Yes | ISO-like country codes or internal country strings |
| contract_start_date | date or None | No | Optional |
| contract_end_date | date or None | No | Optional |
| exit_strategy_summary | str or None | No | Plain summary of exit approach |
| is_active | bool | Yes | Active relationship flag |
| source_record_id | str or None | No | Optional upstream record ID from CMDB/ingest |
| created_at | datetime (UTC) | Yes | server default |
| updated_at | datetime (UTC) | Yes | server default + updated on change |

### 3.2 criticality_level allowed values

Define module-level constants in `src/models/dora_register_entry.py`:

- `CRITICALITY_CRITICAL = "critical"`
- `CRITICALITY_HIGH = "high"`
- `CRITICALITY_STANDARD = "standard"`

These are Kerno internal values for Sprint 2. They intentionally simplify the
broader DORA terminology into practical operating classes.

### 3.3 provider_type allowed values

Define module-level constants in `src/models/dora_register_entry.py`:

- `PROVIDER_TYPE_CLOUD = "cloud"`
- `PROVIDER_TYPE_SOFTWARE = "software"`
- `PROVIDER_TYPE_MANAGED_SERVICE = "managed_service"`
- `PROVIDER_TYPE_TELECOM = "telecom"`
- `PROVIDER_TYPE_OTHER = "other"`

### 3.4 DORAReportingWindow

This table stores the reporting window metadata for a competent authority.

| Field | Type | Required | Notes |
|---|---|---|---|
| reporting_window_id | UUID (v4) | Yes | Generated in Python |
| authority_code | str | Yes | e.g. bafin, dnb |
| authority_name | str | Yes | Human-readable name |
| member_state | str | Yes | e.g. Germany, Netherlands |
| reporting_year | int | Yes | e.g. 2027 |
| submission_open_date | date | Yes | Window opens |
| submission_close_date | date | Yes | Window closes |
| notes | str or None | No | Human-readable context |
| created_at | datetime (UTC) | Yes | server default |

Important: reporting windows are **global reference data**, not tenant-scoped.
Therefore, Document 14 service methods that query reporting windows do **not**
call `resolve_and_set_tenant_context()`.

---

## 4. Service behavior

### 4.1 `generate` is not used here

This document does not generate RoI data from evidence automatically. It creates
the live data layer only. Entries are created explicitly via the service API.

### 4.2 `dora_roi_service.py` responsibilities

Create `src/services/dora_roi_service.py` with these public methods:

- `create_register_entry(conn, tenant_id, entry_input: RegisterEntryInput) -> RegisterEntryOutput`
- `update_register_entry(conn, tenant_id, register_entry_id: str, entry_input: RegisterEntryInput) -> RegisterEntryOutput | None`
- `get_register_entry(conn, tenant_id, register_entry_id: str) -> RegisterEntryOutput | None`
- `list_register_entries(conn, tenant_id, criticality_level: str | None = None) -> list[RegisterEntryOutput]`
- `list_active_register_entries(conn, tenant_id) -> list[RegisterEntryOutput]`
- `list_reporting_windows(conn, reporting_year: int | None = None) -> list[ReportingWindowOutput]`

### 4.3 Dataclasses

Create these frozen dataclasses in the service module:

#### RegisterEntryInput
Fields:
- provider_name: str
- service_name: str
- provider_type: str
- criticality_level: str
- business_function: str
- data_types: list[str]
- countries_supported: list[str]
- contract_start_date: date | None
- contract_end_date: date | None
- exit_strategy_summary: str | None
- is_active: bool
- source_record_id: str | None

#### RegisterEntryOutput
Fields mirror the persisted record:
- register_entry_id
- tenant_id
- provider_name
- service_name
- provider_type
- criticality_level
- business_function
- data_types
- countries_supported
- contract_start_date
- contract_end_date
- exit_strategy_summary
- is_active
- source_record_id
- created_at
- updated_at

#### ReportingWindowOutput
Fields:
- reporting_window_id
- authority_code
- authority_name
- member_state
- reporting_year
- submission_open_date
- submission_close_date
- notes
- created_at

### 4.4 Validation rules

Implement validation with private helpers:

- `provider_name`, `service_name`, and `business_function` must be non-empty strings
- `provider_type` must be one of the allowed constants
- `criticality_level` must be one of the allowed constants
- `data_types` must be a non-empty list of non-empty strings
- `countries_supported` must be a non-empty list of non-empty strings
- If both contract dates are present, `contract_end_date` must not be before `contract_start_date`
- `exit_strategy_summary`, if present, must be trimmed and capped to `MAX_EXIT_SUMMARY_LENGTH`
- String normalization should trim leading/trailing whitespace
- Empty optional strings should be stored as `None`

Use `ValueError` for validation failures in this document.

### 4.5 Query rules

- All tenant-scoped methods must call `resolve_and_set_tenant_context(conn, tenant_id)` before DB access
- If `tenant_id` is falsey, raise `TenantContextMissingError` from `src.exceptions`
- All DB access must use `conn.execute(sql, params)` with `:name` parameters
- No Session API
- `list_reporting_windows()` is global reference data and therefore must **not** set tenant context

### 4.6 Ordering rules

- `list_register_entries()` sorts by `updated_at DESC, provider_name ASC`
- `list_active_register_entries()` returns only `is_active = true` and uses the same ordering
- `list_reporting_windows()` sorts by `reporting_year DESC, submission_open_date ASC, authority_code ASC`

---

## 5. Files in scope

### 5.1 `src/models/dora_register_entry.py` — NEW

SQLAlchemy ORM model for `dora_register_entries`.

Requirements:
- module docstring
- all fields in §3.1
- `data_types` stored as `ARRAY(Text)`
- `countries_supported` stored as `ARRAY(Text)`
- `created_at` server default `func.now()`
- `updated_at` server default `func.now()` and updated on change
- constants from §3.2 and §3.3 defined at module level

### 5.2 `src/models/dora_reporting_window.py` — NEW

SQLAlchemy ORM model for `dora_reporting_windows`.

Requirements:
- module docstring
- all fields in §3.4
- reporting windows are global reference data
- no tenant_id column

### 5.3 `src/services/dora_roi_service.py` — NEW

Implements all service methods from §4.

Requirements:
- module docstring
- all public and private functions have docstrings
- all functions under 40 lines
- use private helper(s) for:
  - tenant validation
  - input normalization
  - entry validation
  - row-to-dataclass conversion
- define the three dataclasses in this file

### 5.4 `migrations/versions/011_create_dora_roi_tables.py` — NEW

Creates both tables.

Requirements:
- revision chains after migration 010
- creates `dora_register_entries`
- creates `dora_reporting_windows`
- adds index on `(tenant_id, criticality_level, is_active)`
- adds index on `(tenant_id, updated_at)`
- enables RLS on `dora_register_entries`
- creates tenant isolation policy on `dora_register_entries`:
  `tenant_id = current_setting('app.current_tenant_id', true)::uuid`
- does **not** enable RLS on `dora_reporting_windows` because it is global reference data
- `downgrade()` drops both tables
- module docstring must explain What / Why / How

### 5.5 `config/constants.py` — extend only

Add if not already present:
- `MAX_EXIT_SUMMARY_LENGTH = 1000`

Do not remove or rename existing constants.

### 5.6 `tests/unit/services/test_dora_roi_service.py` — NEW

No live database.

Required tests:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_create_register_entry_success | valid input persists and returns output |
| 2 | test_update_register_entry_success | update returns changed fields |
| 3 | test_get_register_entry_missing_returns_none | missing row returns None |
| 4 | test_list_register_entries_orders_by_updated_at_desc | ordering rule enforced |
| 5 | test_list_register_entries_filters_by_centrality | criticality filter enforced |
| 6 | test_list_active_register_entries_only_returns_active | inactive excluded |
| 7 | test_invalid_provider_type_raises | bad provider_type rejected |
| 8 | test_invalid_criticality_level_raises | bad criticality rejected |
| 9 | test_empty_data_types_raises | empty list rejected |
| 10 | test_empty_countries_supported_raises | empty list rejected |
| 11 | test_contract_end_before_start_raises | date validation works |
| 12 | test_none_tenant_raises | falsey tenant_id -> TenantContextMissingError |
| 13 | test_tenant_context_set_before_query | first SQL call is tenant context |
| 14 | test_list_reporting_windows_does_not_set_tenant_context | global query bypasses tenant context |
| 15 | test_exit_strategy_trimmed_and_capped | normalization + max length applied |

Note: Test name #5 intentionally says "centrality" in the original planning notes,
but the actual field is `criticality_level`. Use the test name exactly as listed
above, while asserting the criticality filter behavior.

### 5.7 `tests/unit/models/test_dora_register_entry.py` — NEW

Required tests:
- criticality constants are defined correctly
- provider type constants are defined correctly
- table name is correct
- tenant_id column exists
- array-backed fields exist for `data_types` and `countries_supported`

---

## 6. Gate checks (apply to every file produced)

| Check | Rule |
|---|---|
| Module docstring present | Answers What, Why, and How to run or test |
| All functions have docstrings | No exceptions |
| No spec notation in variable names | No Greek letters, subscripts, raw spec symbols |
| No magic numbers | Numeric literals named in constants.py where appropriate |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule followed | resolve_and_set_tenant_context before tenant DB access |
| TenantContextMissingError from src.exceptions | Not from any other module |

---

## 7. Out of scope for Document 14

- Automatic extraction of RoI entries from CMDB or Jira
- xBRL-CSV file creation
- 116 ESA validation rules
- authority submission workflow
- multi-authority filing orchestration
- DORA incident workflows
- CRA reporting

---

## 8. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md | Highest |
| This file | Authoritative for Document 14 scope |
| KERNO_STRATEGY.md | Context only |
| Document 13 recommendation architecture | Prior sprint pattern |
| DORA Regulation 2022/2554 | Business context only, not implementation schema authority |
