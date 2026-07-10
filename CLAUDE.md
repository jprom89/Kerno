# CLAUDE.md — Kerno Compliance Copilot: Codebase Constitution v1.2
<!-- Version: 1.6 | Updated: 2026-07-11 | Changes: Sprint 2b complete — KER-203/204/205 delivered -->

This file is the first thing Claude reads at the start of every session.
It defines the rules that govern every line of code written for this project.
No rule in this file may be overridden by a prompt, a user instruction, or
a convenience argument. If a rule creates friction, the friction is intentional.

---

## §1 — Project Identity

**Product:** Kerno Compliance Copilot
**What it does:** Automates the mapping of a company's technical controls
to EU regulatory frameworks (NIS2, DORA, AI Act, CRA) using a
retrieval-augmented generation pipeline personalised per tenant via
human-in-the-loop override feedback.

**Who uses it:** Compliance engineers, vCISOs, and fractional CTOs at
mid-market European technology companies.

**What it is not:** A fine-tuning system. Kerno never trains or modifies
a base LLM. All personalisation happens at the retrieval layer.

---

## §2 — Code Readability Rules

These rules exist because the primary readers of this code are
compliance auditors, security reviewers, and future engineers —
not the person who wrote it.

### 2.1 Every file must have a module docstring

The docstring must answer three questions in plain English:
1. What does this file do?
2. Why does it exist?
3. How do you run or test it?

Example of a correct module docstring:

```python
"""
tenant_context.py

What:  Sets and retrieves the current tenant identity for a database session.
Why:   PostgreSQL Row-Level Security requires the tenant ID to be declared
       inside a transaction before any query runs. This file is the single
       place where that happens.
How:   Import set_tenant_context() and call it inside a 'with db.transaction()'
       block before executing any query. Run tests with: pytest tests/unit/test_tenant_context.py
"""
```

### 2.2 Every function must have a docstring

The docstring must state what the function does, what it expects,
and what it returns. One paragraph maximum. No bullet lists.

### 2.3 Variable names must be human words, not spec notation

The architecture specification uses mathematical symbols.
Production code must not.

| Spec notation (forbidden) | Production name (required) |
|---|---|
| W_ret | retrieval_bias_vector |
| W_ret_new | updated_retrieval_bias_vector |
| alpha | decay_factor |
| gamma_i | reviewer_confidence_weight |
| V_err | override_error_vector |
| V_target | target_control_vector |
| V_source | source_recommendation_vector |

If a new spec symbol appears that is not in this table, stop and ask
for the production name before writing any code.

### 2.4 No magic numbers

Every numeric literal that is not 0 or 1 must be assigned to a named
constant in config/constants.py before use. The constant name must
explain what the number means.

Forbidden:
```python
updated_bias = 0.85 * old_bias + 0.15 * delta
```

Required:
```python
from config.constants import DECAY_FACTOR, LEARNING_RATE
updated_bias = DECAY_FACTOR * old_bias + LEARNING_RATE * delta
```

### 2.5 No function longer than 40 lines

If a function exceeds 40 lines, it is doing more than one thing.
Split it. Name each part after what it does.

### 2.6 No clever code

Clever code is code that requires the reader to hold more than one
concept in their head simultaneously to understand what it does.

Forbidden patterns:
- Nested list comprehensions with conditions
- Chained ternary operators
- Walrus operators inside complex expressions
- Lambda functions assigned to variables (use def)
- Single-letter variable names outside of loop counters

If the code looks impressive, it is probably wrong for this codebase.

---

## §3 — Tenant Isolation: Non-Negotiable Security Rule

This is the most important rule in this file.
A violation is a security defect, not a style issue.

### 3.1 The rule

Every function that opens a database connection and executes a query must:

1. Call `set_tenant_context(tenant_id)` before any query runs.
2. Wrap the context-setting and the query in the same transaction block.
3. Raise `TenantContextMissingError` if `tenant_id` is `None` or empty string —
   never proceed silently with a missing context.
4. Never accept `tenant_id` directly from user-supplied request input —
   always resolve it from the authenticated session object.

### 3.2 The correct pattern

```python
def get_controls_for_tenant(session_context: SessionContext) -> list[Control]:
    """
    Retrieves all compliance controls belonging to the current tenant.
    Requires an authenticated SessionContext. Raises TenantContextMissingError
    if the session does not contain a valid tenant ID.
    """
    tenant_id = session_context.tenant_id
    if not tenant_id:
        raise TenantContextMissingError("tenant_id is required before querying controls")

    with database.transaction() as db:
        set_tenant_context(db, tenant_id)
        return db.query(Control).all()
```

### 3.3 The forbidden pattern

```python
# FORBIDDEN — never do this
def get_controls(tenant_id: str) -> list[Control]:
    with database.transaction() as db:
        # Missing: set_tenant_context() call
        return db.query(Control).filter(Control.tenant_id == tenant_id).all()
```

The filter `Control.tenant_id == tenant_id` does not substitute for
`set_tenant_context()`. The RLS policy is a safety net, not the primary
enforcement mechanism. Application-layer enforcement is mandatory.

### 3.4 TenantContextMissingError

