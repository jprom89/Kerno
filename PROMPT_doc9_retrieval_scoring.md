# PROMPT_doc9_retrieval_scoring.md
# Document 9 — Retrieval Scoring & Bias Application
# Spec version: 1.0 | Status: Authoritative
# Supersedes: any inline spec description in Claude prompts for Document 9

---

## 1. Purpose

This document specifies Document 9 of the Kerno learning pipeline codebase.

Document 9 closes the architectural pipeline break identified during the Document 8
build review: the bias vector written nightly to the `retrieval_bias` table never
reaches the live similarity query in `retrieval_service.py`.

After Document 9 is implemented, the full learning pipeline is end-to-end connected:

  Compliance engineer correction
    → capture_override()
    → nightly_bias_recalculation (writes retrieval_bias)
    → retrieval_service.get_similar_controls() (reads retrieval_bias, applies bias at query time)
    → ranked control results returned to the tenant

---

## 2. Background — the pipeline break

### 2.1 What was built in Documents 7 and 8

- `bias_recalculation_service.py` computes an updated bias vector per tenant and
  writes it to the `retrieval_bias` table (one row per tenant, upserted nightly).
- `retrieval_service.py` has a `fetch_tenant_bias_vector()` helper that reads the
  bias vector from `retrieval_bias` into a Python `list[float]`.
- `retrieval_service.py` passes that list as the `bias_vector` parameter to
  `run_biased_query()`.

### 2.2 The break

- `run_biased_query()` accepts `bias_vector: list[float]` as a parameter but
  never uses it.
- The SQL inside `run_biased_query()` instead references `retrieval_bias_vector`
  as a column name inside the `tenant_embeddings` table.
- No migration ever added that column to `tenant_embeddings`.
- No code ever writes the bias vector from `retrieval_bias` into `tenant_embeddings`.
- Result: the bias produced by the nightly batch has no effect on query results.
  The `bias_vector` parameter in `run_biased_query()` is dead.

### 2.3 Root cause

Two conflicting storage models existed simultaneously:
- **Normalised model** (intended): bias vector stored once per tenant in
  `retrieval_bias`, fetched at query time and passed as a parameter.
- **Denormalised model** (accidentally implemented in SQL): bias vector stored as
  a column on every row of `tenant_embeddings`, read from the table at query time.

Document 9 resolves this by standardising on the normalised model (option A).

---

## 3. Architecture decision

### 3.1 Chosen approach — Option A (normalised, parameter-passing)

The bias vector is passed as a SQL query parameter at query time.
It is NOT written into `tenant_embeddings` rows.
The `retrieval_bias` table remains the single source of truth for bias vectors.

### 3.2 Rationale

Denormalising the bias vector into every embedding row (option B) would require
updating O(n) rows on every nightly recalculation, introducing write amplification
proportional to the number of embeddings per tenant. Option A is cheaper, keeps
storage normalised, and requires no additional migration for tenant_embeddings.

### 3.3 Consequences

- `run_biased_query()` must use `bias_vector` as a bound SQL parameter.
- All references to `retrieval_bias_vector` as a column in `tenant_embeddings`
  must be removed from the SQL.
- If `tenant_embeddings` does not have a `retrieval_bias_vector` column in the
  live schema (migration 002 never added it as a functional pgvector column),
  no migration is required. If it does exist, a migration must drop it.
- The public API of `get_similar_controls()` does not change.

---

## 4. Biased similarity query specification

### 4.1 Formula

For each embedding row belonging to the tenant, compute:

  adjusted_score = cosine_similarity(query_vector, embedding_vector)
                 + (BIAS_INJECTION_COEFFICIENT * dot_product(bias_vector, embedding_vector))

Rank results by adjusted_score descending. Return the top MAX_SIMILAR_CONTROLS_RETURNED.

Notes:
- pgvector's cosine distance operator (<=>) computes cosine *distance*, not similarity.
  Cosine similarity = 1 - cosine distance.
  For ranking purposes, minimising cosine distance is equivalent to maximising
  cosine similarity. You may use distance throughout provided the bias term is
  subtracted (not added) consistently with the ranking direction.
- The bias vector and query vector must be cast to the pgvector `::vector` type
  before use in the query. The `_DbConnection._convert_named_params()` wrapper in
  `tests/conftest.py` handles this automatically for list[float] parameters.
- BIAS_INJECTION_COEFFICIENT is defined in config/constants.py.
- MAX_SIMILAR_CONTROLS_RETURNED is defined in config/constants.py.

### 4.2 Unbiased fallback

If `fetch_tenant_bias_vector()` returns `None` or an empty list (the tenant has no
bias row yet — for example, on their first day before any nightly run), fall back to
the unbiased cosine similarity query. Do not error. Document this behaviour in the
function docstring.

### 4.3 Table name

The embeddings table is named `tenant_embeddings`. Do not use `embeddings` anywhere
in SQL.

