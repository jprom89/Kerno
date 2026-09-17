# PROMPT_doc8_learning_pipeline.md — Build Instructions: Core Learning Pipeline

**Document Status:** Baseline v1.1 (Gap-Fixed)
**Scope:** Sprint 1 stories KER-101 through KER-114
**Prerequisites:** CLAUDE.md read and pre-flight questions answered

---

## Pre-Flight: Answer These Before Writing Code

Answer all three questions from CLAUDE.md §0 before proceeding. Write your answers as comments at the top of the first file you create.

---

## What You Are Building

The core data isolation and learning pipeline for Kerno's Compliance Copilot. This is the foundation everything else runs on. It must be secure, readable by non-engineers, and fully testable.

The output is 12 files in the order listed below. Complete each file before starting the next. Do not skip ahead.

---

## Build Order

### File 1: `config/constants.py`

Define every numeric constant used in the pipeline. Each constant must have a comment referencing the spec section it comes from. No constants may appear elsewhere in the codebase as raw numbers.

Required constants:
- `RETENTION_DECAY_FACTOR` — from LEARNING_PIPELINE_SPEC.md §5.2
- `SENIOR_REVIEWER_WEIGHT` — from LEARNING_PIPELINE_SPEC.md §5.2
- `JUNIOR_REVIEWER_WEIGHT` — from LEARNING_PIPELINE_SPEC.md §5.2
- `CALIBRATION_THRESHOLD_OVERRIDES` — from LEARNING_PIPELINE_SPEC.md §6.1
- `CALIBRATION_TARGET_ACCEPTANCE_RATE` — from LEARNING_PIPELINE_SPEC.md §6.2
- `MAX_SIMILAR_CONTROLS_RETURNED` — value: 5, from LEARNING_PIPELINE_SPEC.md §5.3

---

### File 2: `src/db/rls.py`

Implement `set_tenant_context()` and `TenantContextMissingError`.

Rules from CLAUDE.md §3:
- Raises `TenantContextMissingError` if tenant_id is None, empty, or not a valid UUIDv4
- Sets `SET LOCAL app.current_tenant_id` inside the provided connection
- Does NOT open its own connection — the caller owns the transaction
- Has a plain-English docstring a non-engineer can read

---

### File 3: `src/models/tenant.py`

SQLAlchemy model for the Tenant record.

Required fields:
- `tenant_id`: UUIDv4, immutable, server-default, primary key
- `registered_at`: timestamp with timezone, server-default now()
- `display_name`: non-null string
- `is_active`: boolean, default True

No field named `tenant_id` may accept input from the HTTP layer directly. The model must document this constraint in its docstring.

---

### File 4: `src/models/retrieval_bias.py`

SQLAlchemy model for the per-tenant retrieval bias vector.

Required fields:
- `id`: UUIDv4, primary key
- `tenant_id`: UUIDv4, foreign key to tenant, indexed
- `bias_vector`: pgvector column (1536 dimensions to match embedding model)
- `override_count`: integer, tracks how many overrides contributed to this vector
- `last_recalculated_at`: timestamp with timezone
- `created_at`: timestamp with timezone

---

### File 5: `src/services/tenant_context.py`

Application-layer wrapper that combines session resolution with `set_tenant_context()`.

Required:
- `resolve_and_set_tenant_context(session, conn)` — resolves tenant_id from the authenticated session object, validates it, calls `set_tenant_context(conn, tenant_id)`, returns the validated tenant_id
- `TenantContextMissingError` re-exported from `src/db/rls.py`
- Must never accept tenant_id from raw request input

---

### File 6: `src/services/anonymisation.py`

The anonymisation pipeline. Strips PII from inbound security metadata before any data is used for cross-tenant telemetry.

Required behaviour:
- Strips all five identifier types listed in LEARNING_PIPELINE_SPEC.md §4.2
- Returns a new string; never mutates the input
- Writes an entry to the structured logger for each stripping event (identifier type, not the value)
- Has unit tests that prove each identifier type is stripped

---

### File 7: `src/services/override_service.py`

Captures human override events and writes the immutable audit log entry.

