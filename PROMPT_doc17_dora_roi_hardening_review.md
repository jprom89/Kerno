You are performing a **hardening review** of the DORA Register of Information
implementation (Docs 14–16A) in the Kerno compliance copilot codebase.

This is *not* a feature sprint. It is a structured verification pass.

Read the following files in full before writing any review text or code:
- PROMPT_doc17_dora_roi_hardening_review.md          (just created above — authoritative for this review)
- PROMPT_doc16a_rls_scheduler_guard.md               (Doc 16A patch — scheduler guard)
- PROMPT_doc16_dora_roi_submission_workflow.md       (Doc 16 — submission workflow + calendars)
- PROMPT_doc15_dora_roi_export_validation.md         (Doc 15 — export + validation)
- PROMPT_doc14_dora_roi_live_register.md             (Doc 14 — live register)
- CLAUDE.md                                          (process and gate rules)
- KERNO_STRATEGY.md                                  (context only)

Then read all **implemented code and tests** produced by Docs 14, 15, 16, and 16A:

- config/constants.py (extensions from Doc 14 & 15)
- src/models/dora_register_entry.py
- src/models/dora_reporting_window.py
- src/models/dora_submission_window.py
- src/models/dora_submission_run.py
- src/services/dora_roi_service.py
- src/services/dora_roi_export_service.py
- src/services/dora_roi_validation_service.py
- src/services/dora_roi_submission_service.py
- src/scheduler/dora_roi_submission_scheduler.py
- migrations/versions/011_create_dora_roi_tables.py
- migrations/versions/012_create_dora_submission_tables.py
- tests/unit/models/test_dora_register_entry.py
- tests/unit/models/test_dora_submission_window.py
- tests/unit/models/test_dora_submission_run.py
- tests/unit/services/test_dora_roi_service.py
- tests/unit/services/test_dora_roi_export_service.py
- tests/unit/services/test_dora_roi_validation_service.py
- tests/unit/services/test_dora_roi_submission_service.py
- tests/unit/scheduler/test_dora_roi_submission_scheduler.py
- tests/security/test_tenant_isolation.py
- tests/conftest.py (only as needed for fixture understanding)

Do NOT modify any of these files until §4.

---

## 1. Purpose

This document defines **Document 17 — DORA RoI Hardening Review**.

The goal is to:
- treat the DORA RoI implementation (Docs 14–16A) as one cohesive subsystem,
- verify its correctness, safety, and maintainability,
- and produce a prioritized remediation plan before further DORA work.

You will:
- perform a written, adversarial review of the existing code and tests,
- capture findings and recommendations in a structured report,
- and only then, optionally, apply targeted patches in a separate step.

---

## 2. Scope

In-scope:

- live register: models, service, migrations, tests (Doc 14)
- export + validation: services, dataclasses, constants, tests (Doc 15)
- submission workflow + calendars: models, service, scheduler, migrations, tests (Doc 16)
- RLS scheduler guard patch (Doc 16A)
- tenant isolation behavior for all DORA-related paths
- test coverage and behavioral scenarios for DORA RoI end-to-end

Out of scope:

- new DORA features or documents
- UI or API surfaces that are not already implemented
- real regulator integrations (portals, APIs)
- non-DORA parts of the codebase (unless directly impacted by DORA changes)

---

## 3. Required workflow

Follow these steps in order. Do NOT skip steps.

### 3.1 Orientation

1. Summarize, in your own words, the end-to-end DORA RoI flow implemented by Docs 14–16A:
   - how data enters the live register,
   - how it is exported and validated,
   - how submission runs and calendars work,
   - how tenant isolation is enforced.

2. List the key invariants that should always hold for a correct, safe system, for example:
   - tenant A can never see or influence tenant B's RoI data,
   - inactive entries are never exported,
   - failed validation never results in a "ready" submission status,
   - scheduler never silently reports "everyone compliant" due to RLS.

### 3.2 Written hardening review (MANDATORY, NO CODE CHANGES)

Produce a written review report with the following structure.

#### 3.2.1 System overview

- 5–10 bullets describing:
  - main components,
  - how they interact,
  - and where trust boundaries and tenant boundaries lie.

#### 3.2.2 Tenant isolation & RLS

- Analyze how tenant isolation is enforced for:
  - RoI service,
  - export service,
  - submission service,
  - any other DORA service touching tenant data.

For each one:

- Describe:
  - where `set_tenant_context` is called,
  - which queries depend on it,
  - and any exceptions (global tables like reporting windows, submission windows).
