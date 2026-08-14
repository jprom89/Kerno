# LEARNING_PIPELINE_SPEC.md — Document #8: Core Learning Pipeline & Data Isolation Specification

**Document Status:** Spec for a designed subsystem — **not the production
recommendation path** as of 13 August 2026.
**Target Framework:** Kerno Compliance Copilot Core Engine
**Classification:** Internal Technical Spec. Not a sales or investor claim
that the pipeline is live.
**Last updated:** 2026-08-13 (status correction; body otherwise v1.1)

Production generation is `recommendation_service.generate_recommendation`
(deterministic scorer + LLM rationale). This document remains the spec for
the retrieval/bias subsystem if/when KER-404 wires it. Present-tense
language below describes the *designed* behaviour of that subsystem, not
what `POST /api/v1/recommendations/generate` does today.

---

## 1. Executive Summary

Kerno does not fine-tune base Large Language Models. The *designed* personalisation path is an Override-Weighted Retrieval-Augmented Generation (RAG) pipeline: human overrides (KER-106) would adjust a per-tenant retrieval bias, and similarity search would use that bias at query time.

**As of 13 August 2026 that path is not live.** Overrides are stored and nightly bias recalculation can write `retrieval_bias`. Nothing in the production generate path calls `get_similar_controls()` or `retrieve_similar_records()`. Evidence upload does not write embeddings (`context_records.embedding` stays NULL). Switching cost and "calibrated recommendations" in this document are conditional on wiring retrieval into generation (KER-404) and on real override volume. Do not quote this section to investors as a description of the running product.

---

## 2. The Vector Store Decision: pgvector + RLS

Kerno uses a shared PostgreSQL instance with the pgvector extension and native Row-Level Security (RLS), rather than dedicated per-tenant vector clusters (such as separate Qdrant collections).

### 2.1 Why Not Dedicated Clusters

Dedicated clusters scale linearly in cost. A mid-market client processing 50–150 controls generates idle resource waste on a dedicated cluster. At seed stage, hyper-efficient multi-tenant infrastructure that guarantees data isolation produces better unit margins and is the architecture investors expect to see.

### 2.2 Why pgvector + RLS

PostgreSQL RLS combined with application-layer encryption provides defence-in-depth that passes German and French financial and infrastructure audits under DORA and NIS2. A single shared instance with strict tenant isolation enforced at two layers (PostgreSQL RLS + application `SET LOCAL`) is auditable, cost-efficient, and legally defensible.

---

## 3. Multi-Tenant Isolation Architecture

### 3.1 Data Flow

```
[ Inbound Security Metadata / Webhook ]
                     |
                     v
    [ Anonymisation Pipeline ]
      Strips: IPs, hostnames, email addresses,
      cloud account IDs, internal ticket references
                     |
                     v
    [ PostgreSQL (pgvector + RLS) ]
    ┌────────────────────────────────────────┐
    │                                        │
    │  Tenant A (RLS-isolated)               │
    │  - Local vectors                       │
    │  - Override records                    │
    │  - Retrieval bias vector               │
    │                                        │
    │  Tenant B (RLS-isolated)               │
    │  - Local vectors                       │
    │  - Override records                    │
    │  - Retrieval bias vector               │
    │                                        │
    └────────────────────────────────────────┘
```

### 3.2 Cryptographic Tenant Separation

Every vector embedding, control mapping, and telemetry chunk is bound to an immutable UUIDv4 `tenant_id`. This field is set at tenant registration and never changes.

**The RLS policy on the core vector table:**

```sql
ALTER TABLE tenant_embeddings ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_policy ON tenant_embeddings
  USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid);
```

**The application-layer guard (must be called before every query):**

```python
def set_tenant_context(conn, tenant_id: UUID) -> None:
    """
    Sets the PostgreSQL session variable that activates the RLS tenant isolation policy.
    Must be called inside a transaction before any vector query is executed.
    Raises TenantContextMissingError if tenant_id is None or invalid.
    """
    if not tenant_id:
        raise TenantContextMissingError("Tenant ID required before database query")
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
```

Both layers must be present. The RLS policy is a safety net; the application guard is the primary enforcement point.

---

## 4. Data Classification Boundaries

### 4.1 Binary Signal Classification

| Layer | Contents | Storage | Crosses Tenant Boundary? |
|---|---|---|---|
| **Tenant-Specific Context (High-Sensitivity)** | Manual overrides (KER-106), justification text, risk register descriptions, internal policy files | RLS-isolated pgvector table | Never |
| **Cross-Tenant Telemetry (Low-Sensitivity)** | Aggregate match success rates, control-mapping precision scores, token usage profiles | Central analytics table | Yes, after anonymisation pipeline |

### 4.2 GDPR Legal Basis

Cross-tenant model optimisation is grounded in **GDPR Article 6(1)(f) — Legitimate Interest**. Kerno has a legitimate interest in improving the accuracy and security performance of its automated mapping engine.

The anonymisation pipeline is the legal gate. Before any data is processed for cross-tenant optimisation, the following identifiers must be stripped and replaced with generalised tokens:

