# PROMPT_doc16_dora_roi_submission_workflow.md
# Document 16 — DORA RoI Submission Workflow + Reporting Calendars
# Spec version: 1.0 | Status: Authoritative
# Covers: KER-106 (part 3 of 3)
# Builds on: Doc 14 (live register), Doc 15 (export + validation)
# Supersedes: any prior description of DORA RoI submission workflow in prompts

---

## 1. Purpose

This document specifies Document 16 of the Kerno compliance copilot codebase.

Document 16 is the third part of the DORA Register of Information sequence. It builds on:

- **Document 14**: live tenant-scoped RoI register and reporting windows
- **Document 15**: deterministic export rows and rule-based validation

and adds:

- a persistent model for competent‑authority submission windows,
- a persistent model for per-tenant submission runs,
- a service that wraps export + validation into a submission-ready package with status tracking,
- a simple scheduler hook for reporting windows and submission reminders.

The design principle is:

**Document 14 keeps the live data. Document 15 prepares a validated package. Document 16 captures when and how that package is actually filed.**

This document **does not** implement real regulator APIs or portal uploads. It models and tracks the submission workflow inside Kerno, so that a later integration can plug into that workflow.

---

## 2. Scope — KER-106 (authoritative for this document)

This document is complete when all of the following are true:

- The system can represent **submission windows** for RoI at the competent‑authority level.
- For each tenant and reporting year, the system can track **submission runs** from draft to submitted.
- A tenant can trigger a submission run that:
  - calls Doc 15's `build_export_package`,
  - stores a submission run record with status and timestamps,
  - captures the validation outcome,
  - and exposes a stable identifier that a future authority integration can use.
- Submission status is auditable and deterministic for a given tenant, reporting year, and window.
- A basic scheduler hook can:
  - list upcoming submission windows,
  - and identify tenants that have not yet completed a submission run for the relevant window.

Out of scope for this document:

- Actual upload to regulator portals (LH Portal, Central Bank Portal, etc.)
- xBRL-CSV zip creation or taxonomy packaging
- Multi‑jurisdiction routing logic
- Notification delivery (email, Slack, etc.)
- Full workflow UI; this is a backend workflow and calendar model.

---

## 3. Submission and calendar model

### 3.1 Submission windows

Create a **global** model (no tenant_id; no RLS) representing competent‑authority submission windows.

#### DORASubmissionWindow

New SQLAlchemy model in `src/models/dora_submission_window.py`:

Fields (columns):

- `id`: UUID primary key (server-generated)
- `authority_code`: string, required (e.g. `"MFSA"`, `"CBI"`)  
- `reporting_year`: integer, required
- `register_reference_date`: date, required  
  Typically 31 December of the year preceding the reporting year.
- `window_open_date`: date, required  
- `window_close_date`: date, required  
- `created_at`: UTC datetime, default now
- `updated_at`: UTC datetime, auto-updated

Rules:

- No `tenant_id` column; this is global reference data, like Doc 14's reporting windows.
- `authority_code`, `reporting_year`, and `register_reference_date` must be unique together (composite unique index).
- `window_open_date <= window_close_date`.
- Dates are stored as naive UTC dates (no time zone component).

This model represents **when** a RoI for a given authority and reporting year is expected to be filed, independently of any specific tenant.

### 3.2 Submission runs

Create a **tenant-scoped** model representing individual submission runs.

#### DORASubmissionRun

New SQLAlchemy model in `src/models/dora_submission_run.py`:

Fields (columns):

- `id`: UUID primary key (server-generated)
- `tenant_id`: UUID, required, with RLS
- `submission_window_id`: UUID, required, FK to `DORASubmissionWindow.id`
- `reporting_year`: integer, required
- `status`: string, required, one of:
  - `"draft"`
  - `"ready"`
  - `"submitted"`
  - `"failed"`
- `validation_overall_status`: string, required, one of:
  - `"pass"`
  - `"warn"`
  - `"fail"`
- `validation_issue_count`: integer, required
- `entry_count`: integer, required
- `created_at`: UTC datetime, default now
- `updated_at`: UTC datetime, auto-updated
- `submitted_at`: UTC datetime, nullable (set when status becomes `"submitted"`)
- `submission_reference`: string, nullable  
  A stable identifier that a future integration can store the authority's reference ID into.

Rules:

- This table is tenant-scoped and MUST have RLS applied like `dora_register_entries`.
- `tenant_id`, `submission_window_id`, and `reporting_year` together identify the logical "slot" for a given tenant's submission in a given window.
- Multiple runs for the same slot are allowed (retries); status and timestamps make the history auditable.
- All timestamps are in UTC, stored as naive datetimes at the DB level.

### 3.3 Status semantics

Status values have the following meaning:

