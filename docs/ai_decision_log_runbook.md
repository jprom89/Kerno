# AI-Decision Log Runbook (KER-203)

**Audience:** compliance engineers, vCISOs, auditors, and on-call engineers.
**System:** Kerno Compliance Copilot — `ai_decision_log` table (migration 020).
**Last updated:** 10 July 2026.

---

## 1. Purpose

Every time Kerno's mapping engine produces a compliance recommendation, one
row is written to `ai_decision_log` **in the same database transaction** as
the recommendation itself. A recommendation cannot exist without its retained
decision record, and vice versa.

Each row records:

| Field | Meaning |
|---|---|
| `correlation_id` | Kerno-generated UUID for this decision record |
| `control_id` | The control that was mapped (TEXT ref, matches `recommendations.control_id`) |
| `evidence_ids` | The evidence record refs the model cited |
| `input_snapshot_hash` | SHA-256 of the canonical JSON of the mapping inputs — see §4 |
| `output_status` | The model's outcome: `met`, `partial`, or `gap` |
| `confidence_score` | The model's self-reported confidence, 0.0–1.0 |
| `rationale_extract` | Short extract of the model's reasoning |
| `model_version` | The LLM that produced the decision (`KERNO_LLM_MODEL` at generation time) |
| `created_at` | Database timestamp of the decision |

This log is **separate from the KER-107 human-decision audit ledger**: it is
append-only in practice but not hash-chained, because it has a different
volume, retention, and query profile. Human decisions (overrides) stay in the
tamper-evident ledger; machine decisions live here.

## 2. Retention window

- Rows are retained **at least 180 days** — the window is
  `AI_DECISION_LOG_RETENTION_DAYS` in `config/constants.py`.
- A nightly prune job deletes rows **older** than the window. Rows inside the
  window are never touched: the retention duty is a floor, and the prune
  respects it by construction.
- Changing the window is a config change reviewed like code — do not shorten
  it below a customer's contractual or regulatory floor.

## 3. Legal basis

- **EU AI Act Articles 12, 19, and 26** — record-keeping and log-retention
  duties for high-risk AI systems. The Annex III obligations (as deferred by
  the EU Digital Omnibus on AI, in force since ~2 July 2026) apply from
  **2 December 2027**. Kerno ships the capability ahead of that deadline for
  procurement readiness with NIS2/DORA enterprise buyers.
- The log demonstrates that AI mapping decisions are **reconstructable**:
  what the model saw (via hash), what it decided, how confident it was, and
  which model version decided.

## 4. GDPR alignment — hash-only inputs

The raw mapping inputs (control text, evidence bodies) may contain personal
or confidential data. **They are never stored in this log.** Only
`input_snapshot_hash` is stored: the SHA-256 hex digest of the canonical
JSON (sorted keys, no insignificant whitespace) of the input snapshot.

- Verification works by re-deriving: given the recommendation's stored
  snapshot (in `recommendations.input_snapshot`, which lives under the same
  tenant's row-level security), re-canonicalise, re-hash, and compare to
  `input_snapshot_hash`.
- No log field contains free-text copied from evidence; `rationale_extract`
  is the model's own reasoning summary, which passes through the same
  pipeline controls as the recommendation's rationale.

Tenant isolation: the table has **FORCE row-level security** (migration 020)
— even the database owner role cannot read across tenants.

## 5. How to query

Authenticated tenants use the API (tenant identity comes from the JWT — it
is never accepted from the request):

```
GET /api/v1/ai-decisions
GET /api/v1/ai-decisions?control_id=ctrl-001
GET /api/v1/ai-decisions?after=2026-07-01T00:00:00Z
GET /api/v1/ai-decisions?confidence_gte=0.8
```

Filters compose (AND). Results are newest-first. The response is
`{ "decisions": [...], "count": N }`.

Engineers with database access can query directly — always under tenant
context:

```sql
BEGIN;
SET LOCAL app.current_tenant_id = '<tenant-uuid>';
SELECT correlation_id, control_id, output_status, confidence_score, created_at
FROM ai_decision_log
ORDER BY created_at DESC
LIMIT 50;
COMMIT;
```

## 6. How to run the prune job manually

The prune is a cron entrypoint (same mechanism as the KER-201 nightly bias
recalculation). From the repo root, with `.env` configured:

```
python -m src.scheduler.prune_ai_decision_log
```

It iterates every active tenant, deletes only rows older than the window,
logs one line per tenant plus a summary
(`success=<n> failure=<n> deleted=<n>`), and exits normally even when
individual tenants fail (failures are logged and skipped).

Schedule it nightly alongside the bias recalculation — cron on Linux,
Task Scheduler on Windows dev.

## 7. How to verify compliance for a regulator

To demonstrate the loop end to end:

1. **Existence + atomicity** — pick a recent recommendation
   (`recommendations.recommendation_id`) and show its decision record:
   same `control_id`, `created_at` within the same generation moment, and
   `output_status`/`confidence_score` matching the recommendation row. The
   integration test `tests/integration/test_ker203_ai_decision_log.py`
   proves the same-transaction guarantee mechanically.
2. **Input fingerprint** — re-hash the recommendation's stored
   `input_snapshot` (canonical JSON, SHA-256) and show it equals
   `input_snapshot_hash`.
3. **Retention** — show the oldest retained row is younger than the window
   plus the last prune date, and the prune logs show no deletion inside the
   window: `SELECT min(created_at) FROM ai_decision_log` (under tenant
   context) against `AI_DECISION_LOG_RETENTION_DAYS`.
4. **Isolation** — show `relrowsecurity` and `relforcerowsecurity` are both
   true for the table:
   `SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'ai_decision_log';`
5. **Model attribution** — every row carries `model_version`, so any decision
   can be attributed to the exact model configuration that made it.

## 8. Failure modes

| Symptom | Meaning | Action |
|---|---|---|
| Recommendation write fails with an `ai_decision_log` error | The same-transaction guarantee is working — neither row was committed | Fix the underlying error; nothing to clean up |
| Prune job logs `failure=N` | One or more tenants' prunes failed; their rows are still retained (safe direction) | Inspect the per-tenant error log lines; re-run manually |
| Row count grows without bound | Prune job not scheduled or failing silently | Check the scheduler entry and the prune logs |
