# NOW.md — Current mandate (20 August 2026)

This file is in force via `CLAUDE.md` §0. For implementation priority it
outranks `DORA_MODEL_V2.md`, `KERNO_STRATEGY.md`, every `PROMPT_doc*.md`, and
`FILE_STRUCTURE.md`. `DORA_MODEL_V2.md` is authoritative for *how* the DORA
domain is modelled; this file is authoritative for *what is built next*.
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

The hole Ticket B opened — no product UI for the DORA register and
submissions outside development — is filled. Those screens live in Next.js
(`/dashboard/register`, `/dashboard/submissions`). Do not rebuild them. Do
not replace the next slice with more NIS2 cards.

## Product slice in force

One UI: Next.js `frontend/`.

**Hygiene (landed on `main`, do not re-do):** C1 `ed3f3f2`, A `7b6738b`,
B `715dbbe`, D `fb741f0` + `e5c28ac`. C2 still held.

**KER-409 through KER-412, KER-402, CORS/docs hygiene, and the frozen
filing download are LANDED. Do not re-implement any of them.** The DORA
hole Ticket B opened is filled: ledgered register writes, Next.js register,
Next.js windows/runs, contract-shaped errors, a per-control Analyse button,
and a download of the filing JSON frozen at Start-run. If you are reading
this to pick up work, **do not start another register or submissions UI
ticket.** Next is founder HTTPS, then the partner's own vendors and
evidence. That is the proof, not a bet on a platform.

| Ticket | What | Status |
|---|---|---|
| **KER-409** | Ledger + 404. `create_register_entry` / `update_register_entry` append KER-107 in the same transaction (JWT `user_id`, `before_state` on PATCH). Same ledger write on `POST /submissions/runs`. Unknown `submission_window_id` → `EntryNotFoundError` (404), not `ValueError` (500). | ✅ done `0b3e63e` |
| **KER-410** | Next.js register: list/detail/create/edit on existing `/api/v1/register` routes. Nav leads with Register. Do not rebuild `src/dashboard/`. | ✅ done `729cc34` |
| **KER-411** | Next.js windows + runs on existing `/api/v1/submissions` routes. Start a run from an open window; show history. No xBRL, no ESA 116. | ✅ done `521b387` |
| **KER-412** | Register validation → 422 with the reason; malformed ids → 404. | ✅ done `36df799` |
| **KER-402** | Thin Analyse button on `/dashboard/controls` — wire to the existing `POST /api/v1/recommendations/generate` only. No new engine, no analyse-all. | ✅ done `907bef3` |
| **Frozen filing download** | `dora_submission_runs.frozen_package_json` TEXT stores the canonical export JSON at Start-run. `GET /api/v1/submissions/runs/{run_id}/package` returns those bytes unchanged. A later register edit does not change the file; Start-run again replaces the freeze. Same 404 as a missing run when the column is NULL. Ledger `filing_package_downloaded` on success only. Roles: `compliance_lead`, `vciso` — filing authority, not the NIS2 evidence-pack list. | ✅ done `7e64caf` |

Do not ship 410/411 without 409. A UI that multiplies unledgered RoI rows is
worse than no UI — that prerequisite is met.

### Standing pre-flight — restart the API from HEAD before any live E2E

Do not assume a running `uvicorn` is on the current commit. It usually is not:
the process was started earlier in the session and does not reload. This has
produced a confident wrong answer twice — Ticket A read as six ungated routes,
and KER-410 read as zero ledger entries — and both times the code was correct
and the server was old. Restart it, then click through:

    python -m uvicorn src.api.app:app --port 8000 --log-level warning

### KER-410 constraints (confirmed 14 August 2026)

- No work in `src/dashboard/`. It is development-only since Ticket B and is
  not being rebuilt.
- No KER-405 work beyond what Ticket D already shipped.
- No coverage chrome, no RAG, no generate-all.
- No CRA, incidents, country packs, or MSP.
- **Auditor writes stay gated by Ticket A's 403s.** The UI hides
  create/edit/start-run for `auditor`; it does not re-do RBAC. Hiding a
  button is UX, the 403 is the guarantee.

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

