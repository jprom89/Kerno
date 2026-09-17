# PROMPT_doc17b_dora_roi_schema_and_service_hardening.md
# Document 17B — DORA RoI Schema and Service Hardening (Items 5–10)
# Spec version: 1.0 | Status: Authoritative
# Type: Hardening patch — no new features, no new models, no new public API
# Source: Doc 17 hardening review §3.2.6 items 5–10
# Prerequisite: Doc 17A must be merged before this patch is applied

---

## 1. Purpose

This document specifies the remediation of the six remaining findings
from the Doc 17 DORA RoI Hardening Review (items 5–10).

Items 5–7 are bundled into a single Alembic migration.
Items 8–10 are pure service/code changes with no schema impact.

This document is complete when all six items are fixed,
tested, and gate-checked, and no new risks have been introduced.

---

## 2. Scope

In scope:
- Item 5: Standardize DateTime(timezone=True) across Doc 16 models and migration
- Item 6: Add FK constraint submission_runs.submission_window_id → submission_windows.id
- Item 7: Add CHECK constraints on status, validation_overall_status,
          provider_type, and criticality columns
- Item 8: Deduplicate _SELECT_OPEN_WINDOWS SQL between submission service and scheduler
- Item 9: Document and guard the race condition in _upsert_submission_run
- Item 10: Replace magic string "fail" with VALIDATION_SEVERITY_FAIL constant
           in _insert_draft_run

Out of scope:
- Any changes to items 1–4 already fixed in Doc 17A
- Any changes to models outside the DORA RoI slice
- Any new public API endpoints or new service functions
- Any frontend changes

---

## 3. Remediation items

### 3.1 Item 5 — Timezone-naive DateTime columns in Doc 16 models

Finding: submission_windows and submission_runs models use DateTime()
without timezone=True. All other Kerno timestamp columns use
DateTime(timezone=True). Mixed timezone awareness causes silent
comparison errors when filtering across tables.

Fix:
- In migration 013_dora_hardening_constraints.py, alter the following
  columns to TIMESTAMP WITH TIME ZONE (PostgreSQL) using
  op.alter_column with postgresql_using:
  - submission_windows.window_opens_at
  - submission_windows.window_closes_at
  - submission_windows.regulatory_deadline
  - submission_runs.submitted_at
  - submission_runs.validated_at
  - submission_runs.created_at
  - submission_runs.updated_at
- Use explicit postgresql_using clauses:
  e.g. postgresql_using="window_opens_at AT TIME ZONE 'UTC'"
- Do not change any Python model class files — alter via migration only.
  The SQLAlchemy model definitions already declare DateTime(timezone=True)
  as the target; this migration brings the DB schema into alignment.

---

### 3.2 Item 6 — Missing FK constraint on submission_runs.submission_window_id

Finding: submission_runs.submission_window_id references submission_windows.id
in the ORM but the FK constraint was never added to the database schema.
Orphaned runs can exist silently without referential integrity.

Fix:
- In migration 013_dora_hardening_constraints.py, add:
  op.create_foreign_key(
      "fk_submission_runs_window_id",
      "submission_runs",
      "submission_windows",
      ["submission_window_id"],
      ["id"],
      ondelete="RESTRICT"
  )
- ondelete="RESTRICT" is mandatory: a submission window must not be
  deletable while active runs reference it.

---

### 3.3 Item 7 — Missing CHECK constraints on enum-like columns

Finding: Four columns accept arbitrary string values at the DB level
despite being logically constrained to fixed sets. Invalid values can
be inserted via raw SQL or migration scripts, bypassing ORM validation.