This exception must be defined in `src/exceptions.py` and imported
wherever database access occurs. It must never be caught silently.

---

## §4 — File Naming and Structure

Every file has exactly one home. Consult FILE_STRUCTURE.md before
creating any new file. If a file does not appear in FILE_STRUCTURE.md,
stop and ask where it belongs before creating it.

Source files: `src/`
Configuration and constants: `config/`
Database migrations: `migrations/versions/`
Unit tests: `tests/unit/`
Integration tests: `tests/integration/`
Security boundary tests: `tests/security/`
Documentation and specs: project root

---

## §5 — The Learning Pipeline Specification

Before writing any code in the following files, read LEARNING_PIPELINE_SPEC.md:

- src/db/rls.py
- src/models/retrieval_bias.py
- src/services/bias_recalculation_service.py
- src/services/retrieval_service.py
- src/scheduler/nightly_bias_recalculation.py
- migrations/versions/002_create_embedding_table_with_rls.py

The spec defines the mathematical model, the GDPR legal basis for processing,
the data classification boundaries, and the exact retrieval query pattern.
Code that contradicts the spec is wrong, even if it passes tests.

---

## §6 — GDPR and Data Classification

Two data layers exist. They must never be mixed.

**Tenant-Specific Context Layer (High Sensitivity)**
Contents: manual overrides, override justification text, risk register
descriptions, internal security policy text.
Storage: RLS-bounded PostgreSQL table (tenant_embeddings).
Rule: this data never leaves the tenant's isolated container and is never
used for cross-tenant model optimisation.

**Cross-Tenant Telemetry Layer (Low Sensitivity)**
Contents: aggregate matching success rates, abstract precision scores,
token usage counts.
Storage: centralised analytics table.
Rule: individual tenant data is anonymised before aggregation.
The anonymisation pipeline (src/services/anonymisation.py) is mandatory
for all data moving from the tenant layer to the telemetry layer.

The GDPR legal basis for cross-tenant optimisation is Article 6(1)(f)
Legitimate Interest. This basis is documented in LEARNING_PIPELINE_SPEC.md §3.2.
Code comments must reference this when processing cross-tenant telemetry.

---

## §7 — Database Migration Rules

Every schema change must be a numbered Alembic migration file.
Migration files must be named: `NNN_description_in_snake_case.py`
where NNN is a zero-padded three-digit sequence number.

Every migration file must:
- Enable RLS on every new table that stores tenant data
- Define the tenant_isolation_policy for every new table
- Be reversible (implement the `downgrade()` function)
- Include a docstring explaining what the migration changes and why

---

## §8 — Sprint 1 Story Reference

## Sprint 1 — CLOSED
All Must-have and Should-have stories delivered.
Final suite: 343 passed, 0 failed (unit + security + integration).
Dev DB at migration head s4t5u6v7.
Closed: 2026-07-04

These are the 14 stories for Sprint 1. Files must implement exactly
what the story specifies — no more, no less.

| Story ID | Title | Must-have? | Implementing file(s) |
|---|---|---|---|
| KER-101 | Tenant model and UUID assignment | Yes | src/models/tenant.py |
| KER-102 | RLS policy migration | Yes | migrations/versions/002_... |
| KER-103 | Tenant context service | Yes | src/services/tenant_context.py |
| KER-104 | Evidence retrieval query | Yes | src/services/retrieval_service.py |
| KER-105 | AI control mapping engine | Yes | src/services/mapping_service.py |
| KER-106 | Override capture and storage | Yes | src/services/override_service.py |
| KER-107 | Anonymisation pipeline | Yes | src/services/anonymisation.py |
| KER-108 | Jira side-panel integration | Yes | src/integrations/jira.py |
| KER-109 | Trust Center status display | Yes | src/api/trust_center.py |
| KER-110 | Webhook ingestion endpoint | Yes | src/api/webhooks.py |
| KER-111 | Evidence pack export | Yes | src/services/export_service.py |
| KER-112 | Audit log write | Yes | src/services/audit_log.py |
| KER-113 | Cross-tenant isolation test | Yes | tests/security/test_tenant_isolation.py |
| KER-114 | Nightly weight recalculation stub | Should | src/services/bias_recalculation_service.py, src/scheduler/nightly_bias_recalculation.py |

### Sprint 1 status notes (updated 2026-07-03)