### 4.4 Tenant isolation

`resolve_and_set_tenant_context(conn, tenant_id)` must be called before any SQL
is issued. This sets `app.current_tenant_id` for the duration of the transaction,
activating the RLS policy on `tenant_embeddings` and `retrieval_bias`.

---

## 5. Files in scope

### 5.1 src/services/retrieval_service.py — REWRITE

Rewrite this file to fix the dead-parameter bug and close the pipeline break.

Required changes:
- `run_biased_query(conn, tenant_id, query_vector, bias_vector)`:
  - Use `bias_vector` as a bound `:bias_vector` parameter in the SQL.
  - Remove any reference to `retrieval_bias_vector` as a column in `tenant_embeddings`.
  - Apply the formula from §4.1.
- Import `TenantContextMissingError` from `src.exceptions` (not `src.db.rls`).
- All SQL must reference `tenant_embeddings` (not `embeddings`).
- No bare numeric literals. Constants from `config.constants` only.
- Every function under 40 lines. Factor helpers if needed.
- Module docstring must answer: What / Why / How to run or test.
- `conn` contract documented in module docstring: raw connection, `conn.execute(sql, dict)`,
  `:name`-style params.

### 5.2 tests/unit/services/test_retrieval_service.py — NEW

Unit tests for `retrieval_service.py` that run without a live database.

Required tests — implement all ten:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_biased_query_uses_bias_vector_parameter | When bias row exists, SQL contains `:bias_vector` parameter, not a column reference |
| 2 | test_unbiased_fallback_when_no_bias_row | When fetch returns None or [], unbiased path is taken |
| 3 | test_tenant_context_set_before_query | "SET LOCAL" appears in the first SQL call before any SELECT |
| 4 | test_none_tenant_raises_tenant_context_missing_error | None tenant_id raises TenantContextMissingError |
| 5 | test_empty_tenant_id_raises_tenant_context_missing_error | "" raises TenantContextMissingError |
| 6 | test_non_v4_uuid_raises_tenant_context_missing_error | Non-v4 UUID raises TenantContextMissingError |
| 7 | test_limit_respected | SQL contains the value of MAX_SIMILAR_CONTROLS_RETURNED as LIMIT |
| 8 | test_bias_injection_coefficient_applied | SQL references BIAS_INJECTION_COEFFICIENT by value, no bare float literals |
| 9 | test_table_name_is_tenant_embeddings | All SQL references tenant_embeddings, not embeddings |
| 10 | test_no_sqlalchemy_session_api_called | conn.add() and conn.flush() are never called |

Use a spy/stub connection (no live DB). Mirror the _SpyConn pattern from
`tests/unit/services/test_override_service.py`.

### 5.3 config/constants.py — only if new constants are needed

If any constant required by §4.1 is not already present in `config/constants.py`,
add it. Do not remove or rename existing constants. If no new constants are needed,
do not produce a new version of this file.

### 5.4 Migration — conditional

If `tenant_embeddings` has a `retrieval_bias_vector` column in the live schema
(check migration 002), produce a migration to drop it:

- New revision chaining after `e5f6a7b8`.
- `upgrade()`: drops `retrieval_bias_vector` column from `tenant_embeddings`.
- `downgrade()`: adds it back as `TEXT` (consistent with what migration 002 created,
  if anything — if migration 002 never added it, state this and omit the migration).
- Full module docstring (What / Why / How: `alembic upgrade <rev>` /
  `alembic downgrade <prev_rev>`).
- Docstrings on `upgrade()` and `downgrade()`.
- All standard gate checks must pass.

---

## 6. Gate checks (apply to every file produced)

Every file produced for Document 9 must pass all applicable checks:

| Check | Rule |
|---|---|
| Module docstring present | Answers What, Why, and How to run or test |
| All functions have docstrings | No exceptions |
| No spec notation in variable names | No Greek letters, subscripts, or raw spec symbols |
| No magic numbers | All numeric literals named in config/constants.py |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule followed | resolve_and_set_tenant_context before any DB access |
| TenantContextMissingError from src.exceptions | Not from src.db.rls or any other location |

---

## 7. Open questions — must be declared, not silently resolved

If any ambiguity exists in this spec that requires an assumption, state it
explicitly in the Open Questions section of your output. Do not silently resolve
ambiguities. The reviewer will confirm or correct each assumption before the
document is closed.

---

## 8. Authoritative references

| Document | Authority | Notes |
|---|---|---|
| CLAUDE.md (current version in working directory) | Highest | Overrides all other documents on process and gate rules |
| This file (PROMPT_doc9_retrieval_scoring.md) | Authoritative for Document 9 scope | |
| LEARNING_PIPELINE_SPEC.md | Authoritative for anonymisation tokens and pipeline design | §4.2 token names are correct; PROMPT_doc8 has been updated to match |
| PROMPT_doc8_learning_pipeline.md | Reference only | Document 8 is closed; do not reopen |