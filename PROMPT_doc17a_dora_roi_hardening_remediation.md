# PROMPT_doc17a_dora_roi_hardening_remediation.md
# Document 17A — DORA RoI Hardening Remediation
# Spec version: 1.0 | Status: Authoritative
# Type: Targeted remediation of findings from Document 17 review
# Implements: Top 4 items from the §3.2.6 prioritized remediation plan

---

## 1. Purpose

This document directs the implementation of the four highest-priority findings
from the Document 17 hardening review (PROMPT_doc17_dora_roi_hardening_review.md).

All four items are defects or security gaps in the existing DORA RoI subsystem
(Docs 14–16A). No new features are being added. Changes must be tightly scoped
to the files listed under each item.

---

## 2. Locked files

Do not alter any file not explicitly listed in scope below.

Locked files include but are not limited to:

- All model files (`src/models/dora_*.py`)
- All migration files (`migrations/versions/011_*.py`, `012_*.py`)
- All scheduler files (`src/scheduler/dora_roi_submission_scheduler.py`)
- All export and submission service files not listed in scope
- `tests/conftest.py`
- `tests/security/test_tenant_isolation.py` existing tests — you MAY ADD to it,
  but you must NOT remove or modify any existing test or class definition
- `src/exceptions.py`

---

## 3. Shared implementation constraints

These rules apply to every file changed in this document:

- No magic strings for severity or status values in service code. Use constants
  from `config.constants` (e.g. `VALIDATION_SEVERITY_FAIL`).
- `TenantContextMissingError` must be imported from `src.exceptions`.
- All new issue codes must use the `ROI_XXX` format already established.
- All new constants must come from `config.constants`; add them there if missing.
- Every function must have a docstring. No function may exceed 40 lines.
- No spec notation in variable names.
- All gate checks from CLAUDE.md apply.

---

## 4. Item 1 — Zero-entry export produces "fail", not "pass"

### 4.1 Problem

`validate_export_rows([])` returns `overall_status="pass"` because the for-loop
produces zero issues and `_build_summary([])` defaults to pass. A zero-entry
export then propagates through `build_and_record_submission` to receive
submission status "ready", which is incorrect: a DORA RoI with no active
third-party entries is always a compliance defect, not a valid filing.

### 4.2 Fix — `src/services/dora_roi_validation_service.py`

In `validate_export_rows`, add a guard at the top of the function body (before
the for-loop):

If `rows` is empty, immediately return a `ValidationSummary` with:
- `overall_status = VALIDATION_SEVERITY_FAIL`
- `issue_count = 1`, `fail_count = 1`, `warn_count = 0`, `pass_count = 0`
- `issues = [ValidationIssue(issue_code="ROI_000", severity=VALIDATION_SEVERITY_FAIL, message=<see below>, register_entry_id=None)]`

Message:
```
"Export contains no active register entries; at least one entry is required for a valid DORA RoI filing."
```

Implement the guard logic in a private helper `_empty_export_summary()` to keep
`validate_export_rows` under 40 lines and the logic named.

Update the `validate_export_rows` docstring to mention the empty-list guard.

### 4.3 Tests — `tests/unit/services/test_dora_roi_validation_service.py`

Add exactly two new tests:

| Test name | Assertion |
|---|---|
| `test_validate_export_rows_fail_on_empty_list` | `validate_export_rows([])` returns `overall_status == "fail"` and `fail_count == 1` and `issue_count == 1` |
| `test_validate_export_rows_empty_list_issue_code_is_roi_000` | The single issue has `issue_code == "ROI_000"` and `register_entry_id is None` |

Update the module docstring count from "Ten tests" to "Twelve tests".

---

## 5. Item 2 — DORA cross-tenant security tests

### 5.1 Problem

`tests/security/test_tenant_isolation.py` does not cover DORA-specific paths.
The retrieval service is tested for tenant isolation but the DORA RoI, export,
and submission services are not. A regression that removes the tenant guard from
any DORA service would not be caught by the current security test suite.

### 5.2 Fix — `tests/security/test_tenant_isolation.py`

Add exactly five new tests. All are pure unit tests (no live database). Add
them in a clearly labelled new section at the end of the file.

Required imports (add to the file top, additive only):
- `from src.services.dora_roi_export_service import build_export_package`
- `from src.services.dora_roi_service import RegisterEntryInput, create_register_entry`
- `from src.services.dora_roi_submission_service import list_tenant_submission_runs`

Add a `_DoraSpyConn` helper class that records `(sql, params)` tuples and
returns the existing `_NullResult` from each `execute()` call. Use it for tests
that need to inspect SQL parameters.

Add a `_make_dora_entry_input()` module-level helper that returns a minimal
valid `RegisterEntryInput`.