- KER-107 — ✅ Done. Delivered as the tamper-evident, hash-chained, append-only
  audit ledger (src/services/audit_log.py, migration 016, PR #1). Numbering
  note: the active sprint backlog labels the audit ledger KER-107; in the table
  above that scope corresponds to the KER-112 row ("Audit log write", now
  implemented by the ledger), while the table's KER-107 row ("Anonymisation
  pipeline") also shipped earlier in src/services/anonymisation.py.
- KER-108 — ✅ Done (MVP). Implemented as src/api/routers/panel.py,
  src/api/schemas/panel.py, src/dashboard/panel.html, and
  src/dashboard/js/panel.js (src/integrations/jira.py deferred — the MVP is the
  embedded panel surface itself). Jira iframe token hand-off deferred
  post-Sprint 1. reviewer_role and reviewer_id are user-provided pending
  per-user JWT claims.
- KER-109 — ✅ Done. Coverage summary + drill-down. Override-wins resolution
  matrix. WCAG AA. Links to KER-108 panel per control. Implemented as
  src/services/coverage_service.py, src/api/routers/coverage.py,
  src/api/schemas/coverage.py, src/dashboard/coverage.html, and
  src/dashboard/js/coverage.js. Numbering note: the active sprint backlog
  labels the control-coverage dashboard KER-109; the table's KER-109 row
  ("Trust Center status display", src/api/trust_center.py) is the external
  Trust Center surface and remains open.
- KER-110 — ✅ Done. Remediation routing: gap → Jira task with SLA due date and
  assignee. Closure → re_review_flagged_at. Both actions in KER-107 audit
  ledger. Migration 017 unapplied to dev DB — run alembic upgrade head before
  integration tests. Implemented as src/services/remediation_service.py,
  src/services/jira_client.py, and src/api/routers/remediation.py. Numbering
  note: the active sprint backlog labels remediation routing KER-110; the
  table's KER-110 row ("Webhook ingestion endpoint", src/api/webhooks.py) is
  the generic ingestion surface and remains open.
- KER-111 — ✅ Done. Deterministic JSON evidence pack export per control family.
  Covers system-of-record statuses, evidence refs, human decisions, and KER-107
  audit extract. Generation recorded in ledger. Validates against EvidencePack
  Pydantic schema. Migrations 017 still unapplied to dev DB. Implemented as
  src/services/export_service.py, src/api/schemas/export.py, and
  src/api/routers/export.py — matching the table's KER-111 row
  (src/services/export_service.py).
- KER-113 — ✅ Done. FORCE ROW LEVEL SECURITY applied to all 11 policy-bearing
  tables via migration 018. Owner role now subject to its own RLS policies.
  Tenant isolation holds at DB layer + app layer + audit trigger.
  test_cross_tenant_override_not_visible and
  test_cross_tenant_bias_vector_not_visible both green. 334/334 — first fully
  green suite. (Note: migrations 017 and 018 are both applied to the dev DB as
  of this entry, superseding the KER-110/111 "017 unapplied" notes above.)
- KER-114 — ✅ Done. Nightly weight recalculation stub.
  POST /api/v1/scheduler/run-recalculation triggers manually
  (JWT-authenticated). Emits structured log + KER-107 audit entry per run.
  Full §5.2 recalculation math already present in
  bias_recalculation_service.py — wiring deferred to post-Sprint 1.
  343/343 — Sprint 1 complete.

---

## §9 — Security Hardening (KER-SEC-01)
Audit date: 2026-07-05
Grade: B → B+ (post-remediation)

Resolved:
- SEC-01: reviewer_role constrained to ReviewerRole enum (VCISO/FCISO/INTERNAL_ADMIN);
  actor_attribution honest marker added to override audit entries.
  (Superseded — see "Resolved (Sprint 2a)" below for the full fix.)
- SEC-02: seed script hard-exits unless KERNO_ENV=development;
  plaintext password no longer printed.
- SEC-03/04: generic RuntimeError handler with correlation ID;
  JiraClientError no longer leaks to HTTP responses.
- SEC-05: slowapi rate limiting on scheduler (10/min),
  export (30/min), overrides (60/min).
- SEC-06: uv.lock generated (54 packages, reproducible installs).

Resolved (Sprint 2a):
- SEC-01 (full): per-user JWT identity live — reviewer_id and
  reviewer_role sourced from verified JWT claims;
  OverrideRequest.reviewer_role removed from request schema.
- SEC-07: log hygiene — audit entries now carry real actor_id
  (user_id from JWT); actor_attribution placeholder removed.
- SEC-08: export role field — reviewer_role in override audit
  after_state is now the JWT-derived ReviewerRole enum value.

Open (deferred):
- SEC-05 (full): gateway-level rate limiting — pending infra decision.

---

## §10 — GTM Correction (Pitch Material Alignment)

The GTM Strategy document states that vCISO referral partnerships
are a near-term acquisition channel. This is incorrect.

vCISOs will not refer a pre-PMF tool to clients they are accountable for.
The vCISO referral channel activates after 20+ paying logos exist.

When any code, copy, or documentation references customer acquisition
channels, the correct sequencing is:
- Year 1: Founder-led direct sales only
- Year 2: vCISO referral partnerships (after 20+ logos)
- Year 3+: White-label channel expansion

---

## §11 — Post-File Review Protocol (Non-Negotiable)

After writing every file, before moving to the next file in the build order,
produce a review block in this exact format:

---

### ✅ File N Review — filename.py

**What this file does (one sentence a non-engineer can read)**
Plain English. No jargon. If it cannot be explained in one sentence,
the file is doing too much.

**Gate checks**

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ / ❌ | |
| All functions have docstrings | ✅ / ❌ | List any missing |
| No spec notation in variable names | ✅ / ❌ | e.g. W_ret, gamma_i are forbidden |
| No magic numbers | ✅ / ❌ | List any bare literals |
| No function longer than 40 lines | ✅ / ❌ | List any violators |
| Tenant isolation rule followed (if DB file) | ✅ / ❌ / N/A | |
| TenantContextMissingError raised on null/empty context | ✅ / ❌ / N/A | |

**Test coverage summary**
List each test and whether it passes, fails, or is marked integration
(waiting for live DB). Format:
- test_name — ✅ passes / ❌ fails / 🔶 integration (needs live DB)

**Open questions before next file**
List anything ambiguous, any assumption made, or any dependency on a
previous file that has not yet been confirmed. If there are none, write:
"None — ready to proceed."

**Proceed to File N+1?**
Write either:
- "Yes — all gates pass, no open questions."
- "No — blocked by: <reason>. Waiting for instruction."

---

### Why this protocol exists

Claude must not silently accumulate decisions across files.
Each file is a contract. The review block is the signature on that contract.
If a gate fails, Claude stops and waits — it does not proceed and fix it later.
Fixing problems introduced at file N when writing file N+1 is how
codebases become unreadable.

The review block also serves as the human-readable audit trail.
A compliance auditor, a new engineer, or an investor reviewing the codebase
must be able to read the review blocks and understand every decision
made during the build without reading the code itself.

### What Claude must NOT do

- Must not skip the review block, even for "simple" files like constants.py.
- Must not abbreviate the gate table.
- Must not mark a gate ✅ if it has not actually checked it.
- Must not write "None" under open questions if there is any ambiguity.
- Must not proceed to the next file if any gate shows ❌.

---

## §12 — Sprint 2a Backlog

**Sprint goal:** Close the learning-loop and identity gaps left open after
Sprint 1 — activate real nightly bias recalculation (KER-201) and replace the
placeholder actor identity with verified per-user authentication and RBAC
(KER-202).

### Regulatory update (recorded 7 July 2026)

The EU Digital Omnibus on AI (adopted 29 June 2026) defers the Annex III
high-risk obligations — including EU AI Act Article 19 log retention — from
2 August 2026 to 2 December 2027. The AI-decision log story (KER-203) is
therefore no longer a hard-deadline emergency and moves to Sprint 2b. Sprint 2a
is scoped to two stories: KER-201 and KER-202.

### KER-202 — Per-user identity and RBAC enforcement

- **Priority:** Must-have · **Points:** 13 · **Reg tie:** EU AI Act Article 14
  (human-oversight accountability); NIS2 (audit-trail attribution).

**Acceptance criteria:**
1. New users table (migration 019): user_id UUID PK, tenant_id, email (unique
   per tenant), scrypt password_hash, role, is_active, created_at. RLS +
   tenant_isolation_policy on the users table.
2. Login issues a JWT carrying user_id (as sub), email, role, tenant_id.
3. Override capture records reviewer_id from the verified JWT user_id and
   reviewer_role from the verified JWT role claim.
4. OverrideRequest.reviewer_role field is REMOVED — role is never accepted from
   the request body. The ReviewerRole enum (§9 SEC-01) still bounds the value.
5. The actor_attribution="tenant_principal_pending_per_user_auth" marker is
   removed from override audit after_state; the ledger now attributes to a real
   actor_id. The TODO at src/api/routers/overrides.py:52 is removed.
6. RBAC gates on the six roles:
   - Auditor = read-only (403 on any write)
   - Compliance Lead + vCISO = approve/override
   - Platform Engineer = connector/webhook management
   - Security Engineer + End-Customer Admin = per §4 existing scope
7. All existing tests continue to pass; auth fixtures updated to mint per-user
   JWTs; new tests cover role gating (403 cases) and the removed request field.
8. Resolves SEC-01 fully; auto-resolves SEC-07/08 (update §9 open items).

**Design decisions implemented (KER-202):**
1. **REVIEWER_ROLE_MAP** (src/services/override_service.py) bridges the two role
   vocabularies without merging them: a user's 6-value RBAC role (JWT claim,
   config.constants.RbacRole) maps to the 3-value override ReviewerRole enum used
   for confidence weighting —
     vciso -> VCISO (senior 1.0), compliance_lead -> VCISO (senior 1.0),
     security_engineer -> FCISO (senior 1.0),
     platform_engineer -> INTERNAL_ADMIN (junior 0.5),
     end_customer_admin -> INTERNAL_ADMIN (junior 0.5),
     auditor -> None (read-only — 403 before any DB write).
   OVERRIDE_CAPABLE_ROLES is derived from the map (every non-None role) so the
   allow-list and the map never drift. reviewer_role is always derived from the
   verified JWT role, never accepted from the request body.
