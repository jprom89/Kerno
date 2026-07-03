# CLAUDE.md — Kerno Compliance Copilot: Codebase Constitution v1.2
<!-- Version: 1.2 | Updated: 2026-06-19 | Changes: Added §10 Post-File Review Protocol -->

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

---

## §9 — GTM Correction (Pitch Material Alignment)

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

## §10 — Post-File Review Protocol (Non-Negotiable)

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

