# PROMPT_doc16a_rls_scheduler_guard.md
# Document 16A — Scheduler RLS Guard Patch
# Spec version: 1.0 | Status: Authoritative
# Type: Narrow follow-up patch to Doc 16
# Purpose: Make cross-tenant scheduler assumptions explicit and fail loudly

---

## 1. Purpose

This patch tightens the scheduler behavior introduced in Document 16.

Document 16 correctly noted that `find_tenants_missing_submission()` depends on
cross-tenant visibility to enumerate tenants from RLS-protected tables.
However, the original design leaves open a dangerous failure mode:

- if the function is called with a regular tenant-scoped connection,
- RLS may hide all rows,
- and the function may return an empty result that *looks valid*.

For a compliance scheduling workflow, silent false negatives are worse than loud failures.

This patch makes that behavior explicit and defensive.

---

## 2. Scope

This patch is complete when all of the following are true:

- The scheduler module explicitly treats cross-tenant enumeration as a privileged/admin path.
- The scheduler checks for evidence that cross-tenant visibility is actually available.
- If cross-tenant visibility is not available, the function raises a dedicated runtime error rather than returning misleading empty results.
- Tests cover both:
  - successful privileged execution path
  - fail-loud restricted path

Out of scope:
- changing RLS policies
- changing DB roles
- creating new admin connection factories
- modifying submission services
- modifying models or migrations

---

## 3. Design decision

### 3.1 New runtime error

In `src/scheduler/dora_roi_submission_scheduler.py`, define a **module-local**
runtime error:

- `SchedulerAdminConnectionRequiredError(RuntimeError)`

Reason:
- This patch must not change `src/exceptions.py` because that file is locked.
- The error is scheduler-specific and operational, not a general domain exception.

### 3.2 Detection rule

`find_tenants_missing_submission()` must fail loudly if it cannot establish that
cross-tenant visibility exists.

Because this codebase does not provide role-inspection helpers and this patch
cannot change infrastructure, use the following deterministic guard:

1. Query open submission windows for `today`.
2. If there are no open windows, return `[]` normally. This is not an error.
3. If there are open windows, perform tenant enumeration using the existing union approach.
4. If:
   - there is at least one open window, and
   - tenant enumeration returns zero tenants,
   then raise `SchedulerAdminConnectionRequiredError` with a clear message explaining:
   - the function requires an admin/privileged connection for cross-tenant visibility,
   - a tenant-scoped RLS connection may hide all rows,
   - and an empty result would be unsafe to interpret as "all tenants compliant".

This is an intentionally conservative heuristic.
It may occasionally force a loud failure in a genuinely empty environment, and that is acceptable for this patch.

### 3.3 Docstring requirement

The module docstring and the public function docstring must explicitly state:

- this is a privileged operational query,
- it must not be called with a standard tenant-scoped connection,
- and it raises `SchedulerAdminConnectionRequiredError` when cross-tenant visibility cannot be established.

---

## 4. Files in scope

### 4.1 `src/scheduler/dora_roi_submission_scheduler.py`

Required changes:

- add module-local `SchedulerAdminConnectionRequiredError`
- add helper function(s) if needed, under 40 lines each
- preserve existing public function name:
  - `find_tenants_missing_submission(conn, today: date | None = None)`
- implement the fail-loud guard from §3.2
- keep module pure (no side effects, no notifications)
- no tenant context setting
- full docstrings for all functions and the new exception class

### 4.2 `tests/unit/scheduler/test_dora_roi_submission_scheduler.py`

Update/add tests so the file covers at least:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_find_tenants_missing_submission_returns_expected_pairs | privileged path returns missing tenant/window pairs |
| 2 | test_find_tenants_missing_submission_ignores_closed_windows | no open windows -> [] without error |
| 3 | test_find_tenants_missing_submission_respects_today_parameter | custom today changes window status deterministically |
| 4 | test_find_tenants_missing_submission_raises_when_open_window_but_no_visible_tenants | fail-loud guard triggers |
| 5 | test_scheduler_error_message_mentions_admin_connection | error explains privileged connection requirement |

No live DB; keep spy/fake connection style consistent with prior docs.

---

## 5. Gate checks

Apply to every file changed:

| Check | Rule |
|---|---|
| Module docstring present | Answers What, Why, and How to run or test |
| All functions have docstrings | No exceptions |
| No spec notation in variable names | Plain English names only |
| No magic numbers | Name values if domain-significant |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule followed | Do not weaken RLS; no tenant-context misuse |
| TenantContextMissingError from src.exceptions | N/A for this patch unless referenced indirectly |

---

## 6. Out of scope

- New DB permissions
- New app roles
- New environment variables
- New scheduler framework integration
- Notification sending
- Changes to services or migrations

---

## 7. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md | Highest |
| This patch spec | Authoritative for this patch |
| PROMPT_doc16_dora_roi_submission_workflow.md | Upstream behavior contract |
| KERNO_STRATEGY.md | Context only |