2. **users table RLS without FORCE** (migration 019) — a deliberate exception to
   the migration-018 FORCE rule. Login must look up a user by email before any
   tenant context exists, and FORCE would block even the owner role from that
   pre-context read (proven: SET row_security=off errors under FORCE). So users
   gets ENABLE ROW LEVEL SECURITY but NOT FORCE, with a context-optional policy
   that permits reads when app.current_tenant_id is unset (login scan) and
   restricts to the tenant otherwise. This mirrors how migration 018 leaves the
   tenants table unforced for the same auth-bootstrap reason. Security note:
   without FORCE the owner role bypasses the policy, so users isolation relies on
   the fact that only the login query reads users (subsequent requests read
   identity from the JWT and never re-query users).

### KER-201 — Activate real nightly bias recalculation

- **Priority:** Must-have · **Points:** 8 · **Reg tie:** EU AI Act Article 14
  (human oversight — override feedback must actually influence retrieval).

The pure-math recalculate_retrieval_bias / persist_retrieval_bias and the
per-tenant batch orchestrator run_nightly_bias_recalculation are already
implemented and unit-tested. This story is stub-wiring + scheduling + end-to-end
proof only — NOT bug-fixing (the bugs listed in earlier drafts were verified
already-resolved at head s4t5u6v7).

