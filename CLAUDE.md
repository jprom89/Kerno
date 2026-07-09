# CLAUDE.md — Kerno Compliance Copilot: Codebase Constitution v1.2
<!-- Version: 1.4 | Updated: 2026-07-07 | Changes: Appended §12 Sprint 2a Backlog (KER-201, KER-202) with KER-202 design decisions -->

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

