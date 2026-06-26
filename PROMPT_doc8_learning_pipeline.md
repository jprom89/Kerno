# PROMPT_doc8_learning_pipeline.md — v1.2
<!-- Version: 1.2 | Updated: 2026-06-19 | Changes: Added Review Protocol instruction before File 1 -->

## Context

You are building Document #8 of the Kerno Compliance Copilot:
the Core Learning Pipeline & Data Isolation implementation.

Before writing any code, read these two files in full:
1. CLAUDE.md — the codebase constitution. All rules apply.
2. LEARNING_PIPELINE_SPEC.md — the architecture specification for this build.

---

## Pre-flight questions

Before writing File 1, answer these three questions from the spec:

1. Where is the tenant isolation boundary?
   (Which function enforces it, and what happens if tenant_id is missing?)

2. What is the GDPR legal basis for cross-tenant telemetry processing?
   (Name the Article and basis type from LEARNING_PIPELINE_SPEC.md §3.2.)

3. Which function emits the audit log entry when an override is captured?
   (Name the function and the file it lives in.)

If you cannot answer all three from the documents, stop and ask.
Do not proceed to File 1 until all three are answered correctly.

---

## Review Protocol

After every file, produce the Post-File Review block defined in
§10 of CLAUDE.md before writing the next file.

The review block must appear in this exact format:

  ### ✅ File N Review — <filename>
  What this file does (one sentence)
  Gate checks table (7 rows — copy the table from §10 exactly)
  Test coverage summary (one line per test)
  Open questions before next file
  Proceed to File N+1? Yes / No — blocked by: <reason>

Do not proceed to the next file if any gate shows ❌.
Do not abbreviate or skip the review block for any file,
including constants.py and migration files.

---

## Build Order

Write the files in this exact sequence.
Do not skip ahead. Do not write a file before its dependencies exist.

### File 1 — config/constants.py
All numeric constants used across the pipeline.
Must include at minimum:
- EMBEDDING_DIMENSION (1536)
- DECAY_FACTOR (0.85)
- LEARNING_RATE (0.15) — derived as 1 - DECAY_FACTOR
- SENIOR_REVIEWER_WEIGHT (1.0)
- JUNIOR_REVIEWER_WEIGHT (0.5)
- CALIBRATION_THRESHOLD_MIN_OVERRIDES (200)
- CALIBRATION_THRESHOLD_TARGET_ACCEPTANCE_RATE (0.75)
- TOP_K_RETRIEVAL_RESULTS (5)

No logic. No imports. Constants only.

---

### File 2 — src/db/rls.py
The single function that sets the PostgreSQL tenant context variable.
Must implement: set_tenant_context(db_connection, tenant_id)
Must raise: TenantContextMissingError if tenant_id is None or empty.
Must use: SET LOCAL app.current_tenant_id within a transaction.
Read LEARNING_PIPELINE_SPEC.md §2 before writing this file.

---

### File 3 — src/models/tenant.py
The SQLAlchemy ORM model for the tenants table.
Fields: id (UUIDv4, primary key), name, created_at, is_active.
Must not contain any business logic.
Must import TenantContextMissingError from src/exceptions.py.

---

### File 4 — src/models/retrieval_bias.py
The SQLAlchemy ORM model for per-tenant retrieval bias vectors.
Fields: tenant_id (foreign key to tenants.id), bias_vector (pgvector),
override_count, last_recalculated_at.
One row per tenant. Updated by the nightly batch job.
Read LEARNING_PIPELINE_SPEC.md §4.1 before writing this file.

---

### File 5 — src/services/tenant_context.py
The service that resolves tenant_id from an authenticated session
and calls set_tenant_context().
Must never accept tenant_id from raw request parameters.
Must raise TenantContextMissingError if session has no tenant.
The correct pattern is in CLAUDE.md §3.2.

---