**Acceptance criteria:**
1. The KER-114 stub path (run_recalculation_stub + POST /api/v1/scheduler/
   run-recalculation) is replaced by / delegates to the real batch, so a manual
   trigger performs an actual recalculation and updates retrieval_bias.
2. Formula uses existing constants exactly: DECAY_FACTOR=0.85, LEARNING_RATE=0.15,
   SENIOR_REVIEWER_WEIGHT=1.0, JUNIOR_REVIEWER_WEIGHT=0.5.
3. A nightly scheduling mechanism is wired into the app (APScheduler or cron
   entrypoint — document the chosen mechanism in CLAUDE.md).
4. Each real recalculation emits a KER-107 ledger entry:
   action="bias_recalculated", object_type="bias_vector",
   after_state={override_count, dimensions, updated_at} — replacing the stub
   marker for real runs.
5. Integration test (live DB): seed overrides for a tenant, run the batch, assert
   retrieval_bias moved in the expected direction and the subsequent
   get_similar_controls ranking reflects the new bias.
6. Per-tenant failure isolation retained.
7. TODO blocks at nightly_bias_recalculation.py:90 and
   bias_recalculation_service.py:57 removed.

**Design decisions implemented (KER-201):**
1. **Scheduling mechanism (AC-3): cron entrypoint, not APScheduler.** The nightly
   trigger is `python -m src.scheduler.nightly_bias_recalculation` (a `main()` in
   the scheduler module), invoked by the platform scheduler — cron on Linux,
   Task Scheduler on Windows dev. Chosen over APScheduler because it adds no
   dependency (uv.lock unchanged, §9 SEC-06), keeps retries/alerting with the
   platform scheduler, and cannot interfere with API worker processes. The
   manual per-tenant path (POST /api/v1/scheduler/run-recalculation) delegates
   to the same shared core, so both paths produce identical writes.
2. **pgvector text coercion (coerce_vector).** psycopg2 returns pgvector columns
   as text (no client adapter is registered), so the "ready-made" batch would
   have crashed on any live vector (`list(row[0])` yields characters). Proven by
   live probe during implementation. `coerce_vector` in
   bias_recalculation_service.py is the single parser; used by the scheduler
   fetches AND by retrieval_service._fetch_tenant_bias_vector — the one touch
   outside the KER-201 file list, required for AC-5's ranking assertion (the
   biased similarity query needs a real float list).
3. **No-new-overrides runs write nothing.** A tenant with zero overrides since
   last_recalculated_at is skipped: no bias upsert (the column is
   vector(1536) NOT NULL — an uncalibrated tenant has no persistable vector)
   and no ledger entry (a nightly no-op entry per silent tenant would bloat the
   KER-107 chain). The manual endpoint reports status="no_new_overrides".
4. **PLATFORM_SCHEDULER_TENANT_ID** — a fixed, valid-v4, deliberately
   nonexistent UUID the batch presents to the §3 tenant-context guard for its
   one internal query (listing active tenants; the tenants table is unforced
   per migration 018). It satisfies the guard without bypassing it: if the
   tenants table were ever policy-forced, the batch would see zero tenants
   (fail closed) rather than leak.
5. **persist_retrieval_bias returns its timestamp** so the ledger entry's
   after_state.updated_at is byte-identical to the row's last_recalculated_at.

### Dependency table

| Story | Depends on | Nature |
|---|---|---|
| KER-202 | — | Independent; foundational for later surfaces (audit-actor + RBAC gating) |
| KER-201 | — | Independent (uses existing overrides / tenant_embeddings / retrieval_bias) |

Neither story has a cross-story prerequisite. Recommended order: **KER-202
first** (its per-user-JWT fixture change ripples through tests), then KER-201.

### Capacity table

| Set | Stories | Points |
|---|---|---|
| **Sprint 2a total (Must-have only)** | KER-202 (13) + KER-201 (8) | **21** |

Baseline: the full test suite must stay green at Sprint 2a close.
Deferred to later sprints: KER-203 (Sprint 2b — Art. 19 deadline now
2 Dec 2027), KER-204 (Trust Center), KER-205 (webhook ingestion).

> ### 🏁 Sprint 2a — Definition of Done (banner)
> Sprint 2a is closed only when **all of the following hold**:
> 1. All baseline tests + every new KER-201/KER-202 test are green
>    (unit + security + integration), 0 failed.
> 2. Both production TODO markers are removed:
>    nightly_bias_recalculation.py:90 and bias_recalculation_service.py:57
>    (KER-201), and the overrides.py actor_attribution placeholder (KER-202).
> 3. Migration 019 is applied and physically verified on the dev DB
>    (users table present with RLS + tenant_isolation_policy).
> 4. SEC-01 is marked closed in §9 (per-user identity landed; role no longer
>    request-supplied; audit attributes to a real actor); SEC-07/08 reviewed.

