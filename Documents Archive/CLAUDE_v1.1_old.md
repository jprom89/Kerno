# CLAUDE.md — Kerno Compliance Copilot: Codebase Constitution

**Status:** Baseline v1.1 (Gap-Fixed)
**Classification:** Internal — Read by Claude at the start of every session
**Last updated:** 2026-06-18

---

## 0. Start Every Session Here

Before writing a single line of code, Claude must:

1. Read this file completely.
2. Read `FILE_STRUCTURE.md` to understand where every file lives.
3. Read `LEARNING_PIPELINE_SPEC.md` before writing any database query, RAG query, or embedding-related code.
4. Answer the three pre-flight questions below. Do not proceed until all three are answered correctly from the documents.

### Pre-Flight Questions (answer before coding)

**Q1 — Tenant Isolation Boundary:**
Where exactly is the tenant isolation boundary enforced? Name the PostgreSQL mechanism and the application-layer function that sets it.

**Q2 — GDPR Legal Basis:**
What is the GDPR legal basis for cross-tenant model optimisation, and what must the anonymisation pipeline strip before any data crosses tenant boundaries?

**Q3 — Audit Entry:**
Which function is responsible for emitting the audit log entry when a human overrides an AI-generated control recommendation? What fields must that entry contain?

If Claude cannot answer all three from the documents, it must stop and ask the human to clarify before proceeding.

---

## 1. Project Purpose

Kerno is a B2B SaaS Compliance-as-a-Service platform targeting EU mid-market companies (50–500 employees) that must comply with NIS2, DORA, EU AI Act, and the Cyber Resilience Act.

The core product is a Compliance Copilot: an AI-assisted control-mapping engine that learns each tenant's specific risk appetite through Human-in-the-Loop (HITL) override data, making its recommendations progressively more accurate the more it is used.

The primary competitive advantage is a compounding data moat: the more overrides a tenant provides, the more calibrated the retrieval layer becomes, and the higher the switching cost to any competitor.

---

## 2. Architecture in One Paragraph

Inbound security metadata enters an anonymisation pipeline that strips identifiable markers. Clean data is embedded and stored in a shared PostgreSQL instance with the pgvector extension. Row-Level Security (RLS) enforces tenant isolation at the database layer. The application layer sets a transactional tenant context before every query. Human overrides are captured, weighted by reviewer seniority, and fed into a nightly batch process that recalculates each tenant's retrieval bias vector. Subsequent queries inject this bias vector to produce progressively more accurate, tenant-calibrated recommendations. No base LLM fine-tuning occurs — all personalisation is handled through dynamic retrieval weighting.

---

## 3. Tenant Isolation — Non-Negotiable Rule

This is the most security-critical rule in the entire codebase. Violation is a security defect, not a style issue.

**Every function that opens a database connection and executes a query must:**

1. Call `set_tenant_context(tenant_id)` before any query runs.
2. Wrap the context-setting call and the query in the same database transaction.
3. Raise `TenantContextMissingError` if `tenant_id` is `None`, empty, or not a valid UUIDv4 — never proceed with a null or invalid context.
4. Never accept `tenant_id` directly from user input — always resolve it from the authenticated session object.

**The PostgreSQL RLS policy is a safety net, not a substitute for application-layer enforcement.** Both layers must be present.

**Example of correct pattern:**

```python
def get_similar_controls(query_vector: list[float], session: AuthenticatedSession) -> list[Control]:
    tenant_id = session.resolve_tenant_id()  # Never from raw input
    if not tenant_id:
        raise TenantContextMissingError("Tenant ID could not be resolved from session")
    with db.transaction() as conn:
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        results = conn.execute(SIMILARITY_QUERY, [query_vector, tenant_id])
    return results
```

**Example of forbidden pattern:**

```python
# FORBIDDEN — no tenant context set before query
def get_similar_controls(query_vector, tenant_id):
    return db.execute(SIMILARITY_QUERY, [query_vector, tenant_id])
```

Claude must flag any function that queries the database without first setting the tenant context via `set_tenant_context`.

---

## 4. Anti-Clever-Code Rules

These rules exist because this codebase will be read by compliance auditors, security reviewers, and investors — not just engineers. Code that a non-engineer cannot follow is a liability.

### 4.1 No Spec Variable Names in Production Code

Mathematical notation from the architecture spec must never appear verbatim in source code.

| Spec notation | Required code name |
|---|---|
| `W_ret` | `retrieval_bias_vector` |
| `W_ret_new` | `updated_retrieval_bias_vector` |
| `W_ret_old` | `current_retrieval_bias_vector` |
| `alpha` | `retention_decay_factor` |
| `gamma_i` | `reviewer_confidence_weight` |
| `V_err` | `override_error_vector` |
| `V_target` | `target_control_vector` |
| `V_source` | `source_recommendation_vector` |
| `tenant_id` | `tenant_id` (this one is fine as-is) |

### 4.2 Every Function Must Have a Plain-English Docstring

The docstring must explain what the function does in one or two sentences that a non-engineer can understand. It must not repeat the function signature.

**Good:**
```python
def recalculate_retrieval_bias(tenant_id: UUID, overrides: list[Override]) -> BiasVector:
    """
    Updates the tenant's personalised search weights based on new human override data.
    Called nightly by the batch scheduler after override events are collected.
    """
```

**Bad:**
```python
def recalculate_retrieval_bias(tenant_id, overrides):
    # updates W_ret using alpha decay
```