Required:
- `capture_override(session, conn, override_input)` — validates input, writes override record, writes audit log entry, returns the created override
- Assigns `reviewer_confidence_weight` based on the reviewer's role: `SENIOR_REVIEWER_WEIGHT` for vCISO/fCISO, `JUNIOR_REVIEWER_WEIGHT` for internal admin
- Audit log entry must contain: `override_id`, `tenant_id`, `reviewer_id`, `reviewer_role`, `action_type` (approve/edit/reject), `original_control_id`, `corrected_control_id` (if edit/reject), `timestamp`
- Must call `resolve_and_set_tenant_context()` before any database write

---

### File 8: `src/services/bias_recalculation_service.py`

Implements the nightly weight recalculation formula from LEARNING_PIPELINE_SPEC.md §5.2.

Required:
- `recalculate_retrieval_bias(tenant_id, overrides, current_bias_vector)` — pure function, no database access, takes override records and current vector, returns updated vector
- The formula must match LEARNING_PIPELINE_SPEC.md §5.2 exactly
- Variable names must match CLAUDE.md §4.1 exactly — no spec notation in code
- `persist_retrieval_bias(conn, tenant_id, updated_vector, override_count)` — writes the updated vector to the database, requires tenant context already set

---

### File 9: `src/scheduler/nightly_bias_recalculation.py`

The nightly batch job that orchestrates the recalculation for all active tenants.

Required:
- Fetches all active tenants
- For each tenant: fetches overrides since last recalculation, fetches current bias vector, calls `recalculate_retrieval_bias()`, calls `persist_retrieval_bias()`
- Writes a structured log entry per tenant: tenant_id, override_count_processed, recalculation_duration_ms, success/failure
- Handles failures per-tenant without stopping the batch (one bad tenant must not block others)

---

### File 10: `src/services/retrieval_service.py`

The RAG query execution service with tenant bias injection.

Required:
- `get_similar_controls(session, conn, query_vector)` — calls `resolve_and_set_tenant_context()`, fetches tenant's `retrieval_bias_vector`, executes the calibrated similarity query from LEARNING_PIPELINE_SPEC.md §5.3, returns top-5 controls
- If no bias vector exists for the tenant yet, falls back to unbiased similarity search
- Every call must validate tenant context before query execution

---

### File 11: `migrations/002_create_embedding_table_with_rls.py`

Alembic migration that creates the `tenant_embeddings` table and activates the RLS policy.

Required:
- Creates the table with all required columns including `tenant_id`
- Executes the `ENABLE ROW LEVEL SECURITY` statement
- Creates the `tenant_isolation_policy` from LEARNING_PIPELINE_SPEC.md §3.2
- Includes a `downgrade()` that drops the policy and the table cleanly

---

### File 12: `tests/security/test_tenant_isolation.py`

**This is KER-113. It is Must-have. It must pass before any other story is marked complete.**

Required test cases:
1. `test_tenant_a_cannot_retrieve_tenant_b_embeddings` — given tenant A session, similarity query returns zero results from tenant B's data
2. `test_null_tenant_id_raises_error` — calling any query function with null tenant_id raises `TenantContextMissingError`
3. `test_empty_tenant_id_raises_error` — same for empty string
4. `test_invalid_uuid_raises_error` — same for a string that is not a valid UUID
5. `test_cross_tenant_override_not_visible` — tenant A cannot retrieve tenant B's override records
6. `test_cross_tenant_bias_vector_not_visible` — tenant A cannot retrieve tenant B's retrieval bias vector

Each test must use two separate tenant fixtures. The test database must have the RLS policy active (not just the application guard).

---

## Quality Gate

Before marking any file complete:

1. The file has a module-level docstring explaining what it does in plain English.
2. Every function has a plain-English docstring (CLAUDE.md §4.2).
3. No spec variable names appear in the code (CLAUDE.md §4.1).
4. No magic numbers appear — all constants reference `config/constants.py` (CLAUDE.md §4.3).
5. No function exceeds 40 lines (CLAUDE.md §4.5).
6. Every security-critical function has a negative test (CLAUDE.md §7.2).

---

## What Success Looks Like

When all 12 files are complete and all tests pass:

- The cross-tenant isolation test (KER-113) passes with RLS active at the database layer.
- The nightly batch (KER-114) runs end-to-end in the integration test and produces an updated `retrieval_bias_vector` for a tenant with at least one override.
- A non-engineer reading any function can understand what it does from the docstring alone.
- An investor reading `bias_recalculation_service.py` can trace the formula back to LEARNING_PIPELINE_SPEC.md without asking an engineer to explain it.