- `"draft"`: A submission run has been created, but validation may fail or have warnings.
- `"ready"`: Validation `overall_status` is `"pass"`; the package is ready for filing.
- `"submitted"`: A submission run has been exported and (from Kerno's perspective) sent to an authority integration. This document does not implement the actual send.
- `"failed"`: An internal error occurred while building the package (e.g., unexpected exception). Validation failures alone do **not** set `"failed"`; they keep `"draft"`.

The `validation_overall_status` is copied from Doc 15's `ValidationSummary.overall_status` for the export package.

### 3.4 Calendar logic

A submission window is considered:

- **"upcoming"** when `window_open_date` is in the future.
- **"open"** when `window_open_date <= today <= window_close_date`.
- **"closed"** when `today > window_close_date`.

For this document, "today" is always derived from `datetime.date.today()` in UTC; no time zones and no partial days.

---

## 4. Service behavior

### 4.1 `dora_roi_submission_service.py`

Create a new service module in `src/services/dora_roi_submission_service.py`.

Public functions:

1. `create_submission_run(conn, tenant_id: str, submission_window_id: str) -> DORASubmissionRun`
2. `build_and_record_submission(conn, tenant_id: str, submission_window_id: str) -> tuple[DORASubmissionRun, DORAExportPackage]`
3. `list_open_windows(conn) -> list[DORASubmissionWindow]`
4. `list_tenant_submission_runs(conn, tenant_id: str) -> list[DORASubmissionRun]`

Requirements:

- All tenant‑scoped behaviors must:
  - validate `tenant_id` (falsey -> `TenantContextMissingError`),
  - call `set_tenant_context(conn, tenant_id)` before any tenant‑scoped query.
- `list_open_windows` is **global** reference data and does **not** set tenant context.
- All DB access uses `conn.execute(sql, params_dict)` with named parameters (no ORM sessions, no `.add`, no `.flush`, no `.commit`).

Behavior details:

#### 4.1.1 `create_submission_run`

- Validates `tenant_id`; raises `TenantContextMissingError` if falsey.
- Verifies that `submission_window_id` exists in `DORASubmissionWindow`.
- Derives the `reporting_year` from the submission window.
- Inserts a new `DORASubmissionRun` row with:
  - status `"draft"`,
  - `validation_overall_status = "fail"` (pessimistic default),
  - `validation_issue_count = 0`,
  - `entry_count = 0`,
  - `submitted_at = None`,
  - `submission_reference = None`.
- Returns the created row (as model instance or lightweight struct, consistent with existing patterns).

#### 4.1.2 `build_and_record_submission`

- Validates `tenant_id`; raises `TenantContextMissingError` if falsey.
- Looks up the submission window and reporting year.
- Calls Doc 15's `build_export_package(conn, tenant_id, reporting_year)`:
  - This enforces tenant isolation and builds a `DORAExportPackage` with validation summary.
- Creates or updates a `DORASubmissionRun` row for that `(tenant_id, submission_window_id, reporting_year)` slot:
  - If there is no existing row, create one as in `create_submission_run`.
  - If there is an existing row, update it in place.
- Sets:
  - `status`:
    - `"ready"` if `ValidationSummary.overall_status == "pass"`;
    - `"draft"` if `"warn"`;
    - `"draft"` if `"fail"`;
  - `validation_overall_status` from `ValidationSummary.overall_status`;
  - `validation_issue_count` from `ValidationSummary.issue_count`;
  - `entry_count` from `DORAExportPackage.entry_count`;
  - `submitted_at` is **not** set in this function (submission happens later).
- Returns `(submission_run, export_package)`.

This function does **not** send anything to an authority; it prepares and records the package and status so a later integration can pick it up.

#### 4.1.3 `list_open_windows`

- Queries `DORASubmissionWindow` where `window_open_date <= today <= window_close_date`.
- Returns them sorted by `window_open_date ASC`, then `authority_code ASC`.

#### 4.1.4 `list_tenant_submission_runs`

- Validates `tenant_id`; raises `TenantContextMissingError` if falsey.
- Returns all submission runs for that tenant, ordered by:
  - `reporting_year DESC`, then
  - `created_at DESC`.

### 4.2 Scheduler hook

Create a minimal scheduler module in `src/scheduler/dora_roi_submission_scheduler.py`.

Public function:

- `find_tenants_missing_submission(conn, today: date | None = None) -> list[tuple[str, DORASubmissionWindow]]`

Behavior:

- Uses `today` if provided; otherwise `datetime.date.today()`.
- Finds all submission windows that are currently `"open"` on that date.
- For each open window, checks which tenants have **no** `DORASubmissionRun` in `"ready"` or `"submitted"` state for that `(tenant_id, submission_window_id, reporting_year)` slot.
- Returns a list of `(tenant_id, submission_window)` tuples for tenants that are still missing a ready/submitted run for that window.

Implementation constraints:

- Because this project does not have a global tenant registry model, this function may assume:
  - There is a way to enumerate tenants that have at least one `dora_register_entries` row or one `DORASubmissionRun` row.
  - You should document any such assumption explicitly in the docstring and tests.
- This module does **not** send notifications or schedule jobs; it just performs the query and returns data that a higher-level scheduler can act on.

---

## 5. Files in scope

### 5.1 `src/models/dora_submission_window.py` — NEW

Requirements:

- module docstring
- SQLAlchemy model `DORASubmissionWindow` as per §3.1
- global reference data (no `tenant_id`, no RLS)
- composite unique index on `(authority_code, reporting_year, register_reference_date)`
- all functions under 40 lines

### 5.2 `src/models/dora_submission_run.py` — NEW

Requirements:

- module docstring
- SQLAlchemy model `DORASubmissionRun` as per §3.2
- tenant-scoped model with:
  - `tenant_id` column
  - row-level security (RLS) applied via migration in §5.3
- all fields in §3.2 present
- all functions under 40 lines

### 5.3 `migrations/versions/012_create_dora_submission_tables.py` — NEW

Requirements:

- module docstring answering What / Why / How to run
- `upgrade` and `downgrade` functions, plus private helpers
- creates `dora_submission_windows` and `dora_submission_runs` tables
- `dora_submission_windows`:
  - global table; no RLS
  - composite unique index
- `dora_submission_runs`:
  - tenant-scoped table with:
    - `tenant_id` (UUID)
    - RLS policy and direct tenant_id predicate
- no magic numbers; string constants and lengths via `config/constants.py` where applicable
- all functions under 40 lines

### 5.4 `src/services/dora_roi_submission_service.py` — NEW

Covered in §4.1.

Additional requirements:

- module docstring
- use `DORAExportPackage` and `build_export_package` from Doc 15
- import `TenantContextMissingError` from `src.exceptions`
- import and use `set_tenant_context` from `src/db/rls.py`
- no ORM Session; only `conn.execute(sql, params_dict)`
- all functions under 40 lines
- no spec notation in variable names

### 5.5 `src/scheduler/dora_roi_submission_scheduler.py` — NEW

Covered in §4.2.

Additional requirements:

- module docstring
- pure query / computation; no side effects
- no tenant context setting (this is global analysis), but obeys RLS by using regular tables
- functions under 40 lines

### 5.6 `tests/unit/models/test_dora_submission_window.py` — NEW

Required tests:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_window_model_has_expected_columns | Model contains all fields from §3.1 |
| 2 | test_window_unique_constraint | Duplicate authority/year/reference_date fails |
| 3 | test_window_date_ordering_rule | window_open_date <= window_close_date enforced (at least via helper/validation) |

### 5.7 `tests/unit/models/test_dora_submission_run.py` — NEW

Required tests:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_run_model_has_expected_columns | Model contains all fields from §3.2 |
| 2 | test_run_defaults_are_set_safely | Defaults for status and timestamps are correct |
| 3 | test_run_is_tenant_scoped | tenant_id is present and used in queries (at least via helper) |

### 5.8 `tests/unit/services/test_dora_roi_submission_service.py` — NEW

No live database; use a spy connection as in Doc 14/15 tests.

Required tests:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_create_submission_run_inserts_draft | draft row created with pessimistic defaults |
| 2 | test_build_and_record_submission_creates_run_when_missing | run created if none exists |
| 3 | test_build_and_record_submission_updates_existing_run | existing run updated |
| 4 | test_build_and_record_submission_copies_validation_summary | status and counts copied correctly |
| 5 | test_build_and_record_submission_does_not_set_submitted_at | submitted_at remains None |
| 6 | test_list_open_windows_filters_by_today | only open windows returned |
| 7 | test_list_tenant_submission_runs_sorted | tenant runs ordered correctly |
| 8 | test_falsey_tenant_raises | guard enforced |
| 9 | test_tenant_context_set_before_tenant_queries | set_tenant_context called before tenant queries |
| 10 | test_no_session_api_used | conn.add / conn.flush never called |

### 5.9 `tests/unit/scheduler/test_dora_roi_submission_scheduler.py` — NEW

Required tests:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_find_tenants_missing_submission_returns_expected_pairs | tenants without ready/submitted runs are returned |
| 2 | test_find_tenants_missing_submission_ignores_closed_windows | closed windows ignored |
| 3 | test_find_tenants_missing_submission_respects_today_parameter | custom today changes behavior deterministically |

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

## 7. Out of scope for Document 16

- Real portal uploads (LH Portal, Central Bank Portal, etc.)
- Authentication or authorization for authority APIs
- Multi‑regulator routing logic beyond authority codes on windows
- Background job orchestration and monitoring
- Notification delivery to humans (email, Slack, etc.)
- Persistence of actual xBRL or CSV files

---

## 8. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md | Highest |
| This file | Authoritative for Document 16 scope |
| PROMPT_doc15_dora_roi_export_validation.md | Upstream export + validation contract |
| PROMPT_doc14_dora_roi_live_register.md | Upstream live register contract |
| KERNO_STRATEGY.md | Context only |
| DORA RoI supervisory guidance (e.g. MFSA, CBI, etc.) | External regulatory context only |
