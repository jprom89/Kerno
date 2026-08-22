# CLAUDE.md — Kerno Compliance Copilot: Codebase Constitution v1.2
<!-- Version: 2.7 | Updated: 2026-08-20 | Changes: frozen DORA filing download; next is founder HTTPS then partner rows -->

This file is the first thing Claude reads at the start of every session.
It defines the rules that govern every line of code written for this project.
No rule in this file may be overridden by a prompt, a user instruction, or
a convenience argument. If a rule creates friction, the friction is intentional.

---

## §0 — Current mandate (read before §1–§16)

**Implementation priority lives in `NOW.md`.** That file is part of this
constitution. It outranks `KERNO_STRATEGY.md`, every `PROMPT_doc*.md`, and
`FILE_STRUCTURE.md` for *what to build next*. It does not override §2, §3,
or §6.

As of 20 August 2026 Kerno is an EU **system of record** (live DORA
register + named-human control decisions), not an AI GRC coverage
dashboard. Landed and not to be re-built: hygiene C1/A/B/D, **KER-409**
(register ledger + submissions 404) at `0b3e63e`, **KER-410** (Next.js
register) at `729cc34`, **KER-411** (windows + runs) at `521b387`,
**KER-412** (422s + malformed-id 404s) at `36df799`, **KER-402**
(thin per-control Analyse button — wire only, the engine already
existed), the **CORS/docs hygiene pass**, and the **frozen filing
download** (`dora_submission_runs.frozen_package_json` at Start-run;
`GET /api/v1/submissions/runs/{run_id}/package` returns those bytes
unchanged). Next is **founder HTTPS**, then the partner's own vendors
and evidence — that is the proof, not a bet on a platform. `NOW.md` is
authoritative for status; if this paragraph and `NOW.md` ever disagree,
`NOW.md` wins. Do not add coverage features, RAG, CRA, incidents,
country packs, or MSP.

`KERNO_STRATEGY.md` is a research memo, not a ship plan. Checkmarks in its
Part G are aspirational — those features are not built. Do not implement
from that document.

Demo and outreach language: use only the approved sentence in §15. Do not
describe a live RAG or learning loop. `generate_recommendation()` does not
call retrieval; `context_records.embedding` is never populated.

---

## §1 — Project Identity

**Product:** Kerno Compliance Copilot
**What it does:** Holds an EU operational-resilience **system of record** —
starting with a live DORA Register of Information and named-human decisions
on NIS2 controls, each tied to evidence, a reproducible score, and a
tamper-evident ledger. A hybrid engine (deterministic scorer + LLM prose)
helps a human update that record; it is not the product.

**Who uses it:** Compliance engineers, vCISOs, and fractional CTOs at
mid-market European technology companies.

**What it is not:** A fine-tuning system. Kerno never trains or modifies a
base LLM. It is also not, today, a retrieval-augmented or
embedding-personalised system. The retrieval/bias machinery exists and is
tested; it has no production caller. Do not describe it as live. Reserved
for KER-404 only after the register lives in the product UI and humans are
actually signing decisions.

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

Every file has exactly one home.

`FILE_STRUCTURE.md` is a historical map and is **known-stale** (recorded
during KER-406; reconciliation is backlog, not a gate). Do not block work
because a path is missing from it. The live tree under `src/`, `frontend/`,
`config/`, `migrations/`, and `tests/` is authoritative for where code lives.

When creating a **new top-level** directory, still update `FILE_STRUCTURE.md`
first. `NOW.md` is an approved top-level file (the §0 mandate).

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
| KER-108 | Jira side-panel integration | Yes | src/api/routers/panel.py, src/dashboard/js/panel.js, src/services/jira_client.py |
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
- KER-108 — ✅ Done. Implemented as src/api/routers/panel.py,
  src/api/schemas/panel.py, src/dashboard/panel.html, and
  src/dashboard/js/panel.js; the Jira API client is
  src/services/jira_client.py (KER-110). Path correction (v1.7): the file
  src/integrations/jira.py named by earlier drafts was never created and no
  src/integrations/ package exists — the table row above now lists the real
  files. Jira iframe token hand-off remains deferred. The old note that
  reviewer_role/reviewer_id were user-provided is resolved: both come from the
  verified per-user JWT since KER-202.
- KER-109 — ✅ Done. Coverage summary + drill-down. Override-wins resolution
  matrix. WCAG AA. Links to KER-108 panel per control. Implemented as
  src/services/coverage_service.py, src/api/routers/coverage.py,
  src/api/schemas/coverage.py, src/dashboard/coverage.html, and
  src/dashboard/js/coverage.js. Numbering note: the active sprint backlog
  labels the control-coverage dashboard KER-109; the table's KER-109 row
  ("Trust Center status display", src/api/trust_center.py) is the external
  Trust Center surface — ✅ Done in Sprint 2b (KER-204, commit 8ef9fbc).
- KER-110 — ✅ Done. Remediation routing: gap → Jira task with SLA due date and
  assignee. Closure → re_review_flagged_at. Both actions in KER-107 audit
  ledger. Migration 017 unapplied to dev DB — run alembic upgrade head before
  integration tests. Implemented as src/services/remediation_service.py,
  src/services/jira_client.py, and src/api/routers/remediation.py. Numbering
  note: the active sprint backlog labels remediation routing KER-110; the
  table's KER-110 row ("Webhook ingestion endpoint", src/api/webhooks.py) is
  the generic ingestion surface — ✅ Done in Sprint 2b (KER-205, commit 793223f).
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

### Live-database rule (added 18 July 2026 — lesson from KER-401)

**New paths that touch the database must have at least one live-DB integration
test before being considered Done, not just mocked unit tests.**

Why this is a rule and not advice: the rules-based scoring path passed its
full unit suite for months while it would have failed on first real use — a
raw dict passed as a JSON parameter and raw UUID objects that psycopg2 cannot
adapt. Mocks proved those functions "work". They had never once executed
against PostgreSQL. A spy connection tests the SQL you *wrote*; only a real
connection tests the SQL the driver can *run*.

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

---

## §14 — Sprint 3 Backlog

**Sprint goal:** Ship a dashboard MVP that a Compliance Lead or vCISO can use
to evaluate Kerno without touching Jira — in time for design partner sessions
before the September 2026 beta rollout.

### Stack decision (recorded 15 July 2026, product owner)

- **Frontend:** Next.js (React), TypeScript, Tailwind CSS, App Router — lives
  at `frontend/` in this mono-repo; deployed to Vercel. No UI component
  library dependency: build only what is needed.
- **Backend:** the existing FastAPI service, unchanged in architecture,
  deployed publicly over HTTPS before the first design partner session.
- **Auth:** JWT from the existing KER-202 login endpoint, held in an httpOnly
  cookie managed by a Next.js API route — never in localStorage, never
  readable by client-side JavaScript.
- **API communication:** Next.js → FastAPI over HTTPS; CORS restricted to the
  Vercel preview and production domains.
- **Frontend tests:** Jest + React Testing Library — a separate suite; the
  431-test backend baseline stays green throughout.

### Pre-sprint deployment note (action required — not a story)

