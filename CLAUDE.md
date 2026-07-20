# CLAUDE.md — Kerno Compliance Copilot: Codebase Constitution v1.2
<!-- Version: 1.9 | Updated: 2026-07-18 | Changes: Added §15 post-diligence roadmap — KER-401 production trigger + hybrid engine; KER-402/403/404 deferred; §11 live-database rule -->

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
- KER-404 — retrieval-augmented correction memory (~8 pts): inject similar
  past human corrections into generation via the (already built, tested)
  biased retrieval. Gated on weeks of real design-partner override volume.
- Fine-tuning: never (constitution §1).

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
