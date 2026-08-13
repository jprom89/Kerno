# NOW.md — Current mandate (13 August 2026)

This file is in force via `CLAUDE.md` §0. For implementation priority it
outranks `KERNO_STRATEGY.md`, every `PROMPT_doc*.md`, and `FILE_STRUCTURE.md`.
Read this before starting a coding session.

It does **not** override `CLAUDE.md` §2 (readability), §3 (tenant isolation),
or §6 (GDPR data classification). Those still bind.

---

## What Kerno is (the object we are filling)

Kerno is an EU **system of record** for operational-resilience obligations:
the live DORA Register of Information (maintain → validate → submit) and
named-human decisions on controls, with evidence attached.

Coverage grids, recommendation queues, and LLM rationale are **how a human
updates that record**. They are not the product. A US GRC buyer already
sells a coverage dashboard. Do not finish another one.

The hole, after Ticket B turns off `src/dashboard/` outside dev: the DORA
register and submissions APIs exist; the **product UI does not**. Fill that
hole in `frontend/`. Do not replace it with more NIS2 cards.

## Product slice in force

One UI: Next.js `frontend/`.

**Hygiene (in flight — do not duplicate):** C1, A, B, D. No new product
epics until those have landed.

**Then, in this order — stop if a story is really “nicer GRC”:**

1. **Thin wire only:** generate button (KER-402) on the existing
   recommendations page so a human can produce something to sign. No new
   engine, no batch-analyse-all, no RAG. Cap the ticket.
2. **HTTPS + `ALLOWED_ORIGINS`** so a design partner can log in.
3. **The hole — DORA in the real UI:** register list/detail/create/edit and
   submission windows/runs in `frontend/`, calling the existing
   `/api/v1/register` and `/api/v1/submissions` APIs. This replaces the
   legacy HTML B is removing from production. Do not rebuild `src/dashboard/`.
4. **One filing increment:** make a register export that could be handed to
   an authority (the xBRL-CSV-ready package that already exists, as a
   download from the Next.js register). Do **not** take on 116 ESA checks,
   portal upload, or BaFin/DNB integrations in the same ticket.
5. **Partner loop:** a tenant’s own vendors and evidence, without a developer
   in the database.

Nav should lead with Register once (3) exists. Coverage stays a read-only
view. Do not add coverage features, Trust Center polish, or recommendation
chrome until (3) and (4) exist.

## Honest claim (demo, deck, outreach)

Use only this sentence (already verified in `CLAUDE.md` §15):

> Every recommendation and every human decision made in Kerno is traceable
> to named evidence, a reproducible score, a named human, and a timestamp —
> with tamper-evident, database-enforced logging of every human decision.

Talk about a **register you maintain and a decision you can show**. Do not
talk about AI GRC, personalised retrieval, or competing with Vanta’s
dashboard.

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
  stops serving it outside development.
- After B, DORA is API-only until step (3) above. That gap is intentional and
  is the next product ticket — not a reason to keep the localStorage app.

## In flight — do not duplicate

A parallel session is landing security tickets. Do not re-implement them here.

| Ticket | Intent | Status |
|---|---|---|
| C1 (KER-408) | Login requires `tenant_slug`; lookup is `(slug, email)` | On `main` as `ed3f3f2` |
| A | `require_role` on scheduler, export, register writes, submissions runs, remediation trigger + close-callback | In progress |
| B | Legacy dashboard + OpenAPI off outside dev | Queued after A |
| D | Server-side justification; `ai_decision_log` append-only triggers | Queued after B |
| C2 | Non-owner DB role + FORCE RLS on `users` / webhook registrations | **Not this pass.** Own PR, real Postgres role |

Role matrix for A (authoritative):

- `POST /api/v1/scheduler/run-recalculation` → `compliance_lead`, `vciso`
- `GET /api/v1/export/evidence-pack` → `compliance_lead`, `vciso`, `security_engineer`, `platform_engineer`
- `POST/PATCH /api/v1/register/entries` → `compliance_lead`, `vciso`
- `POST /api/v1/submissions/runs` → `compliance_lead`, `vciso`
- `POST /api/v1/remediation/trigger` → `platform_engineer`
- `POST /api/v1/remediation/close-callback` → `platform_engineer` (JWT-consistent; HMAC redesign is backlog)

## Reject these even after A / B / D

- New coverage dashboard features, charts, or “operating cycle” UX
- Embeddings, RAG, bias injection in generate, KER-404
- Batch generate / “analyse all controls” as a launch epic
- CRA, DORA incidents, BSI/ANSSI/DNB packs, MSP, billing
- Extending `src/dashboard/`
- New `PROMPT_doc*.md` series
- KER-405 beyond Ticket D

KER-405 stays on hold except the two items in Ticket D.

## Backlog — log only, do not start

Pulled from the August 2026 audit. None of these block partner outreach once
C1 / A / B / D have landed.

- C2 — app DB role is not the table owner; FORCE on `users` and
  `webhook_registrations` with a login bootstrap that still works
- Login and webhook `/ingest` rate limits
- JWT revocation / shorter TTL
- `_CursorResult.fetchall` / `fetchone` swallowing exceptions
- Webhook signing secrets at rest
- Connection pool `RESET` / `DISCARD` on return; do not hold a pool
  connection across the LLM call
- Jira Cloud auth (Basic email:api_token vs Bearer); per-tenant credentials
- PDF page cap; evidence list pagination
- `FILE_STRUCTURE.md` full reconciliation against the live tree
- `close-callback` HMAC (KER-205 pattern), not a human RBAC role
- Register-create emits no KER-107 ledger entry (same class as KER-405 #1)
- `POST /submissions/runs` returns 500 instead of 404 for an unknown window

## How to add work

If the story does not move the **register you can maintain and file**, or the
**named human decision** that updates it, it is the wrong story.

Do not implement from `KERNO_STRATEGY.md` Part F/G.