The FastAPI backend must be reachable over HTTPS before design partners can
use the dashboard. This is a deployment task, not a code change. Options in
order of speed: (1) Railway or Render — connect the repo, set env vars,
~10 minutes, free tier sufficient for beta (recommended); (2) Fly.io — more
config, more control; (3) existing VPS — nginx + certbot. Must happen before
the first design partner session.

### Baseline (verified 15 July 2026, commit 8eabc93)

CLAUDE.md v1.7; migration head w8x9y0z1 (022 — webhook tables); 431 tests
passing, 0 failed; no frontend exists — this sprint creates it. Sprint 2 auth
infrastructure is live: per-user JWT (KER-202), require_role(), six seeded
RBAC roles, scrypt login. Endpoint verification against the draft (performed
before this backlog was written): POST /api/v1/auth/login exists;
GET /api/v1/auth/me does NOT (KER-301 creates it); the coverage surface is
GET /api/v1/coverage/summary and /coverage/controls (KER-109); NO
recommendations-list endpoint exists (KER-303 adds one); the export surface is
GET /api/v1/export/evidence-pack?control_family=… and it already returns
Content-Disposition: attachment; the internal coverage endpoint is UNCACHED —
no invalidation call exists or is needed, and PUT /api/v1/trust-center/
visibility is NOT a cache control (it flips public visibility — never call it
for refresh).

---

### KER-301 — Auth UI and session management

**Priority:** Must-have · **Points:** 5 · **Reg tie:** EU AI Act Article 14
(human oversight requires identified human actors — the UI must surface who
is logged in and what role they hold).

**Acceptance criteria:**
1. Next.js project initialised at `frontend/` (TypeScript, Tailwind CSS, App
   Router). No UI component library dependency.
2. Login page at `/login`: email + password form, POST to FastAPI
   `/api/v1/auth/login` via the Next.js API route `/api/auth/login`, which
   sets the returned JWT as an httpOnly cookie. The JWT never reaches
   localStorage or client-side JS.
3. All `/dashboard/*` routes are protected: unauthenticated requests redirect
   to `/login`.
4. Persistent session: the JWT is re-validated on each dashboard page load
   via `GET /api/v1/auth/me` (created in this story — returns `{ email, role }`
   decoded from the verified token; the endpoint does not exist today).
5. Logout: `/api/auth/logout` clears the httpOnly cookie and redirects to
   `/login`.
6. Nav header on every dashboard page: Kerno logo, logged-in user email,
   role badge, logout button.
7. FastAPI CORS: add `CORSMiddleware` (none exists today) reading a
   comma-separated `ALLOWED_ORIGINS` env var at startup; documented in
   `.env.example` with the Vercel preview + production domains.
8. Frontend unit tests (Jest/RTL): valid login → cookie set + redirect;
   logout → cookie cleared; protected route without cookie → redirect.

**Design decisions (KER-301):**
1. **Cookie is set by the Next.js API route, not the browser.** FastAPI
   returns the JWT in the response body (existing TokenResponse contract,
   unchanged); the Next.js route sets it as httpOnly/Secure/SameSite. The
   token never exists in client-readable storage.
2. **Role is decoded server-side** in Next.js middleware for route protection;
   the client receives only email + role as display strings via
   /api/v1/auth/me.
3. **GET /api/v1/auth/me is a new backend endpoint** (verified absent): it
   decodes the presented JWT with the existing dependency helpers and returns
   { email, role } — no database read (identity lives in the verified token,
   per the KER-202 users-table design note).
4. **KERNO_API_URL (server-side env var, never NEXT_PUBLIC_*)** is the only
   place the FastAPI base URL lives. The browser never calls FastAPI
   directly: all FastAPI calls go through Next.js route handlers and server
   components, which hold the httpOnly cookie. Documented in
   frontend/lib/api.ts and .env.example (decided 15 July 2026).

**Files to create:** `frontend/` (Next.js project scaffold),
`frontend/app/login/page.tsx`, `frontend/app/dashboard/layout.tsx` (auth
guard), `frontend/app/api/auth/login/route.ts`,
`frontend/app/api/auth/logout/route.ts`, `frontend/middleware.ts`,
`frontend/components/NavHeader.tsx`, frontend tests.
**Files to modify:** `src/api/app.py` (CORSMiddleware + ALLOWED_ORIGINS),
`src/api/routers/auth.py` (+ GET /me), `src/api/schemas/auth.py`
(+ MeResponse), `.env.example` (ALLOWED_ORIGINS),
`tests/unit/api/test_auth.py` (+ /me tests).
**Migration:** No.

**Story DoD (inherits §11 per-file review protocol, frontend files included):**
every file passes its §11 gate; backend suite green (431 + new /me tests);
Jest suite green; `next build` exits 0; cookie flags verified httpOnly in a
browser inspector.

---

### KER-302 — NIS2 coverage dashboard

**Priority:** Must-have · **Points:** 8 · **Reg tie:** NIS2 Articles 21, 23
(demonstrable security posture — internal view; the KER-204 Trust Center is
the public view).

**Acceptance criteria:**
1. Dashboard home at `/dashboard`: overall met/partial/gap counts plus a
   breakdown by NIS2 category, sourced from `GET /api/v1/coverage/summary`
   (KER-109 — verified to exist and to return exactly these counts per
   category; no new read logic needed).
2. Coverage breakdown as a visual category grid: each category card shows
   met (green) / partial (amber) / gap (red) counts with a percentage bar.
   WCAG AA contrast.
3. Last-recalculated timestamp shown on the dashboard. **Verified gap: no
   endpoint exposes this today.** Before writing any code, check where
   `POST /api/v1/scheduler/run-recalculation` writes a completion timestamp
   (look for an updated_at or completed_at column in the scheduler or
   retrieval_bias tables). Use that verified column; do not add a new column
   without confirming it does not already exist. Extend the
   `GET /api/v1/coverage/summary` response with a nullable
   `last_recalculated_at`. A `null` value renders as "Never calibrated" in
   the UI.
4. Manual recalculate button calls `POST /api/v1/scheduler/run-recalculation`
   (KER-201); shown only to `compliance_lead` and `vciso`; the response's
   status and fresh timestamp update the display.
5. Clicking a category card navigates to `/dashboard/controls?category=…` —
   the control list from `GET /api/v1/coverage/controls?category=…` with
   met/partial/gap status badges per control.
6. Auditor sees the dashboard read-only (no recalculate button); all other
   roles see the full view. UI gating only — the backend endpoint keeps its
   existing auth semantics.
7. Responsive at 1280px+ desktop.

**Design decisions (KER-302):**
1. **No cache layer in the frontend and none needed in the backend** — the
   KER-109 coverage endpoint is computed live per request (verified); after
   any state-changing action the dashboard simply re-fetches.
2. **`frontend/lib/api.ts` is the single typed fetch wrapper** for all FastAPI
   calls (auth header from the httpOnly cookie via Next.js route handlers /
   server components); every later story imports it rather than calling
   `fetch` directly.

**Files to create:** `frontend/app/dashboard/page.tsx`,
`frontend/app/dashboard/controls/page.tsx`,
`frontend/components/CoverageGrid.tsx`, `frontend/components/ControlList.tsx`,
`frontend/lib/api.ts`.
**Files to modify:** `src/api/schemas/coverage.py` and
`src/services/coverage_service.py` (+ `last_recalculated_at` in the summary —
after verifying the source column per AC-3),
`tests/unit/api/test_coverage.py`,
`tests/unit/services/test_coverage_service.py`.
**Migration:** No.