- Identify any:
  - direct SQL that could leak cross-tenant data if mis-scoped,
  - places where tenant context is assumed but not enforced,
  - reliance on RLS bypass or admin connections (like the scheduler).

Explicitly comment on whether each path appears **safe**, **fragile**, or **unsafe**, and why.

#### 3.2.3 Data and workflow correctness

Review the data model and workflow logic across:

- `dora_register_entries`
- `dora_reporting_windows`
- `dora_submission_windows`
- `dora_submission_runs`
- the services that operate on them.

For each of the following questions, answer with:
- a short yes/no or "partially",
- plus a brief technical justification (pointing to specific files/behaviors).

Questions:

1. Do models and migrations align (same columns, types, indexes)?
2. Are all required DORA RoI fields represented in the live model per Doc 14?
3. Are export rows deterministic in content and ordering given a fixed DB state?
4. Are validation rules in Doc 15 implemented faithfully and deterministically?
5. Does `build_and_record_submission` always set submission status and counts consistently with the ValidationSummary?
6. Can `submitted_at` ever be set incorrectly (e.g., without a real submission, or never set even after "submitted")?
7. Are submission windows interpreted consistently in terms of dates and "open/closed" logic?
8. Are there any code paths that can lead to an inconsistent combination, e.g.:
   - status `"ready"` with `overall_status == "fail"`,
   - `"submitted"` with `entry_count == 0`,
   - or "no runs" in a tenant that clearly has entries?

#### 3.2.4 Test coverage & scenario analysis

- Summarize what the existing tests cover *by behavior*, not just by file, e.g.:
  - "successful create/update/list for register entries",
  - "validation warnings for over-long fields".

Then identify gaps by scenario, for example:

- cross-tenant access attempts,
- extreme but valid inputs (large arrays, many entries),
- interactions between reporting windows and submission windows,
- replay or retry semantics for submissions.

Classify each gap as:
- **Critical** (must be filled before any production use),
- **Important** (should be filled before external demos),
- **Nice-to-have** (can wait).

#### 3.2.5 Security & failure modes

Identify any security-relevant or failure-mode issues. Examples:

- functions that fail open instead of fail closed,
- error handling that swallows exceptions needed for audit,
- places where RLS or tenant_id filtering is bypassed or assumed,
- reliance on default values that could mask bugs.

For each issue:

- describe the problem precisely,
- explain the potential impact,
- suggest a concrete mitigation (code, tests, or documentation).

#### 3.2.6 Prioritized remediation plan

Finally, produce a **prioritized list** of remediation items.

For each item:

- provide:
  - a short title,
  - severity (Critical / High / Medium / Low),
  - scope (files/functions affected),
  - and an outline of the change you recommend.

The list should be roughly ordered by impact and urgency, not by file path.

### 3.3 Output for this step

At the end of §3.2, output the full written review report in markdown.

Stop there.

**Do NOT modify any code yet.**
The human (project owner) will read the report and decide which items to implement.

---

## 4. Optional remediation phase (ONLY if explicitly asked)

Only if the user explicitly instructs you to proceed with fixes:

- Implement remediation items starting from the top of the prioritized list in §3.2.6.
- Treat each logical change as a mini-doc:
  - restate the intent,
  - list files to modify,
  - then show patches and updated tests.
- Maintain all gate rules from CLAUDE.md and prior prompts:
  - docstrings, no magic numbers, <40-line functions, tenant isolation, etc.
- Keep changes tightly scoped; do not introduce new features.

If the user does not ask for remediation, do not make any code changes.
The review report alone is a valid and complete outcome for Document 17.

---

## 5. Output format (for this doc)

When answering this prompt, produce your answer in this order:

1. Confirmation that PROMPT_doc17_dora_roi_hardening_review.md was written to disk.
2. Orientation summary from §3.1.
3. Full written hardening review report from §3.2.
4. Statement that no code has been changed yet.
5. A short reminder that remediation will only proceed if the user explicitly requests it.

Do **not** include any code blocks in this document's answer.
This document's output is review text only.

---

## 6. References

External references you may consider (informational only, do not override specs):

- DORA RoI supervisory and implementation guidance (EBA, NCAs, etc.)
- Multi-tenant and RLS security best practices (OWASP, Postgres docs, SaaS patterns)
- Secure code review and hardening patterns for backend services

These are for broader judgment and terminology only. The project's own specs remain authoritative.