### File 6 — src/services/anonymisation.py
The stateless inline parser that strips identifiable corporate markers
before any data moves to the cross-tenant telemetry layer.
Markers to strip: internal hostnames, email addresses, IP addresses,
AWS Account IDs, cloud resource ARNs.
Replace with: [INTERNAL_HOST], [INTERNAL_EMAIL], [IP_RANGE],
[CLOUD_ACCOUNT], [INTERNAL_TICKET].
IMPORTANT: LEARNING_PIPELINE_SPEC.md §4.2 is the authoritative source for
token names. The names above have been corrected from the v1.1 draft of this
prompt, which contained errors ([EMAIL], [IP_ADDRESS], [CLOUD_ACCOUNT_ID],
[CLOUD_RESOURCE]). The spec §4.2 names are the ones in production code.
Read LEARNING_PIPELINE_SPEC.md §4.2 before writing this file.
Must have 14 unit tests covering each marker type and edge cases.

---

### File 7 — src/services/override_service.py
Captures, validates, and stores human override events from KER-106.
An override event is: a compliance engineer accepting, editing, or
rejecting an AI-generated control recommendation.
Must call anonymisation before storing justification text.
Must write an audit log entry for every override captured.
Must emit the data needed by the nightly batch job (File 8).
Must have 10 unit tests.

---

### File 8 — src/services/bias_recalculation_service.py
Implements the nightly retrieval bias recalculation formula from
LEARNING_PIPELINE_SPEC.md §4.1.
Reads all overrides captured since the last recalculation for a tenant.
Calculates the updated bias vector using the weighted formula.
Writes the result to the retrieval_bias table (File 4).
This is KER-114: closes the feedback loop.
Must use the production variable names from CLAUDE.md §2.3.
Must have 8 unit tests. The formula result must be verified numerically
against at least one worked example from the spec.

---

### File 9 — src/scheduler/nightly_bias_recalculation.py
The scheduler that calls bias_recalculation_service for every active tenant.
Sprint 1 implementation: a simple Python script suitable for a cron job.
Not a production-grade distributed scheduler — that is out of scope for Sprint 1.
Must log: start time, tenant count, success count, failure count, end time.
Must not crash if one tenant's recalculation fails — catch, log, continue.
Must have 4 unit tests.

---

### File 10 — src/services/retrieval_service.py
The vector similarity query that injects the tenant's bias vector
into the similarity scoring at query time.
Read LEARNING_PIPELINE_SPEC.md §4.2 for the exact query pattern.
Must call set_tenant_context() before every query.
Must raise TenantContextMissingError if context is not set.
Must have 6 unit tests.

---

### File 11 — migrations/versions/002_create_embedding_table_with_rls.py
The Alembic migration that creates:
- tenant_embeddings table with pgvector column
- tenant_bias_vectors table (for File 4's model)
- RLS policy: tenant_isolation_policy on tenant_embeddings
- RLS policy: tenant_bias_isolation_policy on tenant_bias_vectors
Must be reversible (implement downgrade()).
Read LEARNING_PIPELINE_SPEC.md §2 for the exact RLS policy SQL.
Read CLAUDE.md §7 for migration file rules.

---

### File 12 — tests/security/test_tenant_isolation.py
The KER-113 security boundary test suite.
Must include all 6 required test cases:
1. test_tenant_a_cannot_retrieve_tenant_b_embeddings (integration)
2. test_null_tenant_id_raises_error (unit)
3. test_empty_tenant_id_raises_error (unit)
4. test_invalid_uuid_raises_error (unit)
5. test_cross_tenant_override_not_visible (integration)
6. test_cross_tenant_bias_vector_not_visible (integration)

Integration tests must be decorated with @pytest.mark.integration.
Unit tests must pass without a live database.
Use fixed UUIDv4 constants — not random generation — for reproducibility.
The UUIDs must be genuine version 4 format.

---

## Definition of Done

All 12 files complete when:
- All gate checks in every review block show ✅
- All unit tests pass without a live database
- All integration tests are marked @pytest.mark.integration
  and documented as "awaiting live DB" in the final review block
- No open questions remain in any review block