**Story DoD (inherits §11):** every file passes its §11 gate; backend suite
green; Jest suite green; category grid verified against seeded dev data;
`last_recalculated_at` source column confirmed in a code comment before use.

---

### KER-303 — Recommendation review UI

**Priority:** Must-have · **Points:** 8 · **Reg tie:** EU AI Act Article 14
(human oversight — this UI is the human-in-the-loop surface).

**Acceptance criteria:**
1. Page at `/dashboard/recommendations`: paginated list of open recommendations
   showing control_id, status, confidence (percentage + colour badge),
   evidence count, generated_at. **Verified gap: no list endpoint exists** —
   this story adds read-only `GET /api/v1/recommendations` (JWT-scoped,
   paginated; `page`/`page_size` query params) over the existing
   recommendations table. "Open" is defined by the following exact predicate
   (corrected 15 July 2026 — overrides link to controls via
   original_control_id; there is NO overrides.recommendation_id column, and
   `IN (SELECT recommendation_id FROM overrides)` must not appear anywhere):

     is_superseded = FALSE
     AND NOT EXISTS (
         SELECT 1 FROM overrides o
         WHERE o.original_control_id = recommendations.control_id
         AND o.created_at > recommendations.generated_at
     )

   The created_at > generated_at guard is required — an override predating
   the recommendation does not close it. Note: because map_control supersedes
   prior rows on every regeneration, is_superseded = FALSE yields at most one
   open row per control.

2. Each row has three actions mapping **exactly** onto the KER-106 backend
   vocabulary (decided 15 July 2026 — there is no `override` or `dismiss`
   action; `edit` and `reject` REQUIRE `corrected_control_id`; a request with
   any other action_type value will 422):
   - **Approve** button → action_type="approve", submits immediately, no form.
   - **Edit** button → action_type="edit", opens the inline form (AC-3).
   - **Reject** button → action_type="reject", opens the same inline form.