### 4.3 No Magic Numbers

Every numeric constant must be named and placed in `config/constants.py` with a comment explaining its origin.

```python
# config/constants.py
RETENTION_DECAY_FACTOR = 0.85        # Stability factor from LEARNING_PIPELINE_SPEC.md §4.1
SENIOR_REVIEWER_WEIGHT = 1.0         # vCISO / fractional CISO confidence weight
JUNIOR_REVIEWER_WEIGHT = 0.5         # Internal admin confidence weight
CALIBRATION_THRESHOLD_OVERRIDES = 200  # Minimum overrides before bias vector is meaningful
CALIBRATION_TARGET_ACCEPTANCE_RATE = 0.75  # Target recommendation acceptance rate post-calibration
```

### 4.4 No Implicit Type Coercions on Security-Sensitive Fields

`tenant_id`, `reviewer_role`, and `confidence_weight` must always be explicitly typed and validated. Never allow Python to coerce these silently.

### 4.5 Maximum Function Length: 40 Lines

If a function exceeds 40 lines, it must be split into smaller named functions. Long functions are the primary source of hidden security bugs.

---

## 5. Data Classification Rules

All data in this codebase falls into one of two classes. Claude must know which class a piece of data belongs to before deciding where to store or process it.

| Class | Examples | Storage | May leave tenant boundary? |
|---|---|---|---|
| **Tenant-Specific Context (High-Sensitivity)** | Override justification text, risk register descriptions, internal policy files, control mapping decisions | RLS-isolated pgvector table | Never |
| **Cross-Tenant Telemetry (Low-Sensitivity)** | Aggregate match success rates, control-mapping precision scores, token usage profiles | Central analytics table | Yes, after anonymisation pipeline |

The anonymisation pipeline must strip: internal hostnames, developer email addresses, IP ranges, cloud account IDs, and any string matching `[A-Z]+-[0-9]+` (internal ticket references) before telemetry crosses tenant boundaries.

---

## 6. File Placement Rules

Every file has exactly one correct home. See `FILE_STRUCTURE.md` for the canonical directory tree. When in doubt:

- Database models → `src/models/`
- Business logic (retrieval, overrides, bias recalculation) → `src/services/`
- API routes → `src/api/`
- Database migrations → `migrations/`
- Configuration and constants → `config/`
- Tests → `tests/` mirroring `src/` structure
- Architecture specs and design documents → repo root

Never create a new top-level directory without updating `FILE_STRUCTURE.md` first.

---

## 7. Testing Rules

### 7.1 The Isolation Test is Mandatory

Story KER-113 requires a cross-tenant isolation test. This test must exist in `tests/security/test_tenant_isolation.py` and must pass before any feature code is merged.

The test must prove: given tenant A's authenticated session, no vector similarity query returns any embedding, override, weight, or control belonging to tenant B, under any input including edge cases (empty vectors, null inputs, maximum-length inputs).

### 7.2 Every Security-Critical Function Needs a Negative Test

For every function that enforces a security boundary (tenant context, data classification, anonymisation), there must be a test that proves the function fails loudly when given invalid input — not silently proceeds.

### 7.3 Test Naming Convention

```
test_<function_name>_<scenario>_<expected_outcome>

Examples:
test_get_similar_controls_missing_tenant_raises_error
test_set_tenant_context_null_tenant_id_raises_error
test_anonymisation_pipeline_strips_ip_addresses
test_cross_tenant_query_returns_empty_result
```

---

## 8. Sprint 1 Story Reference

The following stories are Must-have for Sprint 1. Claude must not mark a story complete until its acceptance criteria are fully met.

| Story | Title | Points | Key Acceptance Criterion |
|---|---|---|---|
| KER-101 | Tenant model & UUIDv4 registration | 3 | Tenant record created with immutable UUIDv4; RLS policy active |
| KER-102 | Anonymisation pipeline | 5 | All six marker types stripped; audit log written |
| KER-103 | Embedding generation & storage | 5 | Vector stored with tenant_id; RLS query returns only own-tenant results |
| KER-104 | Evidence retrieval (RAG baseline) | 5 | Top-5 controls returned; tenant_id validated before query |
| KER-105 | AI recommendation engine stub | 3 | Returns structured recommendation; confidence score present |
| KER-106 | Override capture (HITL) | 3 | Approve/edit/reject captured; override record written with reviewer_role |
| KER-107 | Override audit log | 2 | Immutable append-only log; timestamp, reviewer_id, action_type present |
| KER-108 | Jira side-panel integration | 5 | Panel renders in Jira; control recommendation visible without leaving Jira |
| KER-111 | Evidence pack export | 3 | Export contains control ID, status, evidence link, human sign-off name |
| **KER-113** | **Cross-tenant isolation test** | **3** | **Tenant A cannot retrieve any data belonging to Tenant B under any query** |
| **KER-114** | **Nightly weight recalculation stub** | **5** | **Given ≥1 override, batch recalculates retrieval_bias_vector and persists it** |

KER-113 and KER-114 are new stories added in this revision. They are Must-have and Should-have respectively, and neither existed in the original Sprint 1 backlog.

---

## 9. GTM Correction Note

The GTM document states that the vCISO referral channel is a near-term acquisition channel. This is incorrect. vCISOs will not refer a pre-PMF tool to clients they are accountable for. The vCISO referral channel activates after 20+ paying logos exist. All pitch materials and planning documents must treat vCISO referrals as a Year 2+ channel, not a Year 1 channel.

