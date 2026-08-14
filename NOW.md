# NOW.md — Current mandate (14 August 2026)

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

**Hygiene (landed on `main`, do not re-do):** C1 `ed3f3f2`, A `7b6738b`,
B `715dbbe`, D `fb741f0` + `e5c28ac`. C2 still held.

**Next session — fill the hole Ticket B opened. In scope together:**

| Ticket | What |
|---|---|
| **KER-409** | Ledger + 404. `create_register_entry` / `update_register_entry` append KER-107 in the same transaction (JWT `user_id`, `before_state` on PATCH). Same ledger write on `POST /submissions/runs`. Unknown `submission_window_id` → `EntryNotFoundError` (404), not `ValueError` (500). |
| **KER-410** | Next.js register: list/detail/create/edit on existing `/api/v1/register` routes. Nav leads with Register. Do not rebuild `src/dashboard/`. |
| **KER-411** | Next.js windows + runs on existing `/api/v1/submissions` routes. Start a run from an open window; show history. No xBRL, no ESA 116. |

Do not ship 410/411 without 409. A UI that multiplies unledgered RoI rows is worse than no UI.

### Confirmed before start (14 August 2026)

**409 tests are two behaviours, two tests.** Live-DB coverage must assert the ledger row and the 404 as **separate tests**, not one incidental pass. Minimum:

- create → ledger row, same transaction (commit and rollback)
- PATCH → ledger row with `before_state`
- known window → run + ledger row
- unknown `submission_window_id` → 404 **and** no ledger row, no run row

Do not fold the 404 case into the happy-path ledger test.

**410/411 auditor: UI hide is not the control.** Ticket A already 403s auditor JWTs on `POST`/`PATCH /api/v1/register/entries` and `POST /api/v1/submissions/runs` (`tests/unit/api/test_rbac_gates.py`). Do not re-do that matrix. Frontend hides create/edit/start-run for `auditor` (same pattern as recommendations/evidence). Hiding buttons is UX; the 403 is the guarantee. If those three routes ever stop 403ing an auditor, stop — that is a regression of audit finding #3, not a UI bug.

**409 first is non-negotiable. 410 before 411 is not a data dependency.** `POST /submissions/runs` looks up a global `dora_submission_windows` row, then builds an export from whatever active register entries exist. Zero entries is a valid run: `entry_count=0`, ROI_000 FAIL, status `draft`. 411 can be tested with no 410 rows. A passing (`ready`) filing in a demo needs entries from 410; that is demo quality, not a test blocker. Windows are not in `seed_dev_tenant.py` — 411 tests insert their own window; do not invent a country-pack seeder.

Order in the sitting: 409 → 410 → 411. After 409, 410 and 411 are independently testable.

**After that, not in the same session:**

4. Thin generate button (KER-402) — wire only, no new engine.
5. HTTPS + `ALLOWED_ORIGINS` / obviously-invalid `.env.example` placeholder.
6. One filing **download** from the Next.js register (existing export package).
7. Partner’s own vendors and evidence.

Nav should lead with Register once KER-410 exists. Coverage stays a
read-only view. Do not add coverage features, Trust Center polish, or
recommendation chrome until KER-409–411 exist.

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
- After Ticket B, DORA is API-only until the Next.js register session
  ships. That gap is the next product ticket — not a reason to keep the
  localStorage app.

## In flight — do not duplicate

Hygiene is on `main`. Do not re-implement C1/A/B/D.

| Ticket | Intent | Status |
|---|---|---|
| C1 (KER-408) | Login requires `tenant_slug` | `ed3f3f2` on `main` |
| A | `require_role` on six routes + structural sweep | `7b6738b` on `main` |
| B | Legacy dashboard + OpenAPI off outside dev | `715dbbe` on `main` |
| D | Justification + `ai_decision_log` retention triggers | `fb741f0` + `e5c28ac` on `main` |
| C2 | Non-owner DB role + FORCE | **Held.** Own PR, real Postgres role |

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

Pulled from the August 2026 audit. Register KER-107 and submissions 404 are
**not** in this list — they are in the next session's scope above.

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

## How to add work

If the story does not move the **register you can maintain and file**, or the
**named human decision** that updates it, it is the wrong story.

Do not implement from `KERNO_STRATEGY.md` Part F/G.