3. The shared Edit/Reject inline form: `justification` text (required,
   pre-filled with the recommendation's `rationale`) AND a required
   `corrected_control_id` chosen from a searchable dropdown populated via
   `GET /api/v1/coverage/controls` (verified — no `/api/v1/controls` route
   exists; the KER-109 endpoint provides control_id/ref/title). The form may
   not be submitted without both fields.

4. Approve requires no justification and no corrected control.

5. After any action: row removed from the open list, success toast, coverage
   re-fetched on next dashboard view. **NO invalidation call** — the coverage
   endpoint is uncached (verified), and `PUT /api/v1/trust-center/visibility`
   must **never** be used as a refresh mechanism (it changes public visibility,
   not internal state).

6. Filtering: confidence band (all/high/medium/low) and NIS2 category —
   client-side on the fetched page.

7. Auditor role: action buttons hidden; read-only list. (The backend already
   enforces this: auditor POSTs to /overrides get 403 via
   OVERRIDE_CAPABLE_ROLES.)

8. Empty state: clear message when no open recommendations exist.

**Design decisions (KER-303):**
1. **Confidence badge colours key off the server's `confidence_level` field**
   (high/medium/low, derived from HIGH_/MEDIUM_CONFIDENCE_THRESHOLD in
   config/constants.py), never off frontend-hardcoded cutoffs — one source
   of truth, no drift.
2. **The new list endpoint is read-only and thin**: router + schema + a
   `list_open_recommendations()` read in `recommendation_service`; "open"
   uses the exact corrected predicate in AC-1 (control + time join).
   No writes, no migration.

**Files to create:** `frontend/app/dashboard/recommendations/page.tsx`,
`frontend/components/RecommendationList.tsx`,
`frontend/components/OverrideForm.tsx`, `frontend/components/Toast.tsx`,
`src/api/routers/recommendations.py`, `src/api/schemas/recommendations.py`,
`tests/unit/api/test_recommendations.py`.
**Files to modify:** `src/services/recommendation_service.py` (+ list read),
`src/api/app.py` (register router),
`tests/unit/services/test_recommendation_service.py`.
**Migration:** No.

**Story DoD (inherits §11):** every file passes its §11 gate; backend suite
green including the new list-endpoint tests; Jest suite green; the three
action mappings verified against a live backend (approve → 201; edit/reject
→ 422 without corrected_control_id, 201 with).

---

### KER-304 — Evidence pack export button

**Priority:** Should-have · **Points:** 3 · **Reg tie:** NIS2 Article 23
(evidence for competent authority reporting).

**Acceptance criteria:**
1. Export button on `/dashboard` and on each category detail page
   (`/dashboard/controls?category=…`).
2. Calls the existing KER-111 endpoint at its verified path:
   `GET /api/v1/export/evidence-pack?control_family=…` (the draft path
   /api/v1/evidence-pack/export does not exist). The page passes the NIS2
   category value as `control_family` — **verify the category/family
   vocabulary match** against compliance_controls during implementation before
   wiring the query parameter.
3. Browser receives a file download. Verified: the endpoint already returns
   Content-Disposition: attachment with a safe filename — no backend change
   required; the frontend streams the response through a same-origin route
   handler or uses an authenticated fetch + blob anchor.
4. Loading spinner during export; button disabled while in progress.
5. Export is tenant-scoped server-side via the JWT — no tenant_id anywhere
   in the request.
6. UI role-gating: visible to compliance_lead, vciso, security_engineer,
   platform_engineer; hidden for auditor and end_customer_admin. **Recorded
   honestly: this is UX-layer gating only** — the backend endpoint currently
   accepts any authenticated role (tenant-scoped + rate-limited); adding
   require_role() server-side is a Sprint 4 decision, not assumed here.

**Files to create:** `frontend/components/ExportButton.tsx`.
**Files to modify:** `frontend/app/dashboard/page.tsx`,
`frontend/app/dashboard/controls/page.tsx`.
**Migration:** No.

**Story DoD (inherits §11):** every file passes its §11 gate; a real export
downloaded from the dev backend for at least one control family; suites green.

---

### Dependency table

| Story       | Depends on          | Notes                                      |
|-------------|---------------------|--------------------------------------------|
| KER-301     | —                   | Must land first — all other stories need auth |
| KER-302     | KER-301             | Needs auth guard + api.ts wrapper          |
| KER-303     | KER-301             | Needs auth guard + api.ts wrapper          |
| KER-304     | KER-302             | Needs dashboard pages to attach the button |
| KER-302 + KER-303 | Each other    | Independent after KER-301                  |

Recommended order: KER-301 → KER-302 → KER-303 → KER-304 (302 before 303
so api.ts and the controls data shapes exist before the heavier review UI).

### Capacity table

| Set           | Stories                                   | Points |
|---------------|-------------------------------------------|--------|
| Must-have     | KER-301 (5) + KER-302 (8) + KER-303 (8)  | 21     |
| Should-have   | KER-304 (3)                               | 3      |
| Sprint 3 total |                                          | 24     |

Target close: ~1 August 2026. Backend baseline: the 431-test suite must stay
green throughout (plus the new KER-301/302/303 backend tests); the frontend
Jest suite is separate.

> ### 🏁 Sprint 3 — Definition of Done (banner)
> Sprint 3 is closed only when **all of the following hold**:
> 1. **Backend:** all 431 baseline tests plus the new KER-301 (/me), KER-302
>    (summary timestamp), and KER-303 (recommendations list) tests are green,
>    0 failed.
> 2. **Frontend:** the Jest suite is green, including the KER-301 auth-flow
>    tests (login sets cookie, logout clears it, protected-route redirect).
> 3. `next build` exits 0 — no type errors, no lint errors.
> 4. **CORS:** FastAPI allows the Vercel preview and production domains via
>    ALLOWED_ORIGINS.
> 5. **Deployment note actioned:** the FastAPI backend is reachable over HTTPS.


---

## §15 — Post-Diligence Roadmap (recorded 16 July 2026)

**Context:** technical due diligence (15 July 2026) found that neither
recommendation engine had a production caller, that the KER-201 feedback loop
terminates in an unconsumed ranking, and that LOW_CONFIDENCE_THRESHOLD (0.5)
contradicted MEDIUM_CONFIDENCE_THRESHOLD (0.40). This section is the approved
response. Sprint goal: make the recommendation engine real — reachable in
production, coherent in its thresholds, and honest to the demo claim:
"every recommendation shows its confidence level, cites the exact evidence it
relied on, and is never final until a named human approves, edits, or rejects
it, with that decision permanently logged."

### KER-401 — Production trigger + hybrid recommendation engine

- **Priority:** Must-have · **Points:** 7 · **Reg tie:** EU AI Act Article 14
  (human-initiated analysis, human-gated outcome); Articles 12/19 (decision
  retention via KER-203 on the new path).

**Engine decision (approved 16 July 2026):** hybrid (c) built on
generate_recommendation's chassis — the deterministic evidence scorer produces
status/confidence/citations (provable by construction: evidence_ids IS the
list the mean was computed over), and the LLM is confined to writing the
rationale PROSE explaining a score it cannot change. map_control's
LLM-decides-everything path stays intact but UNWIRED — reserved, documented,
not deprecated. Chassis choice is forced by types: map_control's EvidenceInput
has no relevance_score, so it cannot feed the scorer.

**Acceptance criteria:**
1. POST /api/v1/recommendations/generate, body { control_id }. JWT tenant +
   require_role(GENERATE_CAPABLE_ROLES = compliance_lead, vciso,
   security_engineer). Rate limit 10/minute (SEC-05 pattern — each call may
   invoke the LLM). Unknown control_id → 404 (EntryNotFoundError). 201 with
   the persisted recommendation, including rationale_source.
2. Status, confidence_score, confidence_level, and evidence_ids come ONLY
   from the deterministic scorer (_score_evidence). The LLM cannot alter them.
3. The LLM writes the rationale text from (control meta, evidence records,
   scoring result). On ANY LLM failure — missing key, network, bad JSON — the
   existing template rationale is used instead. Prose is not the decision, so
   this fallback cannot poison scores. The snapshot records
   rationale_source: "llm" | "template".
4. The SAME single LLM call also returns the model's independent opinion of
   status and confidence, stored in the snapshot as llm_opinion (never
   persisted to the scored columns) — free engine-agreement data for KER-403.
   A mapping's snapshot MUST record this opinion alongside the deterministic
   score (approved addition, 16 July 2026 — cheap now, expensive to retrofit).
5. KER-203 invariant extended to the new path: every generation emits exactly
   one ai_decision_log row in the same transaction as the recommendation
   write (commit/rollback together), with input_snapshot_hash = SHA-256 of
   the canonical snapshot JSON and model_version identifying both the scoring
   engine (SCORING_ENGINE_VERSION) and the rationale source. Proven by a
   live-DB integration test — spies are not sufficient for this AC.
6. Each generation appends a KER-107 ledger entry
   (action="recommendation_generated") attributing the triggering user's
   verified JWT identity (actor_id = user_id), in the same transaction.
7. LOW_CONFIDENCE_THRESHOLD is DELETED. requires_human_review :=
   (confidence_level == CONFIDENCE_LOW) in both engines — one definition of
   "needs a human". HIGH (0.75) and MEDIUM (0.40) unchanged: swapping
   arbitrary numbers is not calibration (KER-403 earns that right).
   Known behavioural delta: mappings with confidence in [0.40, 0.50) are no
   longer review-flagged. Note: §14 KER-303's "80/50" badge prose never
   matched the code; the frontend keys off the server's confidence_level, so
   no frontend change.
8. The LLM rationale prompt gets one round of real-output iteration against
   seeded evidence BEFORE any design partner sees output (approved risk
   mitigation — the first LLM output must not be the demo).

**Deferred by decision (16 July 2026), named future hooks:**
- Nightly batch + link-creation triggers (reuse KER-201 cron plumbing;
  remediation's re_review_flagged_at — currently written by Jira closures and
  consumed by NOTHING — becomes a batch predicate then. Do not wire it now.)
- KER-402 — dashboard "Analyse" button (frontend proxy + wiring, ~2 pts).
- KER-403 — calibration measurement, REPORT-ONLY (~3 pts): override-rate per
  confidence band per tenant; no auto-adjustment below ≥50 human-reviewed
  recommendations per band per tenant.
  **Known methodological flaw to fix in KER-403 (recorded 20 July 2026):** the
  snapshot's llm_opinion is NOT an independent second opinion. The rationale
  prompt shows the model the deterministic verdict before asking for its own
  status/confidence, so llm_opinion is anchored to the scorer. First real runs
  confirmed this — the model echoed the deterministic score closely (0.85→0.9,
  0.2→0.2, 0.5→0.6). Do NOT treat llm_opinion as independent corroboration in
  any calibration metric until the prompt is changed to withhold the verdict
  when soliciting the opinion (e.g. a separate opinion-first call, or a single
  call that asks for the opinion before revealing the score). Until then it
  measures agreement-with-anchor, not engine agreement.
- KER-404 — retrieval-augmented correction memory (~8 pts): inject similar
  past human corrections into generation via the (already built, tested)
  biased retrieval. Gated on weeks of real design-partner override volume.
- KER-405 — decision-provenance hardening (~6–8 pts, recorded 22 July 2026):
  found by auditing the pitch sentence "prove to an auditor exactly how every
  decision was made" against the live schema. Four named gaps, in priority
  order:
  1. **Relevance scores have zero provenance** (the structural one — the score
     IS the verdict): link/score creation and mutation must write KER-107
     ledger entries with verified JWT identity, and control_evidence_links
     needs change-history or immutability. Today linked_by is a free string,
     unledgered, silently editable.
  2. ai_decision_log has no append-only trigger (audit_log has two) — retained
     by convention, not enforced. Migration adds the same trigger pair.
  3. justification_text is UI-required but server-optional for edit/reject —
     enforce in _validate_override_input so "why" is a guarantee, not a habit.
  4. The recommendation snapshot attests evidence identity (id/title/score)
     but not content — add each record's content_hash so "what the evidence
     said at decision time" is provable.
  5. **Link provenance is inherited, never independent** (found 28 July 2026
     while scoping intake): control_evidence_links has NO tenant_id column —
     its RLS policy isolates indirectly, via a subquery to the record's
     tenant_id. A link therefore can never be more trustworthy than the
     context_record it hangs off, which caps how strong gap #1's provenance
     can be made without also hardening the record. Logged, not acted on.
  **HOLD — do not build (decided 22 July 2026):** gated on validation with a
  real compliance lead. We have not confirmed that "prove exactly how every
  decision was made" is the claim buyers care about, versus the narrower
  corrected sentence already being sufficient — building provenance-on-
  provenance before that validation repeats the KER-401 pattern (rigor on an
  unconfirmed claim). Three possible outcomes from this week's conversations —
  listen for all three, and do not collapse the third into "no signal":
  1. Evidence-score provenance is what compliance leads/auditors actually
     probe → KER-405 is the immediate next build.
  2. They care about speed/cost/integration with existing tools instead →
     reprioritise toward automated evidence linking (the EuroComply gap).
  3. **Indifference to both** — "I don't think about compliance tooling this
     way at all" / "my current process works fine" → NOT a build prompt of any
     kind; it is the wrong-buyer-persona signal, a different pivot class than
     choosing between 1 and 2. Record it as its own finding if it occurs.

**Files to create:** src/api (generate endpoint pieces in the existing
recommendations router/schemas), tests/integration/test_ker401_generation.py.
**Files to modify:** config/constants.py (delete LOW_CONFIDENCE_THRESHOLD,
add SCORING_ENGINE_VERSION), src/services/recommendation_service.py (hybrid
core + GENERATE_CAPABLE_ROLES + decision-log/ledger emission),
src/services/mapping_service.py (requires_human_review unification; reserved
note), src/services/ai_decision_log_service.py (public hash_snapshot — single
home for canonical snapshot hashing), src/api/schemas/recommendations.py,
src/api/routers/recommendations.py, affected tests.
**Migration:** No.

**Story DoD (inherits §11):** every file passes its §11 gate; full backend
suite green; live-DB integration test proves the same-transaction decision-log
invariant on the new path (commit AND rollback directions); prompt iterated
once against seeded evidence with real output (or explicitly blocked on a
valid MISTRAL_API_KEY and flagged); nothing committed or pushed without
explicit approval.

### Approved demo claim (recorded 22 July 2026)

The demo and any deck must use THIS sentence — verified true against the live
schema, word by word:

> "Every recommendation and every human decision made in Kerno is traceable
> to named evidence, a reproducible score, a named human, and a timestamp —
> with tamper-evident, database-enforced logging of every human decision."

The stronger form ("prove to an auditor exactly how every compliance decision
was made") is NOT approved: it overclaims at four verified points (see
KER-405) and must not be used unless KER-405 ships. The strongest true things
today: evidence citation is provable by construction (evidence_ids IS the
scored list), human decisions are hash-chained with DB-enforced append-only
triggers, and every generation is retained with a re-derivable input hash.

### Pre-demo actions (named tasks — need owners, not stories)

1. **Curate evidence-link relevance scores for the design-partner demo
   tenant.** Owner: product owner (or delegate). Must happen BEFORE any
   partner sees KER-401 output: with uncurated links the deterministic scorer
   truthfully emits a uniform wall of 0.5/"partial/medium", undercutting the
   exact claim being demonstrated. (Recorded 16 July 2026.)
2. **Valid MISTRAL_API_KEY in the demo environment** — required for LLM
   rationale prose (the engine degrades safely to template text without it,
   but a partner demo should show the real prose). Also required for AC-8's
   prompt iteration.
3. Carried from §14, still open: backend HTTPS deployment; ALLOWED_ORIGINS
   real Vercel domains.

---

## §16 — Evidence Intake (recorded 28 July 2026)

**Context:** scoping found the product had **no usable data intake**. Verified in
code: no upload/import endpoint of any kind; the only production writer of
context_records was the webhook ingest; `link_evidence()` existed with ZERO
callers; and the ingest passed `control_id=None`, so ingested evidence was an
orphan by design. A customer who bought and wired webhooks perfectly would
accumulate documents, create zero links, and score every control "gap — no
evidence". The working demo existed only because evidence links were hand-seeded
by a script with direct database access.

**Prerequisite, already shipped:** the orphan fix (commit 7e6fc3c) — webhook
ingest accepts an optional `control_ref` and links on arrival.

**Correction to an earlier claim (recorded here so it is not repeated):** the
pgvector auto-suggest foundation is thinner than previously stated. The
retrieval queries exist, but there is **no embedding-generation service in
src/** and **nothing populates `context_records.embedding`**. Auto-suggest
therefore requires an external embedding API integration built from scratch —
not "connecting a wire".

### KER-406 — Evidence intake, backend

- **Priority:** Must-have · **Points:** 6 · **Reg tie:** NIS2 Art. 21 (the
  evidence base underpinning every control assessment).

**Acceptance criteria:**
1. `POST /api/v1/evidence` (multipart) — one file per request. Extracts text,
   creates a context_records row: `source_system='upload'`, caller-supplied
   `record_type`, `external_id` = truncated filename (VARCHAR(255)),
   `title` = caller-supplied or filename, `body` = extracted text,
   `content_hash` = SHA-256 of the extracted text (fits VARCHAR(64) exactly).
2. Duplicate upload (same content_hash, same tenant) returns the EXISTING
   record rather than creating a twin.
3. `GET /api/v1/evidence?linked=false` — lists the tenant's records with a
   link-status filter. This is what makes webhook-ingested orphans visible and
   actionable.
4. **(REVISED — see design decision 8)** `POST /api/v1/evidence/{record_id}/links`
   with `{control_id, relevance_score, note}`. Both ids are **pre-validated**
   before `link_evidence()` is called: a tenant-scoped SELECT for the record,
   a catalogue SELECT for the control. Either lookup empty → an IDENTICAL 404.
   Score outside [RELEVANCE_SCORE_MIN, RELEVANCE_SCORE_MAX] → 422.
5. `DELETE /api/v1/evidence/{record_id}/links/{control_id}` — sets
   `removed_at` (soft), never a hard delete, preserving link history.
6. `linked_by` is populated from the **verified JWT user_id**, never a free
   string (approved decision 5; a narrow data-integrity fix that does NOT
   un-hold KER-405).
7. Every write emits a KER-107 ledger entry. Upload and link are gated to
   `compliance_lead, vciso, security_engineer`; auditor is read-only;
   platform_engineer is deliberately excluded (connector/webhook permissions
   are a separate concern — do not conflate).

**Approved design decisions (KER-406):**
1. **PDF supported in the MVP**, with `pypdf` DECLARED in pyproject — it is
   currently only transitive, which violates SEC-06 reproducibility. Real
   compliance evidence is overwhelmingly PDF; a text-only MVP would be close
   to useless.
2. **Original files are NOT retained.** No blob storage exists; we store
   extracted text only. An auditor asking for the original signed PDF gets
   text. This is the same class of gap as KER-405 finding #4 and stays on hold
   with it — accepted for MVP, not solved.
3. **CSV is file-as-document** (one upload = one evidence record). Row-wise
   import (one row = one record, e.g. an asset register) is explicitly OUT OF
   SCOPE — it needs column mapping and is its own future story.
4. **Embedding is skipped at upload**; `context_records.embedding` stays NULL.
   Building an embedding service now would bolt an unrelated, riskier system
   (external API, rate limits, retry handling) onto a story whose job is
   making evidence linkable at all. The column is already nullable, so a
   future backfill walks `WHERE embedding IS NULL` at zero migration cost.
5. **Idempotency is DB-enforced**: `uq_control_evidence_links_pair`
   UNIQUE (control_id, record_id) — verified empirically to collide — so
   `link_evidence()`'s upsert is necessary, not defensive.
6. **`link_evidence()` is reused as-is.** It already validates the relevance
   score, sets tenant context, and upserts correctly. KER-406 gives it an HTTP
   surface; it is not rewritten.
7. **File placement** (§4): `src/api/routers/evidence.py`,
   `src/api/schemas/evidence.py`, text extraction in
   `src/services/evidence_intake.py`. `evidence_service.py` remains the
   existing link/read layer. FILE_STRUCTURE.md line 56 is stale — it names
   `services/evidence.py` for the exporter, which is really
   `services/export_service.py`; correct it in this story.
8. **Pre-validate ids; never remap driver exceptions to 404.** Verified
   empirically: a nonexistent control_id raises `ForeignKeyViolation`, but a
   nonexistent record_id raises `InsufficientPrivilege` — because
   control_evidence_links has NO tenant_id column and its RLS policy isolates
   via a subquery to the record's tenant. A cross-tenant record raises the
   SAME error as a nonexistent one, which is the correct no-existence-oracle
   behaviour and must be preserved. So: pre-validate, return an identical 404
   for either miss, and treat any driver exception reaching link_evidence()
   afterwards as a genuine 500 (a real bug or a TOCTOU race — backstopped by
   the unique constraint and FKs, so the worst case is a 500, never bad data).
   The policy's WITH CHECK is None, so pre-validation does real work on the
   insert path rather than decorating it.

**Files to create:** src/api/routers/evidence.py, src/api/schemas/evidence.py,
src/services/evidence_intake.py, tests/unit/api/test_evidence.py,
tests/unit/services/test_evidence_intake.py,
tests/integration/test_ker406_evidence_intake.py
**Files to modify:** src/api/app.py (register router), pyproject.toml (declare
pypdf), FILE_STRUCTURE.md (add the new files; fix the line-56 exporter name).
**Migration:** No — every table and constraint already exists.

### KER-407 — Evidence UI

- **Priority:** Must-have · **Points:** 4. A `/dashboard/evidence` page:
  drag-drop upload, record list with a linked/unlinked filter, and a link
  action using a searchable control picker plus a relevance-score input
  (reusing the KER-303 picker pattern).

### Non-goals for this build (explicit)

Auto-suggested control links. Embedding generation. Bulk row-wise CSV import.
Original-file storage. Any change to the scoring engine.

### Backlog item — FILE_STRUCTURE.md reconciliation (~1 pt, not started)

Recorded 28 July 2026 while shipping KER-406. FILE_STRUCTURE.md is stale well
beyond the single line corrected in that commit: it described an `api/routes/`
directory that does not exist (the real one is `api/routers/`) and listed
`embedding_service.py`, which has NEVER been built — no embedding-generation
service exists and `context_records.embedding` is never populated. Worth
noting as a cause, not just a symptom: a structure document asserting a
foundation that was never laid is part of why the intake gap went unnoticed
for this long. KER-406 annotated the specific entries rather than rewriting
the file; a full reconciliation against the real tree is its own small task.

### Sequencing

Orphan fix (shipped, 7e6fc3c) → KER-406 backend → KER-407 UI. This runs AHEAD
of the outstanding Sprint 3 HTTPS/domain work: intake determines whether the
product is usable at all, which outranks the logistical gate for partner
sessions. KER-405 and the PDF evidence-pack export both stay on hold.

**Story DoD (inherits §11):** every file passes its §11 gate; the full backend
suite stays green; and **every new database path — upload, dedupe-on-
content_hash, link, soft-delete-via-removed_at — has a live-DB integration
test before being marked Done**, not merely mocked unit tests.

---

## §17 — Security Audit Response (recorded 13 August 2026)

A nine-section security audit produced the work below. Tickets A–D are the
approved response; everything under "Recorded, not scoped" is deliberately
parked so it is not lost, not because it has been assessed and cleared.

### Ticket status

| Ticket | Scope | Status |
|---|---|---|
| C1 | Require the organisation at login — tenant collision (KER-408) | ✅ done, commit ed3f3f2 |
| A | require_role() on six ungated mutating/sensitive routes | ✅ done, commit 7b6738b |
| B | Lock down the legacy dashboard and OpenAPI docs outside dev | ✅ done |
| D | Server-side justification_text enforcement + ai_decision_log append-only triggers | ✅ done |
| C2 | Non-owner DB role + FORCE RLS on users/webhook_registrations | **held — its own PR, needs a real DB role** |

### Ticket A — authorisation matrix (approved)

| Route | Roles |
|---|---|
| POST /api/v1/scheduler/run-recalculation | compliance_lead, vciso |
| GET /api/v1/export/evidence-pack | compliance_lead, vciso, security_engineer, platform_engineer |
| POST /api/v1/remediation/trigger | platform_engineer |
| POST /api/v1/remediation/close-callback | platform_engineer |
| POST + PATCH /api/v1/register/entries | compliance_lead, vciso |
| POST /api/v1/submissions/runs | compliance_lead, vciso |

Each allow-list is a named constant beside its own service rather than a shared
central list: the routes share membership today but not authority, and a single
list would make one role change silently move several gates. The matrix is
restated as literal strings in tests/unit/api/test_rbac_gates.py so that editing
a constant fails a test instead of quietly redefining policy.

Two mutating routes are ungated by design and named as such in that test file:
`POST /api/v1/auth/login` runs before authentication, and
`POST /api/v1/webhooks/ingest` is authenticated by its HMAC signature. A
structural sweep fails on any other ungated mutating route, which is what makes
this a standing guarantee rather than a one-time cleanup.

### Backlog — remediation close-callback should not be RBAC-gated

`POST /api/v1/remediation/close-callback` is a machine-to-machine endpoint that
Jira calls when a remediation ticket closes. Ticket A gates it on
platform_engineer, which matches how it authenticates today (a human's JWT) and
is strictly better than leaving it open to every authenticated role — but it is
the wrong control for the shape of the caller. The right design is a per-tenant
HMAC signature exactly like the KER-205 webhook ingest: the signature is the
credential, no human token is involved, and no operator has to hold a role for
an automated callback to work. Redesigning it is its own scoping pass, because
it changes how the Jira side is configured, not just what the server checks.

### Ticket B — development-only surfaces (delivered)

Three surfaces now exist only when `KERNO_ENV` is exactly `development`:
the legacy static dashboard at `/dashboard/`, the interactive docs at `/docs`
and `/redoc`, and the raw schema at `/openapi.json`. `GET /` no longer
redirects into the legacy dashboard; it redirects to `FRONTEND_URL`, falling
back to the first `ALLOWED_ORIGINS` entry.

**`openapi_url=None` is required, not optional.** Nulling `docs_url` and
`redoc_url` alone removes the two HTML viewers and leaves the schema fully
served — verified empirically against the installed FastAPI, where
`FastAPI(docs_url=None, redoc_url=None)` still answers `/openapi.json` with
200. Anyone can point their own Swagger UI at a raw schema, so that would have
been a cosmetic fix. All three must be passed as constructor arguments;
assigning the attributes after construction is a silent no-op because FastAPI
registers the routes at the end of `__init__`.

What the schema was actually publishing anonymously — 28 routes and 45 schemas
on the live dev server, but the volume is not the point. It labelled precisely
which five operations require no bearer token (the anonymous target list), and
because FastAPI promotes endpoint docstrings into the `description` field it
also published the webhook HMAC scheme, the fact that a signing secret appears
exactly once in the 201 response, the trust-center role allow-list by name, and
the 401-not-422 ordering guarantee. That is an architecture-and-controls
document for a compliance product.

**Root redirect, and why it is shaped this way.** The target is read from the
environment only. It is never derived from a query parameter or the Host
header, because that is exactly what would turn it into an open redirect — the
prohibition is written into `root()`'s docstring, since the tempting future
"improvement" (preserve the deep link the user wanted) is the thing that breaks
it. Three failure modes are handled explicitly, each verified rather than
assumed:
- An empty target is an infinite loop, not a no-op: `RedirectResponse(url="")`
  emits an empty `Location`, which resolves to the request URI itself. A bare
  `FRONTEND_URL=` line yields `""` from dotenv, so a presence check is wrong
  and only a truthiness check is safe.
- A protocol-relative value (`//evil.example`) survives Starlette's quoting
  verbatim and redirects off-origin while looking relative. The guard is an
  explicit `http://`/`https://` prefix check; "is it non-empty" catches neither
  this nor a scheme-less typo.
- With nothing configured, `_allowed_origins()[0]` would raise `IndexError`,
  and Starlette re-raises after sending the 500 — a traceback per anonymous hit
  on the front door. The real `.env` sets no `ALLOWED_ORIGINS`, so that was the
  live state. The root now returns a minimal JSON service descriptor instead.

302 explicitly, never 301/308: operators will get `FRONTEND_URL` wrong at least
once, and a permanent redirect to a wrong target is browser-cached and cannot
be corrected server-side.

**Accepted cost, stated plainly:** the legacy dashboard is the only UI for the
DORA register and submission windows, and it is also where the KER-108 Jira
side-panel surface lives. Outside development those have no UI at all. The
routers stay live and RBAC-gated; rebuilding the UI was explicitly out of
scope. Confirmed with the product owner that no active or near-term
conversation needs them outside dev.

### Ticket D — decisions ratified before implementation (13–14 August 2026)

Both halves of Ticket D required a decision that the written spec did not
supply. Recording them here rather than in a migration docstring, because a
docstring is where a decision gets *described*, not where it gets *made*.

**D(i) — justification is required to overturn, not to agree.** Enforced in
`_validate_override_input`, for `edit` and `reject` only, on
`not (justification_text or "").strip()`. Three points that were not obvious:

- The bar is whitespace, not null. The server previously accepted `None`, `""`
  and `"   "` identically, and a blank string is exactly as useless to an
  auditor as a missing one. The stored value is now the stripped value, so what
  is kept is what validation judged.
- The check sits *after* the `corrected_control_id` check, deliberately. An
  existing test submits an edit with both fields missing and asserts on the
  `corrected_control_id` message; putting justification first would silently
  change which error a caller sees.
- Enforced in the service, not the Pydantic schema. The rule is conditional on
  `action_type`, so a schema version would be cross-field validation living in
  the transport layer, split from its identical sibling rule. It would also
  return `detail` as a list of dicts rather than a string, which every existing
  422 test and the frontend's error toast both assume is a string.
- `approve` stays exempt (§14 KER-303 AC-4). **Named honestly:** "a named human
  approved this and gave no reason" is a real gap against the §15 claim's
  spirit, and closing it is a frontend story, not a server one-liner.
- Also worth naming: the dashboard pre-fills the justification box with the
  AI's own rationale, so a reviewer can satisfy this rule with the machine's
  words unedited. Nothing server-side can detect that. The honest claim is "a
  non-empty justification is now a guarantee", not "the reasoning is".

**D(ii) — "append-only" for `ai_decision_log` means append-only except the
retention prune.** §15 KER-405 #2 asks for "the same trigger pair" as
`audit_log`. That is a contradiction and was not implemented literally:
`audit_log` blocks every DELETE because the human ledger is kept forever, while
§13 KER-203 AC-4 requires a nightly job that deletes rows past 180 days. Copied
verbatim, the prune would raise for every tenant on every run.

The ratified shape:

| Operation | Guard |
|---|---|
| INSERT | Allowed — and `created_at` is stamped server-side |
| UPDATE | Blocked always, at any age |
| TRUNCATE | Blocked always (statement trigger — row triggers do not fire) |
| DELETE | Blocked unless `created_at` is strictly older than the window |

The age check lives in the trigger rather than in a flag the deleting session
sets, because a switch the deleter turns on itself enforces nothing — and a
buggy prune with a wrong cutoff would then destroy recent records instead of
failing. `INSERT` is guarded too because `created_at` was client-settable: the
window could otherwise be walked around in two statements, by inserting a
backdated row and deleting it as expired.

The 180 is hardcoded in migration 023 and must match
`AI_DECISION_LOG_RETENTION_DAYS`;
`test_sql_retention_window_matches_the_python_constant` fails on drift.

**The prune's cutoff moved from Python's clock to the database's** — a
correctness requirement, not tidying, and the second letter-versus-spirit call
in this ticket. `interval '180 days'` is a *calendar* interval and Python's
`timedelta(days=180)` is an *absolute* one; under this deployment's
Europe/Berlin session timezone the two boundaries sit an hour apart for roughly
five months of the year, in the direction that makes the prune select rows the
trigger protects. A `BEFORE DELETE` trigger aborts the entire statement, so one
row in that band would fail a whole tenant's prune. Shipping the trigger
against the old client-side cutoff would have worked on a dev box and started
failing intermittently in November. Both sides now evaluate the same
expression, which makes them exact complements rather than merely close.

**How this may be described.** Row and statement triggers reject UPDATE,
TRUNCATE, and DELETE of rows inside the retention window, so accidental and
application-path mutation fail closed. This is **not** tamper-evidence: the
application still connects as the table owner (ticket C2 held) and can disable
the triggers, and unlike `audit_log` there is no hash chain — §13 KER-203
decision 2 deliberately did not give this table one. "Database-enforced" and
"tamper-resistant" are both overclaims until C2 lands, and even then a stronger
word needs a second layer this table does not have. The §15 approved demo
sentence covers **human** decisions and is unchanged; it must not be extended
to the AI log.

### ✅ RESOLVED (19 August 2026) — the docs switch and the dashboard switch are now separable

Both gate on `KERNO_ENV == "development"`, as do the two seed scripts. The
first time anyone wants API docs on a deployed host, the only lever available
is setting `KERNO_ENV=development` there — which simultaneously remounts the
legacy localStorage-JWT dashboard and unlocks `seed_dev_tenant.py` and
`seed_demo_evidence.py` against that database. One convenience request
disarms three unrelated controls.

**Resolved:** `KERNO_ENABLE_DOCS=1` (exactly `"1"`; `true`/`yes`/`TRUE` are all
off, failing closed) serves `/docs`, `/redoc` and `/openapi.json` without
touching the other two controls. The dashboard mount and both seed scripts
still gate on `KERNO_ENV == "development"` alone.
`test_enable_docs_does_not_remount_the_legacy_dashboard` pins the separation by
name, and a mutation that re-merges the two switches fails it.

The original description follows, for why it mattered.

The fix is a separate opt-in (`KERNO_ENABLE_DOCS`) that turns the schema
back on without touching the other two. Not built in Ticket B because the
approved scope named `KERNO_ENV` specifically, and because no staging
environment exists yet — there is no Dockerfile, compose file, CI workflow or
deploy manifest anywhere in the repo, so the coupling costs nothing today. It
should be resolved before the first non-development deployment, which is the
same moment the §14 HTTPS work happens.

This is the highest-priority item in this backlog and is deliberately marked so
in its heading. It is not urgent today — there is nothing to deploy to — but it
is the one that must not age quietly into the furniture the way the
FILE_STRUCTURE.md reconciliation did. The §14 deployment work is its deadline,
not its suggestion.

### ✅ RESOLVED (19 August 2026) — shipped CORS placeholder is a credentialed allow-list entry

Pre-existing, live, and independent of Ticket B — found while scoping it.
`.env.example` instructs `cp .env.example .env` and ships
`ALLOWED_ORIGINS=http://localhost:3000,https://your-vercel-app.vercel.app`,
and `src/api/app.py` passes that list to `CORSMiddleware` with
`allow_credentials=True`. Any deployment that shipped the example values has
granted credentialed cross-origin access to a `vercel.app` subdomain that is
project-name-scoped and first-come-first-served — i.e. potentially registrable
by someone else. Confirm the registrability claim before acting on it, but the
remedy is cheap either way: make the placeholder obviously invalid rather than
plausibly real — `https://REPLACE-ME.invalid` (the `.invalid` TLD is reserved
by RFC 2606 and can never be registered by anyone) — and fail startup on a
non-development environment whose `ALLOWED_ORIGINS` still contains an example
value.

**Resolved:** the shipped value is now `https://REPLACE-ME.invalid`, kept
SECOND so the `GET /` fallback to the first entry still points at
`http://localhost:3000` rather than redirecting a browser to `.invalid`. Both
example origins are named constants in `config/constants.py`, and the lifespan
refuses to start outside development while either is in `ALLOWED_ORIGINS` — the
superseded `vercel.app` one included, so a `.env` copied before the swap still
fails closed. Development is exempt, because `cp .env.example .env` is the
documented local path. Unset `ALLOWED_ORIGINS` remains fail-closed CORS and is
not an error.

Ticket B widens the blast radius slightly, since the same first entry is now
also the `GET /` redirect target. That is the argument for requiring
`FRONTEND_URL` rather than leaning on the fallback: a CORS allow-list has
unordered set semantics and is safe to add to, while a redirect target is
single-valued and order-sensitive. The fallback logs a warning when it fires.

### Backlog — uv.lock and the installed FastAPI disagree

`uv.lock` pins fastapi 0.139.0; the interpreter this repo actually runs on has
0.138.1 (no virtualenv exists — it resolves to the Python 3.14 user
site-packages). The whole test suite therefore executes against a version the
lock file does not describe, which is a SEC-06 reproducibility hole rather than
a functional bug. Noted while verifying the docs-route gating against FastAPI's
source; the gating behaviour is long-standing and does not differ between those
versions, so nothing in Ticket B depends on the resolution.

### Backlog — DORA register writes leave no audit trail

Found 13 August 2026 while verifying Ticket A against the live stack, and
confirmed by query rather than by reading code: creating a register entry
through `POST /api/v1/register/entries` writes the row and nothing else. Eight
entries were created during that verification run and `audit_log` recorded zero
entries for them.

This is the same class of gap as KER-405 finding #1. The DORA register is a
filed regulatory artefact — the thing a competent authority asks to see — and
today there is no record of who added or amended a line in it, or when. The
tenant_id and timestamps on the row itself are not an audit trail: they say a
row exists, not who decided it should. `update_register_entry` has the same
hole, which is worse, because an amendment silently overwrites the previous
values with no before_state anywhere.

The fix is the pattern already used by overrides and evidence links: append a
KER-107 ledger entry in the same transaction as the write, attributing the
verified JWT user_id, with before_state populated on the PATCH path. Not done
in Ticket A because Ticket A's scope was authorisation, not provenance, and
mixing the two would have made the RBAC change unreviewable.

Related: `POST /api/v1/submissions/runs` should be checked at the same time —
it was not verified whether a submission run (the act of filing) writes a
ledger entry either.

### Backlog — nonexistent submission window returns 500, not 404

`POST /api/v1/submissions/runs` with a `submission_window_id` that does not
exist returns HTTP 500. Observed during Ticket A live verification with id
`b0000000-0000-4000-b000-000000000009` on tenant `40b4be35…`; both permitted
roles (compliance_lead, vciso) reached the endpoint and got 500 rather than a
404. Pre-existing and unrelated to RBAC — the gate was working correctly; the
500 is what happens after it.

Every comparable surface raises `EntryNotFoundError` → 404 for an unknown id
(see the register and recommendation routers). This one does not, so an
operator typing a wrong window id sees a server error and a correlation ID
instead of "no such window". Worth checking whether the underlying
`build_and_record_submission` raises something unmapped or fails on a NULL
lookup result before the not-found check.

### Backlog — one test should police the teardown list, not each ticket

KER-409 found `dora_register_entries` and `dora_submission_runs` missing from
`_teardown_seed_data`, which made any test reading the register order-dependent
and leaked rows into the dev tenant. Both are fixed. Two tenant-scoped tables
are still absent, and each is a different failure mode:

- **`recommendations`** — no FK to `tenants`, so leftovers never fail teardown
  loudly. KER-401's tests self-clean because conftest does not. This is the
  same silent shape the DORA gap had.
- **`users`** — has an FK to `tenants`, so a leak fails teardown loudly.
  KER-408's tests clean their own rows.

The fix is not to keep adding table names as each ticket trips over one. It is
a single test that reads `information_schema` for every table carrying a
`tenant_id` column and asserts that set is a subset of the teardown list. That
generalised check would have caught the DORA gap before KER-409 hit it, and it
catches the next one for free.

Deliberately not done in KER-410: that is a UI ticket, and this is testing
infrastructure. It belongs in a testing-infra pass with the two missing tables
fixed at the same time — adding the check without adding them would land a
red test.

### Recorded, not scoped

Raised by the audit, parked without estimates:

- Gateway-level rate limiting (the standing §9 SEC-05 open item).
- JWT revocation and TTL policy — tokens are currently valid until expiry with
  no way to invalidate one.
- Webhook signing secrets at rest (pgcrypto) — §13 KER-205 decision 1 deferred
  this to "Sprint 3" and it did not happen.
- Cursor-level error swallowing that can mask a failed statement.
- Connection-pool RESET between checkouts. **Raised in priority (KER-409):** a
  pooled connection returns with `app.current_tenant_id` set to `''` rather than
  unset, so the next RLS policy that casts it to uuid fails outright. This has
  now cost time in three separate tickets' live tests and will keep doing so.
- Jira client authentication format.
- Page caps on PDF evidence-pack export.
- Pagination on the evidence list endpoint.
- FILE_STRUCTURE.md reconciliation (also logged in §16).
