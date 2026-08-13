# NOW.md — Current mandate (13 August 2026)

This file is in force via `CLAUDE.md` §0. For implementation priority it
outranks `KERNO_STRATEGY.md`, every `PROMPT_doc*.md`, and `FILE_STRUCTURE.md`.
Read this before starting a coding session.

It does **not** override `CLAUDE.md` §2 (readability), §3 (tenant isolation),
or §6 (GDPR data classification). Those still bind.

---

## Product slice in force

One UI: the Next.js app under `frontend/`.

One loop, in this order, on a tenant's own files:

1. Upload evidence
2. Link it to a control (with a relevance score)
3. Generate a recommendation
4. A named human approves, edits, or rejects it
5. Export an evidence pack

Until that loop works without a developer in the database, do not start new
frameworks, country packs, MSP billing, embeddings, or prompt-doc series.

## Honest claim (demo, deck, outreach)

Use only this sentence (already verified in `CLAUDE.md` §15):

> Every recommendation and every human decision made in Kerno is traceable
> to named evidence, a reproducible score, a named human, and a timestamp —
> with tamper-evident, database-enforced logging of every human decision.

## Do not claim (false for the running system)

- Personalised RAG, a live learning loop, or "Kerno's models"
- Production retrieval: `generate_recommendation()` does not call
  `get_similar_controls()` or `retrieve_similar_records()`
- Populated embeddings: `context_records.embedding` stays NULL on upload
- Uncurated links as calibrated confidence (they default to
  `DEFAULT_RELEVANCE_SCORE` = 0.5 → a flat partial/medium)
- DORA xBRL-CSV, 116 ESA checks, incident workflows, CRA reporting,
  member-state modules, or an MSP operator tier

The retrieval/bias code exists and is tested. It has no production caller.
Treat it as reserved machinery (KER-404 later), not as the product identity.

## UI rules

- Next.js `frontend/` is the product UI.
- `src/dashboard/` (localStorage JWT) is legacy. Do not extend it. Ticket B
  (in flight) stops serving it outside development.
- There is no Next.js DORA register/submissions UI. After B, those APIs remain;
  the HTML screens do not, except in dev. Do not silently rebuild them as part
  of an unrelated ticket.

## In flight — do not duplicate

A parallel session is landing security tickets. Do not re-implement them here.

| Ticket | Intent | Status |
|---|---|---|
| C1 (KER-408) | Login requires `tenant_slug`; lookup is `(slug, email)` | Committed locally as `ed3f3f2` (may not be on this branch yet) |
| A | `require_role` on scheduler, export, register writes, submissions runs, remediation trigger + close-callback | In progress |
| B | Legacy dashboard + OpenAPI off outside dev | Queued |
| D | Server-side justification; `ai_decision_log` append-only triggers | Queued |
| C2 | Non-owner DB role + FORCE RLS on `users` / webhook registrations | **Not this pass.** Own PR, real Postgres role |

Role matrix for A (authoritative):

- `POST /api/v1/scheduler/run-recalculation` → `compliance_lead`, `vciso`
- `GET /api/v1/export/evidence-pack` → `compliance_lead`, `vciso`, `security_engineer`, `platform_engineer`
- `POST/PATCH /api/v1/register/entries` → `compliance_lead`, `vciso`
- `POST /api/v1/submissions/runs` → `compliance_lead`, `vciso`
- `POST /api/v1/remediation/trigger` → `platform_engineer`
- `POST /api/v1/remediation/close-callback` → `platform_engineer` (JWT-consistent; HMAC redesign is backlog)

## After A / B / D — next product work

1. Generate button in the Next.js UI (KER-402) calling
   `POST /api/v1/recommendations/generate`
2. HTTPS + real `ALLOWED_ORIGINS` (Sprint 3 deploy leftover)
3. Then only: the loop on a design partner's own evidence

## Out of scope until the loop is true

CRA, DORA incidents, country modules, MSP / billing, embeddings / RAG wiring
(KER-404), the rest of KER-405, new `PROMPT_doc*.md` files, extending
`src/dashboard/`.

KER-405 stays on hold except the two items in Ticket D.

## Backlog — log only, do not start

Pulled from the August 2026 audit. Suggested fixes stay with the finding;
none of these block partner outreach once C1 / A / B / D have landed.

- C2 — app DB role is not the table owner; FORCE on `users` and
  `webhook_registrations` with a login bootstrap that still works
  (`SECURITY DEFINER` or equivalent)
- Login and webhook `/ingest` rate limits
- JWT revocation / shorter TTL
- `_CursorResult.fetchall` / `fetchone` swallowing exceptions
- Webhook signing secrets at rest
- Connection pool `RESET` / `DISCARD` on return; do not hold a pool
  connection across the LLM call
- Jira Cloud auth (Basic email:api_token vs Bearer); per-tenant credentials
- PDF page cap; evidence list pagination
- `FILE_STRUCTURE.md` full reconciliation against the live tree
- `close-callback` HMAC (KER-205 pattern), not a human RBAC role — if Jira
  should ever call it

## How to add work

Small stories that implement the loop or the in-flight security tickets.
Do not add a Document 18 prompt series. Do not implement from
`KERNO_STRATEGY.md` Part F/G.