---

## §13 — Sprint 2b Backlog

**Sprint goal:** Make the AI decision trail retainable and queryable (KER-203),
give tenants a public compliance face (KER-204), and open a secure evidence
intake channel (KER-205) — before the September 2026 customer rollout.

### Regulatory context (recorded 9 July 2026)

The EU Digital Omnibus on AI entered into force ~2 July 2026. Article 19 log
retention (Annex III high-risk) now bites on 2 December 2027 — but KER-203
ships in THIS sprint anyway: NIS2/DORA enterprise buyers ask for decision-log
retention during procurement, and retrofitting logging under every
recommendation write after launch is far more expensive than building it in
before the September 2026 rollout. The legal deadline is the backstop, not
the driver.

Baseline (verified 9 July 2026, commit 76fc09a): CLAUDE.md v1.4; migration
head t5u6v7w8 (019 — users); 373 tests passing, 0 failed;
src/api/trust_center.py and src/api/webhooks.py do not exist (both §8 rows
still open — this sprint creates them); no IngestService ORM layer exists —
KER-205 builds a thin normalisation layer over context_records (migration
007) and the existing evidence-linking patterns.

### KER-203 — AI-decision log retention

- **Priority:** Must-have · **Points:** 13 · **Reg tie:** EU AI Act
  Articles 12, 19, 26 (deadline 2 Dec 2027; ship before Sep 2026 rollout).

**Acceptance criteria:**
1. New ai_decision_log table (migration 020): correlation_id UUID PK,
   tenant_id UUID NOT NULL, control_id UUID NOT NULL, evidence_ids UUID[]
   NOT NULL, input_snapshot_hash TEXT NOT NULL, output_status TEXT NOT NULL,
   confidence_score FLOAT NOT NULL, rationale_extract TEXT NOT NULL,
   model_version TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now().
   ENABLE + FORCE ROW LEVEL SECURITY with tenant_isolation_policy — this is a
   pure tenant-data table; the users-table auth-bootstrap exception
   (§12 KER-202 decision 2) does NOT apply. Indexes on (tenant_id, created_at),
   (control_id), (confidence_score).
2. Every recommendation generation (mapping_service.map_control) emits exactly
   one ai_decision_log row in the same transaction as the recommendation
   write — commit and rollback together. No recommendation can exist without
   its log entry (same atomicity pattern as override + KER-107 ledger).
3. This log is SEPARATE from the KER-107 human-decision ledger — append-only
   but NOT hash-chained. Different retention, volume, and query profile;
   hash-chaining at recommendation volume would be pure overhead.
4. Entries retained >= 180 days. A prune job removes rows past the configured
   window. Window defined as AI_DECISION_LOG_RETENTION_DAYS in
   config/constants.py (default 180, §2.4-compliant named constant). Prune job
   runnable as a cron entrypoint alongside the KER-201 scheduler:
   python -m src.scheduler.prune_ai_decision_log
5. Query API (JWT-scoped; tenant_id from the authenticated session — never
   from the request): GET /api/v1/ai-decisions with optional query params
   control_id (UUID), after (ISO date), confidence_gte (float 0–1).
6. GDPR alignment: input_snapshot_hash only (SHA-256 of the raw input
   snapshot — never the snapshot itself). No raw personal data in any log
   field. Legal basis (EU AI Act Article 19) documented in
   docs/ai_decision_log_runbook.md.
7. Integration test (live DB): a recommendation write produces a retained,
   queryable ai_decision_log entry; the prune job deletes rows outside the
   retention window and retains rows inside it.

**Design decisions (KER-203):**
1. **FORCE RLS, explicitly.** ai_decision_log holds nothing but tenant data
   and is only ever read/written under an authenticated tenant context, so it
   gets the full migration-018 treatment (ENABLE + FORCE + policy).
2. **Append-only without hash-chaining.** The KER-107 ledger proves human
   decisions are tamper-evident; the AI log proves the machine's decisions are
   retained and reconstructable. Conflating them would couple a low-volume
   forensic chain to a high-volume operational log.
3. **Hash-only input snapshots.** SHA-256 of the canonical JSON of the mapping
   inputs. Verifiable ("was THIS input what produced THAT output?") without
   storing personal data. model_version comes from KERNO_LLM_MODEL.
4. **Prune follows the KER-201 scheduler pattern** — cron entrypoint, no new
   dependency, per the §12 KER-201 decision 1 rationale. Prune runs are
   logged; prune does NOT write KER-107 entries per row (volume).

**Files to create:** src/models/ai_decision_log.py,
src/services/ai_decision_log_service.py, src/api/routers/ai_decisions.py,
src/api/schemas/ai_decisions.py,
migrations/versions/020_create_ai_decision_log.py,
src/scheduler/prune_ai_decision_log.py, docs/ai_decision_log_runbook.md,
tests/unit/services/test_ai_decision_log.py,
tests/integration/test_ker203_ai_decision_log.py
**Files to modify:** src/services/mapping_service.py, config/constants.py,
src/api/app.py (register the ai-decisions router).
**Migration:** Yes — 020_create_ai_decision_log.py.