4. ~~Thin generate button (KER-402)~~ — ✅ done `907bef3`. One Analyse button per
   control row; the engine, RBAC, and rate limit were already live.
5. ~~`ALLOWED_ORIGINS` / obviously-invalid `.env.example` placeholder~~ —
   ✅ done `cd62237`. Placeholder is `https://REPLACE-ME.invalid` (RFC 2606, cannot
   be registered), the API refuses to start outside development while a
   placeholder is still in the allow-list, and `KERNO_ENABLE_DOCS=1`
   serves the docs without remounting `/dashboard/` or unlocking the seed
   scripts. Putting the API on an HTTPS host is a founder task, not a
   ticket — nothing in the repo deploys anything.
6. ~~One filing **download**~~ — ✅ done. Frozen at record time (TEXT of
   canonical JSON on `dora_submission_runs.frozen_package_json`), not a live
   rebuild of today's register. Do not reuse the NIS2 evidence pack as the
   DORA filing. A second Start-run replaces the freeze, matching the counts
   on the page.
7. **NEXT (founder, not a Claude ticket):** put the FastAPI API on HTTPS
   with real `ALLOWED_ORIGINS` / `FRONTEND_URL`. No Dockerfile in this repo.
8. **Then:** the partner's own vendors and evidence. That is the proof.

Nav already leads with Register. Coverage stays a read-only view. Do not
add coverage features, Trust Center polish, or recommendation chrome.

## DORA v2 relational workstream (authority established 18 September 2026)

This is a parallel workstream, not a replacement for items 7 and 8 above.
Founder HTTPS and the partner's own vendors and evidence stay where they are
and stay first: **real partner and vendor data remains a required proof
point**, and the v2 model must survive it before any regulatory compiler
work is authorised.

- **`DORA_MODEL_V2.md` governs all future DORA domain-model work.** It sits
  immediately below this file in the authority order set out in
  `CLAUDE.md` §0. Documents 14–17B are historical v1 implementation records.
- **`dora_register_entries` is now legacy v1 persistence and a migration
  input.** Do not add regulatory concepts to it. Do not drop it. Do not
  transform its rows destructively. It remains operational until the v2
  cutover is proven.
- **Everything that works today keeps working.** The current register
  UI/API, submission windows and runs, and the frozen filing download stay
  live until v2 cutover is proven on real data — not until it compiles.
- **Retained, not renegotiated:** the KER-107 hash-chained audit ledger,
  RLS + FORCE tenant isolation, RBAC via Ticket A's literal-string matrix,
  and the frozen-filing invariant (a later register edit never changes an
  existing frozen package). v2 writes use explicit `dora_*` object
  vocabularies on the *existing* ledger — no parallel DORA audit subsystem.
- **Incremental, never greenfield.** v2 lands through `DORA-V2-*` tickets in
  the order `DORA_MODEL_V2.md` §36 sets out, each explicitly approved before
  implementation. A schema that compiles is not a reason to advance.
- **Mandatory real-data checkpoint.** After service arrangements, usages and
  locations (V2-003) and before any regulatory projection, validation, or
  package work (V2-008 through V2-010), the relational model is tested
  against a real partner register (`DORA_MODEL_V2.md` §37). That checkpoint
  is not skippable.