Fix:
- In migration 013_dora_hardening_constraints.py, add the following
  CHECK constraints using op.execute (raw SQL):

  submission_runs.status:
    ALTER TABLE submission_runs
    ADD CONSTRAINT chk_submission_runs_status
    CHECK (status IN ('draft', 'ready', 'submitted', 'failed'));

  submission_runs.validation_overall_status:
    ALTER TABLE submission_runs
    ADD CONSTRAINT chk_submission_runs_validation_overall_status
    CHECK (validation_overall_status IN ('pass', 'warn', 'fail'));

  dora_register_entries.provider_type:
    ALTER TABLE dora_register_entries
    ADD CONSTRAINT chk_dora_register_entries_provider_type
    CHECK (provider_type IN ('cloud', 'software', 'data', 'other'));

  dora_register_entries.criticality:
    ALTER TABLE dora_register_entries
    ADD CONSTRAINT chk_dora_register_entries_criticality
    CHECK (criticality IN ('critical', 'important', 'standard'));

- The allowed values above must be derived from the existing constants
  in config.constants. Do not hardcode values that differ from the
  Python-layer constants.

Downgrade:
- The downgrade() function must drop all constraints and the FK added
  in items 5–7 and revert the DateTime columns to TIMESTAMP WITHOUT
  TIME ZONE, in reverse dependency order.

---

### 3.4 Item 8 — Duplicate _SELECT_OPEN_WINDOWS SQL in two files

Finding: The SQL string _SELECT_OPEN_WINDOWS is defined independently
in both dora_roi_submission_service.py and dora_roi_scheduler_service.py.
If one is updated, the other silently diverges.

Fix:
- Move _SELECT_OPEN_WINDOWS to a new private module-level constant in
  dora_roi_submission_service.py only (it is already defined there).
- In dora_roi_scheduler_service.py, remove the local definition and
  import _SELECT_OPEN_WINDOWS from dora_roi_submission_service:
    from src.services.dora_roi_submission_service import _SELECT_OPEN_WINDOWS
- No behavior change. No new public functions.

---

### 3.5 Item 9 — Undocumented race condition in _upsert_submission_run

Finding: _upsert_submission_run in dora_roi_submission_service.py uses
a SELECT-then-INSERT/UPDATE pattern. Under concurrent scheduler ticks,
two processes can both SELECT, both find no existing run, and both INSERT,
producing duplicate draft runs for the same window.

Fix (defensive guard, not full serializable transaction):
- Add an explicit unique constraint to the migration:
    op.create_unique_constraint(
        "uq_submission_runs_window_tenant",
        "submission_runs",
        ["submission_window_id", "tenant_id"]
    )
  This ensures the DB rejects the second INSERT with an IntegrityError
  even if the application layer does not catch the race.

- In _upsert_submission_run in dora_roi_submission_service.py, wrap the
  INSERT branch in a try/except for IntegrityError:
    from sqlalchemy.exc import IntegrityError
    try:
        conn.execute(INSERT ...)
    except IntegrityError:
        # Concurrent insert by another scheduler tick — silently ignore.
        # The existing run will be picked up on the next SELECT.
        pass

- Add a docstring to _upsert_submission_run that explicitly documents
  the race condition, why it can occur, and how the unique constraint
  plus IntegrityError guard mitigates it.

- The unique constraint op.create_unique_constraint belongs in migration
  013_dora_hardening_constraints.py alongside items 5–7.

New test required in test_dora_roi_scheduler_service.py:
| # | Test name | What it asserts |
|---|---|---|
| new | test_upsert_run_duplicate_insert_is_silently_ignored | Calling _upsert_submission_run twice with identical window/tenant raises no exception and does not create two rows |

---

### 3.6 Item 10 — Magic string "fail" in _insert_draft_run

Finding: _insert_draft_run passes the literal string "fail" as the
validation_overall_status for a draft run with no prior validation.
This bypasses the constant VALIDATION_SEVERITY_FAIL and would silently
break if the constant value is ever changed.

Fix:
- In dora_roi_submission_service.py, replace the literal string "fail"
  in _insert_draft_run with VALIDATION_SEVERITY_FAIL imported from
  config.constants.