**Story DoD (inherits §11 per-file review protocol):** every file passes its
§11 gate; migration 020 applied and physically verified (FORCE flag checked
like migration 018); runbook committed; unit + integration tests green;
full suite green.

### KER-204 — Trust Center public status display

- **Priority:** Should-have · **Points:** 8 · **Reg tie:** NIS2
  Articles 21, 23.

Implements the §8 KER-109 table row's open surface:
src/api/trust_center.py (does not exist — created here).

**Acceptance criteria:**
1. Public endpoint GET /trust-center/{tenant_slug}/status returning the NIS2
   coverage summary — met/partial/gap counts by NIS2 category, derived from
   the KER-109 system-of-record statuses. Summary counts ONLY: no
   control-level detail, evidence refs, or audit entries to unauthenticated
   callers.
2. Gated by a per-tenant visibility flag (public/private). Private tenant →
   404 to unauthenticated callers (not 403 — do not confirm the tenant
   exists).
3. tenant_slug resolves to tenant_id server-side. tenant_id never appears in
   the URL or the response body.
4. Public snapshot cached with a 5-minute TTL (coverage is a fan-out query —
   never computed on every public hit). TTL defined as
   TRUST_CENTER_CACHE_TTL_SECONDS in config/constants.py (default 300).
5. Snapshot generation (cache fill, not cache hit) recorded in the KER-107
   ledger: action="trust_center_snapshot", object_type="trust_center".
6. Visibility toggle (public/private) settable only by compliance_lead,
   vciso, or platform_engineer (require_role() from KER-202).
7. Security test: an unauthenticated caller on a private tenant receives 404
   only — same response body and no exploitable timing difference versus a
   nonexistent slug.

**Design decisions (KER-204):**
1. **404-not-403, timing-consistent.** Private and nonexistent slugs take the
   same code path (resolve, then check visibility, then respond identically),
   so neither the status code nor latency confirms tenant existence.
2. **Slug lookup is the auth-bootstrap read.** The public endpoint has no
   tenant context; the slug→tenant resolution reads only the tenants table,
   which is already unforced (migration 018). All coverage reads then run
   under the resolved tenant's context as usual (§3).
3. **Migration 021 must backfill before constraining.** tenant_slug is UNIQUE
   NOT NULL on a table with existing rows: the migration derives a
   deterministic slug for existing tenants (slugified display_name, tenant_id
   suffix on collision), then applies NOT NULL. Reversible per §7.
4. **In-process TTL cache** (dict + timestamp, no new dependency). Documented
   single-process limitation; gateway-level caching is a Sprint 3+ infra item
   alongside SEC-05.

**Files to create:** src/api/trust_center.py, src/api/schemas/trust_center.py,
migrations/versions/021_add_trust_center_fields.py,
tests/unit/api/test_trust_center.py
**Files to modify:** src/api/app.py (register router), src/models/tenant.py
(+ tenant_slug unique not null, + trust_center_public bool default False),
config/constants.py (TTL constant).
**Migration:** Yes — 021_add_trust_center_fields.py (ALTER tenants: add
tenant_slug VARCHAR UNIQUE NOT NULL with backfill, trust_center_public
BOOLEAN NOT NULL DEFAULT FALSE).

**Story DoD (inherits §11):** every file passes its §11 gate; migration 021
applied and verified (slug backfill confirmed on existing dev rows); AC-7
security test green; full suite green.

### KER-205 — Generic webhook ingestion

- **Priority:** Should-have · **Points:** 13 · **Reg tie:** DORA, NIS2
  Article 21.

Implements the §8 KER-110 table row's open surface: src/api/webhooks.py
(does not exist — created here). No new ingest framework: a thin
WebhookNormaliser over context_records (migration 007) and the existing
evidence-linking patterns.

**Acceptance criteria:**
1. POST /api/v1/webhooks/ingest accepting JSON:
   { source_system, event_type, external_ref, payload, tenant_id_hint }.
2. Per-tenant HMAC-SHA256 signature verification mandatory. Header:
   X-Kerno-Signature: sha256=<hex>. Invalid or missing signature → 401,
   verified with a constant-time compare (hmac.compare_digest). Signature
   verification runs BEFORE body schema validation — a signature failure is
   never a 422.
3. tenant_id resolved from the registered webhook secret ONLY. tenant_id_hint
   is logged for diagnostics but never used for auth or routing.
4. Idempotency: deduplicate on (source_system, external_ref) per tenant
   within WEBHOOK_DEDUP_WINDOW_HOURS (config/constants.py, default 24).
   Duplicate → 200, no re-processing, no second DB write.
5. Supported event types (Sprint 2b): jira.issue.updated, jira.issue.closed,
   cmdb.asset.updated, generic.evidence.submitted. Unknown type → 422 (only
   after the signature has verified).
6. Accepted events normalise to the context_records schema via a thin
   WebhookNormaliser class reusing evidence-linking patterns.