| # | Test name | Assertion |
|---|---|---|
| 1 | `test_dora_roi_create_with_none_tenant_raises_before_sql` | `create_register_entry(conn, None, input)` raises `TenantContextMissingError` and `conn.statements == []` |
| 2 | `test_dora_roi_create_tenant_id_in_sql_params` | `create_register_entry(conn, TENANT_A_ID, input)` issues a SET LOCAL whose params contain `str(TENANT_A_ID)` |
| 3 | `test_dora_export_with_none_tenant_raises_before_sql` | `build_export_package(conn, None, 2025)` raises `TenantContextMissingError` and `conn.statements == []` |
| 4 | `test_dora_export_tenant_id_in_sql_params` | `build_export_package(conn, TENANT_A_ID, 2025)` issues a SET LOCAL whose params contain `str(TENANT_A_ID)` |
| 5 | `test_dora_submission_list_runs_tenant_id_in_sql_params` | `list_tenant_submission_runs(conn, TENANT_A_ID)` issues a SELECT on `dora_submission_runs` with `tenant_id` in the params |

Update the module docstring to note that DORA-specific unit tests were added.

---

## 6. Item 3 — Explicit tenant_id filter in list_tenant_submission_runs

### 6.1 Problem

`_SELECT_TENANT_RUNS` in `dora_roi_submission_service.py` has no WHERE clause.
It relies entirely on PostgreSQL RLS to restrict results to the current tenant.
If the function is ever called on a privileged (RLS-bypassing) connection —
for example, accidentally during a scheduler run — it returns all tenants'
runs with no error. The fix is defense-in-depth: add an explicit
`WHERE tenant_id = :tenant_id` so the query cannot return cross-tenant data
regardless of connection type.

### 6.2 Fix — `src/services/dora_roi_submission_service.py`

1. Modify the `_SELECT_TENANT_RUNS` SQL constant to add:
   ```sql
   WHERE tenant_id = :tenant_id
   ```
   immediately before the existing `ORDER BY` clause.

2. Update `list_tenant_submission_runs` to pass the tenant_id explicitly:
   ```python
   rows = conn.execute(_SELECT_TENANT_RUNS, {"tenant_id": str(tenant_id)}).fetchall()
   ```
   (Previously passed `{}`.)

No other changes in this file.

### 6.3 Tests — `tests/unit/services/test_dora_roi_submission_service.py`

Add one new test:

| Test name | Assertion |
|---|---|
| `test_list_tenant_submission_runs_passes_tenant_id_in_params` | The SQL call to `dora_submission_runs` has `tenant_id` as a key in its params dict |

Update the module docstring count from "Ten tests" to "Eleven tests".

---

## 7. Item 4 — Tenant guard must be first in create/update register entry

### 7.1 Problem

In `dora_roi_service.py`, both `create_register_entry` and `update_register_entry`
call `_normalize_and_validate(entry_input)` before `set_tenant_context(conn, tenant_id)`.
A caller that passes `tenant_id=None` with invalid input receives a `ValueError`
(from input validation) rather than `TenantContextMissingError` (from the tenant
guard). This order is:
- inconsistent with the export and submission services, which both call their
  `_guard_tenant` helper first;
- a minor information-disclosure path (reveals which fields are valid without auth);
- contrary to the intent of CLAUDE.md §3.1.

### 7.2 Fix — `src/services/dora_roi_service.py`

1. Add a private helper `_guard_tenant(tenant_id) -> None` that raises
   `TenantContextMissingError` if `tenant_id` is falsey. Pattern it after the
   equivalent helpers in the export and submission services.

2. Call `_guard_tenant(tenant_id)` as the **first line of the function body**
   in both `create_register_entry` and `update_register_entry` — before
   `_normalize_and_validate`.

3. Update the docstrings of both functions to state that
   `TenantContextMissingError` is raised before `ValueError`.

### 7.3 Tests — `tests/unit/services/test_dora_roi_service.py`

Update `test_none_tenant_raises`:

Change the final assertion from checking that no INSERT or SELECT was issued to
asserting that `spy.calls` is entirely empty:

```python
assert spy.calls == [], "No SQL must be issued when tenant_id is None"
```

This reflects that `_guard_tenant` now fires before `_normalize_and_validate`
and before any `set_tenant_context` attempt.

---

## 8. Gate checks

Apply to every file changed:

| Check | Rule |
|---|---|
| Module docstring present | Answers What, Why, and How to run or test |
| All functions have docstrings | No exceptions |
| No spec notation in variable names | Plain English names only |
| No magic numbers or magic strings | Use named constants |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule followed | No weakening of existing guards |
| TenantContextMissingError from src.exceptions | Always import from there |

---

## 9. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md | Highest |
| This document (Doc 17A) | Authoritative for this remediation |
| PROMPT_doc17_dora_roi_hardening_review.md | Source of findings |
| PROMPT_doc16_dora_roi_submission_workflow.md | Upstream behavior contract |
| KERNO_STRATEGY.md | Context only |