| Ticket | What | Status |
|---|---|---|
| **DORA-V2-000** | Place `DORA_MODEL_V2.md` and establish the authority hierarchy. Documentation only. | ✅ done (`0ae3df4`) |
| **DORA-V2-001** | Organizations, identifiers, roles — `dora_organizations`, `dora_organization_identifiers`, `dora_organization_roles`; ENABLE + FORCE RLS; composite `(tenant_id, organization_id)` FKs; ledger via the existing `audit_log`. No API, no UI. | ✅ done (`3a8ca8e`; review follow-ups merged via PR #7) |
| **DORA-V2-002A** | Contract records and signing parties — `dora_contracts`, `dora_contract_parties`; ENABLE + FORCE RLS; composite `(tenant_id, …)` FKs to contracts and organisations; duplicates decided by the unique constraints (controlled conflict); locking-read updates; ledger via the existing `audit_log`. No API, no UI, no hierarchy, costs, services or functions. Not a regulator-ready Register. | ✅ merged via PR #8 (`649fb69`); live-DB verification first ran on `kerno_dev` before TEST-SAFETY-001, and was repeated on `kerno_test` on 1 October 2026 (TEST-SAFETY-001 acceptance below: full suite and post-round-trip rerun passed, no skips) |
| **DORA-V2-002B** | Contract hierarchy (§7.3) — `dora_contract_relationships` (child → overarching parent); ENABLE + FORCE RLS; composite `(tenant_id, contract)` FKs at both ends; self-link and type CHECKs; a partial unique index allowing one active parent per child; append-and-deactivate history; cycles refused by the service, serialised per tenant on the existing ledger lock, READ COMMITTED only; ledger via the existing `audit_log`. Records links only — does not produce B_02.01.0020/0030, classify contracts, or infer "standalone". No API, no UI. Not customer-ready or regulator-ready. | implemented on branch `dora-v2-002b/contract-hierarchy`, pending independent review |
| **DORA-V2-002 (remaining slices)** | Contract costs (§7.4), ICT services, functions/designations | not started — each slice needs explicit approval |
| **DORA-V2-003 … 011** | Per `DORA_MODEL_V2.md` §36 | not started |

**V2-001 review follow-ups (21 September 2026, branch
`dora-v2-001/concurrent-audit-before-state`, reviewed and merged to main
via PR #7, `bec2959`):** the three V2-001 models now match migration 025
exactly (tenants FKs, `gen_random_uuid()` and `true` server defaults, the
identifier index, no ORM-only `onupdate`), pinned by a scoped Alembic
comparison with server-default comparison on plus catalog checks of every
CHECK, FK, UNIQUE and index; and `update_organization` reads under
`FOR NO KEY UPDATE`, so a concurrent amendment ledgers the state it actually
replaced. That fix protects audit before-state accuracy only. A stale user
edit is **not** rejected — it waits, then wins, with an accurate trail.
Rejecting it needs an optimistic token in an API that does not exist yet.
Lock order is recorded per call and per transaction in the service docstring:
a caller-owned transaction that has already ledgered can deadlock against a
concurrent single update of the same row; PostgreSQL aborts one side (40P01),
the loser writes nothing, and that is pre-existing KER-107 behaviour, pinned
by a live test, not re-designed here.

**Gate before any DORA-V2 API or import ticket exposes identifier or role
writes:** `add_organization_identifier` and `add_organization_role`
check-then-insert. The UNIQUE constraints already prevent duplicates; the
outstanding issue is that a concurrent loser surfaces as the driver's
`UniqueViolation` rather than the service's `ValueError`. Decide and implement
one domain-level conflict outcome (one exception, one HTTP mapping) before
those functions are reachable from outside tests. Not started. DORA-V2-002A
added `DORAContractConflictError`, deliberately contract-scoped (the ticket
kept organisation remediation out of scope); whether organisations reuse it or
a DORA-wide type replaces both is part of this decision.

**Gate before any router or import exposes contract-hierarchy writes:**
`add_contract_relationship` and `deactivate_contract_relationship` refuse to
run outside a READ COMMITTED transaction the caller owns
(`UnsupportedTransactionIsolationError`, before anything is read or written)
and hold the tenant ledger lock until that transaction ends. The caller must
open such a transaction, map `DORAContractConflictError` (duplicate, second
parent, cycle) to one controlled conflict outcome, and keep the transaction
short. Cycle prevention covers writers that use the service; raw SQL by the
table owner can still write a cycle. Deadlocks remain possible over a whole
caller transaction (the KER-107 per-transaction limitation, unchanged): once
a hierarchy write has run, a later call in the same transaction that waits
on a row lock — e.g. `update_contract` on a contract another transaction has
already locked while it queues for the tenant lock — can deadlock, and so can
locks taken earlier without ledgering. The service docstring records both.

**Gate before any slice adds a way to deactivate or delete an organisation
role:** `add_contract_party` requires `provider_signatory` and
`intragroup_provider_signatory` organisations to hold an active `ict_provider`
role, checked with an unlocked read that no database constraint backs. It is
sound only while nothing removes a role. The slice that adds removal must lock
the role row in that check or guard the rule in the database.

## Test-database safety — TEST-SAFETY-001 (prerequisite for every live-DB test)

**Status: merged to main via PR #9 (`a3a313e`). Live acceptance on
`kerno_test` completed on 1 October 2026; the results are below.**

- **Approval record (26 September 2026):**
  `kerno_test@127.0.0.1:5432/kerno_test` was approved as a disposable test
  database by the owner, in writing, on 26 September 2026. On the same date
  the owner delegated its one-time provisioning to Claude Code: the
  inspectable `scripts/provision_test_database.py`, run by the owner in
  their own PowerShell window, with the administrator password typed only at
  its hidden prompt.
- **Provisioning result (owner-reported, 1 October 2026):** the owner ran
  `scripts/provision_test_database.py` on MSI and reported
  `DONE: kerno_test@127.0.0.1:5432/kerno_test provisioned and verified.`
  with exit code 0. The output the owner reported confirmed:
  - a restricted login role with no elevated attributes or memberships;
  - the database owned by that role, with PUBLIC access revoked;
  - `vector` installed and the disposable marker present;
  - `.env.test` created and gitignored;
  - a login as `kerno_test`;
  - `kerno_dev`'s access list unchanged.

  Claude Code did not see that run; this entry records the owner's report.
- **Target verification (Claude Code, 1 October 2026):** read-only, through
  the safety module's guard connection, as `kerno_test` with the settings
  from `.env.test`:
  - database `kerno_test`; session and current user `kerno_test`;
  - server 127.0.0.1:5432, PostgreSQL 18.4;
  - no superuser, BYPASSRLS, CREATEDB, CREATEROLE or REPLICATION, and no
    role memberships;
  - owner `kerno_test`, comment `kerno:disposable-test-database`, access
    list `{kerno_test=CTc/kerno_test}`;
  - extensions `plpgsql` and `vector` 0.8.3, and an empty `public` schema
    before migration.
- **Live acceptance results (1 October 2026, code at `cb0fed1`):**
  - **Schema build:** `python scripts/migrate_test_database.py upgrade head`
    exited 0 after 26 single-revision steps, from base to `a2b3c4d5`.
  - **Full suite:** `python -m pytest --require-live-database` exited 0 with
    **1340 passed, 0 failed, 0 skipped**. Its 225 warnings were
    `DeprecationWarning`, `StarletteDeprecationWarning` and
    `InsecureKeyLengthWarning`.
  - **Exclusion, in both directions, without bypassing the lock:**
    - While the migration held it, a pytest run was refused (exit 2) and a
      second migration run was refused (exit 3).
    - While the full pytest run held it, `downgrade z1a2b3c4` was refused
      (exit 3) and a second pytest run was refused (exit 2).

    Every refusal came before any change and named the holder
    (`kerno-test-guard:migrate` or `kerno-test-guard:pytest`). After each
    release the next workflow ran (exit 0). The refused downgrade left the
    revision at `a2b3c4d5`.
  - **Round trip:**
    - `downgrade z1a2b3c4` exited 0. The revision went from `a2b3c4d5` to
      `z1a2b3c4` and both contract tables were removed.
    - `upgrade head` exited 0. The revision returned to `a2b3c4d5` and both
      tables came back with RLS enabled and forced.
  - **After the round trip:** the DORA-V2-001 and -002A live, isolation and
    schema-parity tests, plus the migration test (9 files), exited 0 with
    **224 passed, 0 skipped**.

- Live-database tests and test migrations run only against the owner-approved
  disposable `kerno_test`, owned by a restricted `kerno_test` role — never
  `kerno_dev`, never via `DATABASE_URL`, `.env` or libpq defaults. Settings:
  `KERNO_TEST_DATABASE_URL` + `KERNO_TEST_DATABASE_APPROVAL` (environment or the
  gitignored `.env.test`).
- `kerno_test` now exists and is verified. For new work, CLAUDE.md §11's
  live-database rule is met only by a run of
  `python -m pytest --require-live-database` on `kerno_test` with zero skips.
  Do not describe any later DORA slice as live-verified without one. Without
  valid settings, every live-database test skips with its reason.
- One workflow at a time: pytest and `scripts/migrate_test_database.py` share one
  session-level advisory lock on `kerno_test`; parallel workers are refused.
- `kerno_dev` is the development database only. It is at `a2b3c4d5`, the same
  head as `main`. Nothing in this ticket changed it, and the 1 October
  acceptance runs did not connect to it.

## Security findings — remediation (source assessment at `75f2bf18`)

A static source-analysis assessment of revision
`75f2bf185781fb15112546e19f917a19a6c94a50` (not a penetration test; none of
its findings was dynamically reproduced by it) reported five findings. Each
is remediated by its own `SEC-REMED-*` ticket on its own branch, merged only
after independent review. The statuses below are evidence states, not
closures.

| Finding | Severity / confidence | Status |
|---|---|---|
| `resource-exhaustion.webhook-pool-lease` (SEC-REMED-001) | medium / medium | Source-established at `75f2bf18`. Reproduced at route level on 4 October 2026 — real ASGI app, recording pool, scripted receive stream: a connection was leased while an anonymous body was pending, and a 1-byte drip got no response after 3 s. Fixed and tested on branch `security/sec-remed-001-webhook-intake`; **pending independent review**. Not a browser or deployment test. |
| `csrf.login-session-replacement` (SEC-REMED-002) | medium / medium | Implemented on `security/sec-remed-002-login-csrf` (5 October 2026): explicit server-configured origin guard before login or cookie-authenticated mutations; JSON required for login. Focused tests 126 passed; full Jest 182 passed; TypeScript and production build passed. **Browser acceptance PENDING** (installed browsers fail to start under the tool account); **real-backend regression BLOCKED** (pytest/backend dependencies unavailable). Not fully closed; pending independent review. See `docs/sec_remed_002_login_csrf.md`. |
| `resource-exhaustion.evidence-buffering` | medium / medium | source-established; not started |
| `race.register-audit-before-state` | low / high | source-established; not started |
| `integrity.stale-recommendation-approval` | low / high | source-established; not started |

Still open after SEC-REMED-001, so they are not mistaken for fixed:

- **Webhook dedup race.** Two concurrent deliveries of the same
  (source_system, external_ref) can both pass `is_duplicate` before either
  records its dedup row. The assessment recorded it as an open question; it
  is unchanged.
- **No rate limit or concurrency cap on the public ingest endpoint** (SEC-05,
  deferred). Each anonymous request now holds no database connection, but
  still holds a coroutine and up to `WEBHOOK_MAX_BODY_BYTES` for up to
  `WEBHOOK_BODY_DEADLINE_SECONDS`.
- **Slow request headers** are bounded by the ASGI server or a gateway, not by
  the route; nothing about that changed.

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
- After Ticket B, DORA register and submissions live in Next.js. `src/dashboard/`
  is not a fallback. Do not extend it.

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
- `GET /api/v1/submissions/runs/{run_id}/package` → `compliance_lead`, `vciso`
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

Pulled from the August 2026 audit. Register KER-107 (KER-409), submissions 404,
and the frozen filing download are **done** — they are not in this list.

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
- Teardown-coverage test: assert every table with a `tenant_id` column is in
  `_teardown_seed_data`'s list (see CLAUDE.md §17)

## How to add work

If the story does not move the **register you can maintain and file**, or the
**named human decision** that updates it, it is the wrong story.

Do not implement from `KERNO_STRATEGY.md` Part F/G.