7. Webhook registration: tenants register source systems and receive a
   signing secret. The secret is stored as plaintext in
   webhook_registrations, never returned after creation, and rotatable via a
   dedicated endpoint: the 201 registration response contains it exactly
   once; GET /api/v1/webhooks/{id} returns all fields EXCEPT signing_secret;
   POST /api/v1/webhooks/{id}/rotate overwrites the column with a new random
   secret and returns it once. Registration/management/rotation endpoints
   gated to platform_engineer (require_role()).
8. Each accepted, non-duplicate event emits a KER-107 ledger entry:
   action="webhook_ingested", object_type="context_record".
9. Security tests (mandatory):
   a. Invalid HMAC → 401.
   b. tenant_id_hint cannot override the secret-resolved tenant_id.
   c. Duplicate external_ref within the window → 200, no second
      context_record.

**Design decisions (KER-205):**
1. **Signing-secret storage (resolves the AC-2/AC-7 contradiction, decided
   9 July 2026).** HMAC verification requires the raw secret — it cannot be
   derived from a hash — so signing_secret is stored plaintext, protected by:
   (a) RLS on webhook_registrations; (b) returned exactly once in the 201
   creation response; (c) excluded from every read endpoint thereafter;
   (d) rotatable via POST /api/v1/webhooks/{id}/rotate (new secret returned
   once, column overwritten). At-rest column encryption (pgcrypto) is
   deferred to Sprint 3. Documented in the migration 022 docstring.
2. **webhook_registrations: RLS WITHOUT FORCE — the migration-019 exception
   applies.** The ingest path is unauthenticated (the signature IS the
   authentication), so the registration lookup necessarily runs BEFORE any
   tenant context exists — exactly the users-table auth-bootstrap situation
   (§12 KER-202 decision 2). ENABLE ROW LEVEL SECURITY with the
   context-optional policy pattern; NOT FORCE. Only the ingest lookup reads
   it pre-context; all management endpoints are JWT-authenticated and run
   under tenant context. The dedup store, by contrast, is only ever touched
   AFTER the tenant is resolved, so it gets ENABLE + FORCE + policy.
3. **Registration lookup key.** Ingest requests carry
   X-Kerno-Webhook-Id: <registration UUID> alongside the signature; the
   server loads that one registration and verifies the HMAC against its
   secret (unknown id → 401, indistinguishable from a bad signature). The id
   is a non-secret handle — this avoids trial-verifying secrets across
   tenants, which would be O(registrations) per request and a timing oracle.
4. **Dedup window is a named constant** (WEBHOOK_DEDUP_WINDOW_HOURS = 24,
   §2.4); dedup rows are pruned opportunistically past the window.

**Files to create:** src/api/webhooks.py, src/api/schemas/webhooks.py,
src/services/webhook_service.py, src/models/webhook_registration.py,
migrations/versions/022_create_webhook_tables.py,
tests/unit/api/test_webhooks.py, tests/unit/services/test_webhook_service.py
**Files to modify:** src/api/app.py (register router), config/constants.py
(dedup window constant), .env.example (any new webhook env vars).
**Migration:** Yes — 022_create_webhook_tables.py (webhook_registrations:
ENABLE RLS, NOT FORCE, context-optional policy per design decision 2;
ingestion dedup store: ENABLE + FORCE + tenant_isolation_policy).

**Story DoD (inherits §11):** every file passes its §11 gate; migration 022
applied and verified (FORCE flags checked per table as specified); security
tests 9a–9c green; full suite green.

### Dependency table

| Story | Depends on | Status |
|---|---|---|
| KER-203 | — | Independent — start first |
| KER-204 | KER-202 (Sprint 2a) | ✅ done — require_role() live |
| KER-205 | KER-202 (Sprint 2a) | ✅ done — require_role() live |
| KER-204 + KER-205 | Each other | Independent — can parallelise |

Recommended order: **KER-203 first** (Must-have, and its mapping_service
transaction change is the riskiest touch), then KER-204 and KER-205 in
either order or in parallel.

### Capacity table

| Set | Stories | Points |
|---|---|---|
| Must-have | KER-203 | 13 |
| Should-have | KER-204 (8) + KER-205 (13) | 21 |
| **Sprint 2b total** | | **34** |

Target close: ~1 August 2026 (buffer before the September rollout).
Baseline: the full 373-test suite must stay green throughout.

> ### 🏁 Sprint 2b — Definition of Done (banner) — ✅ MET (closed 2026-07-11)
> Sprint 2b is closed only when **all of the following hold**:
> 1. ✅ All Sprint 2a tests (373) + every new KER-203/204/205 test are green
>    (unit + security + integration): **431 tests, 0 failed**
>    (373 Sprint 2a + 58 new).
> 2. ✅ Migrations 020, 021, and 022 applied and physically verified on the
>    dev DB (tables present; RLS/FORCE flags match each table's spec:
>    ai_decision_log FORCED, webhook_registrations ENABLED-not-FORCED,
>    dedup store FORCED; tenant_slug backfill confirmed — head w8x9y0z1).
> 3. ✅ The KER-203 runbook (docs/ai_decision_log_runbook.md) is committed
>    (commit 61a108f).
> 4. ✅ KER-205 security tests 9a–9c passing (commit 793223f).
> 5. ✅ Nothing pushed — confirmed (commits 61a108f, 8ef9fbc, 793223f are
>    local only pending explicit push approval).