| Identifier Type | Replacement Token |
|---|---|
| Internal hostnames | `[INTERNAL_HOST]` |
| Developer email addresses | `[INTERNAL_EMAIL]` |
| IP address ranges | `[IP_RANGE]` |
| Cloud account IDs (AWS, GCP, Azure) | `[CLOUD_ACCOUNT]` |
| Internal ticket references (`[A-Z]+-[0-9]+`) | `[INTERNAL_TICKET]` |

Manual override justification text is defined in Kerno's standard Data Processing Agreement as confidential business data, held under zero-knowledge retention limits relative to Kerno's central engineering staff.

---

## 5. The Retrieval Optimisation Loop

### 5.1 Architecture

```
[ Human Override Input ]
  (e.g., reject AWS SecHub mapping to NIS2 Article 21)
                     |
                     v
    [ Nightly Batch Scheduler ]
    (src/scheduler/nightly_bias_recalculation.py)
                     |
                     v
    [ bias_recalculation_service.py ]
    Recalculates retrieval_bias_vector per tenant
                     |
                     v
    [ Updates retrieval_bias row in pgvector ]
    Next similarity query uses updated weights
```

### 5.2 The Weight Recalculation Formula

When a compliance engineer overrides an AI recommendation, the override is captured as a vector distance between what the AI recommended and what the human mapped.

The nightly batch process updates the tenant's retrieval bias vector as follows:

```
updated_bias = (retention_decay_factor × current_bias)
             + (1 - retention_decay_factor)
             × sum_over_overrides(reviewer_confidence_weight × (target_vector - source_vector))
```

**Parameter definitions:**

| Parameter | Code name | Value | Meaning |
|---|---|---|---|
| `W_ret` | `retrieval_bias_vector` | Calculated | The bias applied to similarity scoring for this tenant |
| `alpha` | `retention_decay_factor` | 0.85 | How much historical calibration is preserved in each update |
| `gamma_i` | `reviewer_confidence_weight` | 1.0 (vCISO/fCISO) or 0.5 (internal admin) | Confidence weight of the human reviewer |
| `V_target - V_source` | `target_vector - source_recommendation_vector` | Calculated | Vector distance shift between AI recommendation and human correction |

Constants are defined in `config/constants.py`. Spec notation must never appear in source code — see `CLAUDE.md §4.1`.

### 5.3 Query Execution with Bias Injection

When (and only when) the RAG pipeline runs a similarity query, the tenant's `retrieval_bias_vector` is injected to produce calibrated rankings. Implemented in `retrieval_service.py`. **No production caller as of 13 August 2026** — unit and KER-201 integration tests only.

```sql
-- Calibrated similarity query with tenant bias injection
SELECT
    control_id,
    (embedding <=> :query_vector)
      - (retrieval_bias_vector <=> :query_vector) * :bias_coefficient
      AS calibrated_distance
FROM tenant_embeddings
WHERE tenant_id = :tenant_id   -- RLS enforces this; application also validates
ORDER BY calibrated_distance ASC
LIMIT 5;
```

---

## 6. The Commercial Switching-Cost Moat

**Investor-facing and conditional.** The mechanism below is real *if* overrides flow into bias *and* generation consumes that ranking. KER-201 closed the write side of the loop. Generation still scores manually linked evidence and does not consume retrieval. Treat §6 as a thesis, not a current metric.

### 6.1 Calibration Timeline

A mid-market technology vendor with a typical cloud surface topology maps 50–150 regulatory controls. Meaningful calibration requires 200–500 individual manual confirmation or override events across the developer stack. This baseline is typically established within 90–180 days of active onboarding.

### 6.2 Calibration Threshold

Once 200+ overrides are captured, the retrieval layer targets a recommendation acceptance rate of 75% or higher. This is the threshold at which the system is meaningfully more accurate for the tenant than any generic competitor.

### 6.3 The Exit Barrier

If a tenant migrates to a generic competitor, they abandon their calibrated `retrieval_bias_vector`. Their compliance teams return to a generic baseline and face hundreds of manual alert classifications from scratch. This operational friction is the mechanism that minimises churn.

For investor due diligence: this moat is real only if (a) overrides flow into nightly recalculation (KER-201 does this), (b) generation *uses* the biased ranking (it does not; that is KER-404), and (c) the bias vector improves over the first 200 real overrides. Do not present §6 as closed.

---

## 7. Sprint Story Mapping

| Story | Document #8 Section | What it implements |
|---|---|---|
| KER-101 | §3.2 | UUIDv4 tenant registration, RLS policy activation |
| KER-102 | §4.2 | Anonymisation pipeline, identifier stripping |
| KER-103 | §3.1 | Embedding generation, pgvector storage with RLS |
| KER-104 | §5.3 | RAG query with tenant context and bias injection |
| KER-106 | §5.1 | Override capture, reviewer_confidence_weight assignment |
| KER-107 | §4.1 | Immutable audit log for override events |
| KER-113 | §3.2 | Cross-tenant isolation security test |
| KER-114 | §5.1, §5.2 | Nightly batch recalculation of retrieval_bias_vector |