- Verify the import is already present; add it if missing.
- No other behavior change.

New test required in test_dora_roi_submission_service.py:
| # | Test name | What it asserts |
|---|---|---|
| new | test_insert_draft_run_uses_validation_severity_fail_constant | The SQL params dict for the INSERT contains the value of VALIDATION_SEVERITY_FAIL, not a hardcoded string |

---

## 4. Migration file specification

File: migrations/versions/013_dora_hardening_constraints.py

Revision id: 013
Down revision: 012   (the last DORA RoI migration from Doc 16)
Branch labels: None
Depends on: None

upgrade() must apply in this order:
1. ALTER DateTime columns to TIMESTAMP WITH TIME ZONE (item 5)
2. CREATE FOREIGN KEY fk_submission_runs_window_id (item 6)
3. CREATE UNIQUE CONSTRAINT uq_submission_runs_window_tenant (item 9)
4. ADD CHECK constraints via op.execute (item 7)

downgrade() must reverse in strict reverse order:
1. DROP CHECK constraints
2. DROP UNIQUE CONSTRAINT
3. DROP FOREIGN KEY
4. ALTER columns back to TIMESTAMP WITHOUT TIME ZONE

---

## 5. Integration test specification

File: tests/integration/test_013_migration.py

This test file verifies the migration at the DB schema level using
the existing integration test pattern (inspect the live schema after
migration runs).

Required tests:
| # | Test name | What it asserts |
|---|---|---|
| 1 | test_submission_windows_timestamps_are_timezone_aware | window_opens_at, window_closes_at, regulatory_deadline are TIMESTAMPTZ |
| 2 | test_submission_runs_timestamps_are_timezone_aware | submitted_at, validated_at, created_at, updated_at are TIMESTAMPTZ |
| 3 | test_fk_submission_runs_window_id_exists | FK fk_submission_runs_window_id present in DB |
| 4 | test_unique_constraint_submission_runs_window_tenant_exists | UQ uq_submission_runs_window_tenant present in DB |
| 5 | test_check_constraint_submission_runs_status_exists | CHECK chk_submission_runs_status present in DB |
| 6 | test_check_constraint_validation_overall_status_exists | CHECK chk_submission_runs_validation_overall_status present in DB |
| 7 | test_check_constraint_provider_type_exists | CHECK chk_dora_register_entries_provider_type present in DB |
| 8 | test_check_constraint_criticality_exists | CHECK chk_dora_register_entries_criticality present in DB |

---

## 6. Consistency rules

- All new SQL constants use UPPER_SNAKE_CASE at module level.
- All new constraints use the naming convention:
    fk_{table}_{column}
    chk_{table}_{column}
    uq_{table}_{columns}
- No magic strings in service code — all status/severity values from
  config.constants.
- All new or modified functions must have docstrings.
- No function longer than 40 lines. Factor helpers if needed.
- IntegrityError import from sqlalchemy.exc only.

---

## 7. Gate checks

Apply to every changed or created file:

| Check | Rule |
|---|---|
| Module docstring present | Must be present and accurate for new files |
| All functions have docstrings | Including any new helpers |
| No spec notation in variable names | Plain English names only |
| No magic numbers or strings | All values from config.constants |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule followed | Guard is first in all service functions |
| TenantContextMissingError from src.exceptions | Not from any other module |

---

## 8. References

| Document | Authority |
|---|---|
| CLAUDE.md | Highest |
| This file | Authoritative for items 5–10 remediation |
| PROMPT_doc17_dora_roi_hardening_review.md | Source of findings |
| PROMPT_doc17a_dora_roi_hardening_remediation.md | Items 1–4 already merged |
| PROMPT_doc16_dora_roi_submission_workflow.md | Upstream contracts |
| PROMPT_doc15_dora_roi_export_validation.md | Upstream contracts |
| PROMPT_doc14_dora_roi_live_register.md | Upstream contracts |
