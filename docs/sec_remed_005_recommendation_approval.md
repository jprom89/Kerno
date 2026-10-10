# SEC-REMED-005: recommendation-bound human approval

Finding: `integrity.stale-recommendation-approval`, scope SEC-REMED-005 only.
Base: `2eb3226ece24d52b87c27f886b622fbe10e315e2` (main, PR #14 merged;
SEC-REMED-001 through SEC-REMED-004 are on main). Branch:
`security/sec-remed-005-recommendation-approval`. Commits: implementation
note `670dc99`, fix `f75ccd4`, claim tenant-context hardening `0814468`,
docstrings and fixture split `9c3b097` (final code; tested SHA). Migration 028,
revision `c4d5e6f7`.

**Status, 11 October 2026:**

- **Source-established** at `75f2bf18` by the assessment, which did not
  reproduce it dynamically.
- **Reproduced** here on 10 October 2026, through the real API against
  `kerno_test` at the base code: an approval of R1 confirmed R2, and a stale R1
  screen was accepted.
- **Fixed and tested** at `9c3b097`.
- **Documentation and comment completion** (11 October 2026, after review of
  `43c4cfa`, in the commit that follows it):
  - CLAUDE.md §14 KER-303 now carries a dated supersession note;
  - the advisory-lock commentary in `recommendation_service` is made precise;
  - nothing executable changed.

  Every execution result below stays attributed to the SHA it ran at.
- **Pending independent review.** The branch is unmerged.

The finding's severity (low) and confidence (high) are unchanged.

## What the code does at `2eb3226`

A review decision names only a control. `POST /api/v1/overrides` takes
`original_control_id` and no recommendation identity, and the three readers
each pair "a decision" with "a recommendation" in their own way:

- **Coverage** takes the newest override for the control and the newest
  current recommendation independently. An approval of R1 therefore confirms
  R2 as soon as R2 replaces R1.
- **The queue** treats a recommendation as closed when any override for the
  control was created after `generated_at`. That compares the override's
  transaction-start `now()` with a Python timestamp taken after the LLM call,
  so a concurrent R1 approval can close R2.
- **Export** reads the confirmation from the coverage pass, then fetches
  "latest" again for rationale, gaps and `generated_at`. A generation that
  commits between the two reads attaches R2's prose to R1's confirmation.

## Implementation note (recorded before coding)

**Reviewed identity, supplied and persisted.** The queue already returns each
row's `recommendation_id`. The review component sends the id of the row it
rendered in the `POST /api/overrides` body, the Next.js route forwards the body
unchanged, and `POST /api/v1/overrides` requires the id (missing or malformed
is a 422). `capture_override` stores it in a new nullable
`overrides.recommendation_id` and records it in the KER-107 entry's
before-state and after-state. Tenant, actor and role still come from the
verified JWT only. Nothing at submission time looks up "the latest"
recommendation and substitutes it.

**Tenant/control compatibility.** Migration 028 adds UNIQUE
`(tenant_id, recommendation_id, control_id)` on `recommendations` and a
composite FK `(tenant_id, recommendation_id, original_control_id)` from
`overrides` to it, so the database refuses a decision that references another
tenant's recommendation or another control's. The service checks the same
things first and gives each case an explicit outcome: an id outside the tenant
(nonexistent, or another tenant's) is the same 404 as any missing entry; an id
for another control of the same tenant is a 422.

**Historical unbound overrides.** Existing rows keep `recommendation_id` NULL.
Nothing is backfilled, inferred from timestamps, attached to the latest
recommendation, deleted or rewritten, and no audit entry is touched. An
unbound decision stays in the decision history (export `decisions`, the
ledger), but it never confirms a recommendation and never closes one in the
queue. Behaviour change, stated plainly: a control whose only decisions are
unbound shows its current recommendation as unconfirmed and open until someone
reviews that recommendation. The nightly bias recalculation still reads every
override, bound or not, exactly as before.

**One relationship, three readers.** A control's current recommendation is its
newest non-superseded row, ordered `generated_at DESC, recommendation_id DESC`.
Its decision is the newest override bound to that row's id. Coverage resolves
that pair in one statement and returns the recommendation id with the status.
The queue lists a current recommendation only while no decision is bound to
it. Export takes the recommendation id from its coverage row, reads that
recommendation by id rather than "latest", and derives confirmation and
`decided_at` from the decisions it returns in the same pack. A second decision
on the same current recommendation is accepted; the newest one wins and both
stay in the history.

**Concurrency.** Approval and replacement serialise per tenant and control on a
transaction-scoped advisory lock with its own key prefix. Generation takes it
only after the LLM call has returned, immediately before superseding prior
rows; the reserved `map_control` path does the same. No lock is held across an
external request. Approval takes it before reading the reviewed recommendation
(which it also reads `FOR SHARE`) and refuses with 409 unless that
recommendation is still the current one. Lock order in both paths: control
review lock, then recommendation rows, then the tenant ledger lock inside
`append_audit_entry`.

- R1 approval commits first: generation waits, then supersedes R1 and inserts
  R2. R1 keeps its decision; R2 is unconfirmed and open.
- R2 commits first: the stale R1 approval then reads R1 as superseded and is
  refused with 409. No override row and no audit entry are written.

Neither ordering can attribute R1's approval to R2.

**Precision note (11 October 2026), correcting two phrases in the note above
without changing what was built:**

- **The key prefix.** "Its own key prefix" separates the lock's *input naming
  scheme* (`recommendation-review:<tenant>:<control>`) from the tenant ledger
  lock's (the bare tenant id). It does not separate the keys. Both inputs are
  hashed by `hashtextextended` into PostgreSQL's single bigint advisory-lock
  key space, so a collision is improbable, not impossible. If one happened, the
  two locks would be the same lock: unrelated operations would wait on each
  other, and PostgreSQL would abort one side of any deadlock that caused. No
  collision has been observed or reproduced.
- **"No lock is held across an external request"** holds only for the locks
  this operation acquires. Generation takes no advisory or row lock before or
  during the LLM call; its earlier reads hold only the ACCESS SHARE table locks
  every SELECT takes. A caller-owned transaction may already hold locks it took
  earlier.

The implementation followed this note. Four additions came out of building
and testing it, and are recorded under "What changed": the same lock in the
reserved `map_control` path, the remediation description's by-id read, the
canonical id passed to the claim, and a deterministic tie-break on the queue
page.

## What changed

**Migration 028** (`c4d5e6f7`, parent `b3c4d5e6`, additive). It adds:

- a nullable `overrides.recommendation_id UUID`;
- `uq_recommendations_tenant_recommendation_control UNIQUE (tenant_id,
  recommendation_id, control_id)`;
- `fk_overrides_reviewed_recommendation`, a foreign key from `(tenant_id,
  recommendation_id, original_control_id)` to that key;
- `ix_overrides_tenant_recommendation (tenant_id, recommendation_id,
  created_at)`.

No row is written, no historical migration is edited, and RLS on both tables
is unchanged (ENABLE + FORCE, standard policy). The downgrade drops the four
objects in reverse; it loses the bindings recorded since the upgrade, while
the decisions survive unbound.

**ORM models.** `Override` and `Recommendation` now match migrations 003, 010
and 028, including drift that predates this ticket: TEXT control columns, the
named tenants foreign key, explicit named indexes, the `is_superseded` server
default and `idx_recommendations_current`. A scoped Alembic comparison with
type and server-default comparison on reports no difference.

**Review submission.** The path runs from the UI to the ledger:

- `RecommendationList` sends `recommendation_id: item.recommendation_id`, the
  row it rendered.
- The Next.js proxy is unchanged in behaviour: it checks the origin and
  forwards the JSON body as received. Only its doc comment changed.
- `OverrideRequest.recommendation_id` is a required UUID, so a missing or
  malformed id is a 422 before the service runs.
- `capture_override` validates the id before any SQL. It sets tenant context,
  then calls `claim_recommendation_for_review` with the canonical id. The
  claim takes the review lock, reads the row tenant-scoped `FOR SHARE`, and
  refuses:
  - not this tenant's (nonexistent or another tenant's): `EntryNotFoundError`,
    the app's generic 404 `{"detail": "entry not found"}`;
  - another control: `ValueError`, a 422;
  - superseded or no longer current: `StaleRecommendationError`, a 409 whose
    detail says a newer recommendation needs review.

  Only then does it insert the override with `recommendation_id` and append
  the KER-107 entry, in the same transaction. The entry's before-state is
  `{control_id, recommendation_id, recommendation_status}`; its after-state
  adds `recommendation_id` to the existing fields.
- Every refusal raises, so `get_conn` rolls back.
- The router builds the input from the body and the verified JWT only; tenant,
  actor and role never come from the body.
- `OverrideResponse` returns `recommendation_id`.

**The review UI on a 409.** It sends nothing else. The row stays visible and is
marked replaced: its buttons are hidden, any open form closes, and an inline
notice says the decision was not recorded, with a plain link that reloads the
queue. An error toast says the same. A rejected fetch now always re-enables the
buttons, and a list-shaped 422 detail is shown readably.

**Generation.** `generate_recommendation` is split at the lock:
`_assess_control` scores and writes the prose (it may call the LLM and takes no
advisory or row lock), then the review lock is taken, then
`_replace_current_recommendation`
supersedes, inserts, and writes the decision log and the ledger. The statement
order is unchanged apart from the lock. `map_control` takes the same lock after
its LLM call, before superseding.

**Readers.**

- **Coverage** returns the current recommendation, chosen by the shared
  ordering, and the newest decision bound to it by `recommendation_id`, in one
  statement. It exposes `recommendation_id` on `CoverageControl` and on the
  coverage API item, as an additive, nullable field.
- **The queue** lists a control's current row while no decision is bound to it.
  There is no timestamp comparison. The page has a deterministic tie-break.
- **Export** reads the coverage row's recommendation by id, with a tenant
  predicate added to `_SELECT_BY_ID`. It derives status, `decided_by` and
  `decided_at` from the newest decision bound to that recommendation among the
  decisions the entry publishes, using the same
  `resolve_system_of_record_status`. If the by-id read misses, it raises.
  `ControlEntry.recommendation_id` and `DecisionEntry.recommendation_id` are
  new and nullable; `null` on a decision marks it historical and unbound.
- **Remediation** quotes the rationale of the coverage row's recommendation,
  read by id. It raises if that read misses, instead of saying no
  recommendation exists. The label no longer says "Latest".

**Unchanged and preserved:**

- the nightly bias recalculation and its override weighting;
- the RBAC matrix;
- webhook intake bounding;
- browser-origin checks on cookie mutations;
- the export route's Fetch Metadata check and cache policy;
- evidence authentication and size limits;
- the register amendment lock, stored after-state and UTC ledger timestamps;
- the TEST-SAFETY-001 guards.

## Behaviour changes and compatibility

- **Unbound decisions no longer confirm anything.** A control whose only
  decisions predate this change now shows its current recommendation as
  unconfirmed and lists it in the queue until someone reviews that
  recommendation. This applies to legacy edits and rejects too.
  - **Before:** they forced a human-confirmed gap.
  - **Now:** such a control shows the recommendation's machine status,
    labelled unconfirmed, exactly like a control nobody has reviewed.
  - **Trust Center:** the public page counts statuses, so its counts can move
    for such controls. That is the owner's call: an unbound edit or reject
    could instead cap an unreviewed recommendation at gap without confirming
    it. That alternative was not built, because it would apply an old,
    control-level decision to recommendations it never saw.
- **API contract.** `POST /api/v1/overrides` now requires `recommendation_id`.
  The legacy development-only panel (`src/dashboard/js/panel.js`) does not send
  it, so its decisions now fail closed with a 422. It was not changed, because
  NOW.md says not to extend `src/dashboard/`.
- **Additive fields:** `recommendation_id` on the override response, coverage
  items, export control entries and export decisions.

## Evidence

All database tests ran only against the approved `kerno_test`, through the
TEST-SAFETY-001 guards. `kerno_dev` was not touched. The LLM was never called:
generation used the template path or a mocked client.

### Before the fix: the new end-to-end tests against the code at `2eb3226`

`tests/integration/test_sec_remed_005_review_binding.py` drives the real app
with real JWTs; only `get_conn` is replaced, by a stand-in that commits or
rolls back as production does. The base API ignores the extra
`recommendation_id`, so the file runs unchanged against the old code. The run
below used the file as first written; `9c3b097` later split only its seed
fixture, with the same tests and assertions.

`python -m pytest tests/integration/test_sec_remed_005_review_binding.py --require-live-database -m integration -p no:cacheprovider -rfEs`
(`kerno_test` at `b3c4d5e6`) exited 1: **12 failed, 1 passed** of 13.

| Test | Before the fix |
|---|---|
| Approve R1, then generate a changed R2 | **Failed:** coverage reported R2 as human-confirmed, the original inconsistency (`assert True is False`) |
| Stale R1 screen decides after R2 (approve, edit, reject) | **Failed** ×3: each returned 201 and was recorded, not 409 |
| Another tenant's recommendation id | **Failed:** 201, not 404 |
| Another control's recommendation id | **Failed:** 201, not 422 |
| Missing, empty or malformed id | **Failed** ×3: 201, not 422 |
| Legacy unbound decision | **Failed:** it confirmed the current recommendation (`(True, 'override')`) |
| Export while R2 commits mid-export | **Failed:** the pack paired R2's rationale and gaps with R1's confirmation |
| Explicitly approve R2; all three readers agree | **Failed** on the new response field (`KeyError: 'recommendation_id'`); this test pins the new contract rather than reproducing the defect |
| A failed ledger append leaves neither decision nor event | Passed: a requirement the fix had to keep |

The concurrency file needs the new service API, so it has no before-fix run.
Both orderings it covers were already shown sequentially above: R1 approved,
then R2 generated (first row), and R2 generated, then the stale R1 decision
(second row).

### After the fix

| Command | SHA | Exit | Result |
|---|---|---|---|
| The binding file above, `kerno_test` at `c4d5e6f7` | working tree before `f75ccd4` | 0 | 13 passed |
| `python -m pytest --require-live-database -p no:cacheprovider -rfEs` | `f75ccd4` | 0 | 1,689 passed; 0 failed, errored or skipped |
| `python -m pytest --require-live-database -p no:cacheprovider -rfEs` | `0814468` | 0 | 1,695 passed; 0 failed, errored or skipped |
| Targeted set (the three SEC-REMED-005 integration files, `test_ker107_audit_ledger.py`, `test_ker401_generation.py`, `test_ker203_ai_decision_log.py`, the two migration/model unit files and `test_database_safety_processes.py`, with `--require-live-database -p no:cacheprovider -rfEs`) | `9c3b097` | 0 | 97 passed; 0 skipped |
| `python -m pytest --require-live-database -p no:cacheprovider -rfEs` | `9c3b097` | 0 | **1,695 passed**; 0 failed, errored or skipped |
| `npx jest --runInBand` (frontend) | `9c3b097` | 0 | 19 suites, **262 passed** |
| `node node_modules/typescript/bin/tsc --noEmit` | `9c3b097` | 0 | no type errors |
| `NEXT_TELEMETRY_DISABLED=1 npm run build` | `9c3b097` | 0 | production build completed |
| `npx eslint` on the four changed frontend files | `9c3b097` | 0 | no findings |

`f75ccd4` is the fix. `0814468` adds the claim's own tenant-context check and
six unit tests. `9c3b097` adds docstrings and splits two test fixtures, with no
behaviour change. The final rows ran at `9c3b097`, the code that is pushed;
the commit after it changes only this document and NOW.md. The repository's
`pytest.ini` already passes `-q`, so the commands add only `-rfEs`. Warnings were the existing `DeprecationWarning`,
`StarletteDeprecationWarning` and `InsecureKeyLengthWarning` kinds.

### Migration 028 round trip (guarded wrapper, `kerno_test` only)

| Step | Exit | Result |
|---|---|---|
| `python scripts/migrate_test_database.py upgrade head` | 0 | `b3c4d5e6` → `c4d5e6f7` |
| `python scripts/migrate_test_database.py downgrade b3c4d5e6` | 0 | `c4d5e6f7` → `b3c4d5e6`. The parity checks then failed as expected: the binding column, composite FK and binding index were absent, and only the pre-existing tenants FK and tenant index remained |
| `python scripts/migrate_test_database.py upgrade head` | 0 | back to `c4d5e6f7`; the targeted set and both full runs above ran at this head |

Only this ticket's revision was downgraded. No older domain migration was
touched.

### What the new tests prove

- **End to end, through the real API** (`test_sec_remed_005_review_binding.py`,
  13 tests): the id the queue displays is the id the decision must carry. The
  file covers:
  - R1 approved, then a changed R2: R2 is unconfirmed, open, and exported as
    `ai_unconfirmed` with R2's own rationale;
  - explicitly approving R2 makes coverage, the queue and export agree;
  - a stale R1 screen gets 409 for approve, edit and reject, and nothing is
    written;
  - another tenant's id and a nonexistent id both get the same 404, with no
    writes in either tenant;
  - another control's id gets 422;
  - a missing, empty or malformed id gets 422;
  - a legacy unbound decision stays in the export's decisions, with a null
    `recommendation_id`, and confirms nothing;
  - a failed ledger append leaves neither the decision nor its event;
  - a generation committed mid-export cannot lend its status or prose to R1's
    confirmation.
- **Concurrency, two READ COMMITTED sessions**
  (`test_sec_remed_005_review_concurrency.py`, 7 tests). Each test waits under
  a deadline until `pg_blocking_pids()` shows the interleaving under test:
  - R1 approval committed first: generation waits on the review lock, then
    replaces R1. The decision and its ledger before-state name R1, and R2 is
    unconfirmed and open.
  - R2 committed first: the R1 approval waits, then fails with
    `StaleRecommendationError`, leaving no override and no audit entry.
  - With the mocked LLM call held in flight, the generating session holds no
    advisory lock, and an approval with a 500 ms lock timeout goes through.
  - Two generations serialise and leave exactly one current row.
  - The lock of another control, or of another tenant, does not delay a
    decision.
  - A decision that times out on its own control's lock writes nothing.
  - The hash chain verifies in each ordering.
- **Schema** (`test_sec_remed_005_schema_parity.py`, 11 tests):
  - the scoped Alembic comparison finds no drift, and neither the scope nor
    the server-default check is vacuous;
  - catalog checks pin the column, the foreign keys, the candidate key, every
    index and RLS on both tables;
  - the database itself raises `ForeignKeyViolation` for a decision bound to
    another tenant's or another control's recommendation, and accepts a
    matching binding and a NULL one.
- **Migration and model units** (`test_review_binding_models.py`, 13 tests):
  028's fixed parent and membership in a single-headed chain, its exact four
  upgrade statements, its four reverse drops, that it writes no data, and that
  the models carry exactly the migration's names, columns, types and defaults.
  The historical 027 test now checks its fixed parent and membership in that
  chain, not that 027 is the head.
- **Unit tests:**
  - **Statement order:** review lock, `FOR SHARE` read, current-id read,
    insert, then ledger lock. In generation, the lock sits after the LLM call
    and before the supersede.
  - **Refusals:** every refusal happens before any write. A missing or
    malformed id, and a missing tenant, fail before any SQL.
  - **Readers:** export reads by id and takes confirmation from the entry's
    own bound decision. The remediation description is read by id, and a miss
    raises. The router maps 404, 409 and 422 correctly.
  - **Mutation checks:** the agents' in-memory scripts each changed one thing
    and confirmed that at least one test then failed. They removed or moved
    the lock, ignored the superseded flag, read "latest" instead of by id, and
    used the newest decision overall instead of the bound one.
- **Frontend.** Both files exercise the real code:
  - `recommendation-list.test.tsx`, 12 tests on the actual component:
    - approve, edit and reject post the rendered row's `recommendation_id`;
    - a 409 sends exactly one request, even after waiting;
    - after a 409, the row stays with its notice and reload link, and its
      buttons go;
    - other rows stay actionable;
    - a rejected fetch re-enables the buttons, and a list-shaped 422 detail
      reads properly.
  - `overrides-route.test.ts`, 11 tests on the actual route handler:
    - the body is forwarded once, unchanged and with `recommendation_id`, using
      the session's Bearer token;
    - statuses 201, 401, 403, 404, 409 and 422 are relayed;
    - an untrusted or missing Origin gets 403, and an unconfigured policy gets
      503, with no body read, no cookie read and no upstream call.

## Remaining limitations

- **The version bound is the recommendation's, not the decision's.** A second
  decision on a recommendation that is still current is accepted. The newest
  wins in all three readers, and both stay in the history. Refusing it would
  need a decision-version token, which the API does not have.
- **Currency is enforced by the service, not the database.** The composite FK
  pins tenant and control. "Still current at decision time" is enforced under
  the review lock and `FOR SHARE`, so a writer using raw SQL as the table owner
  could bypass it. Ticket C2 (the non-owner role) is still held.
- **Recommendation content is immutable by convention only.** No trigger stops
  an UPDATE of `rationale` or `status`. The export's by-id read relies on the
  service writing only `is_superseded`.
- **Per-transaction deadlocks remain possible.** A caller-owned transaction
  that has already ledgered can deadlock against a review of the same control.
  PostgreSQL aborts one side (40P01), and the loser writes nothing. This is the
  KER-107 limitation every ledgered service shares; it is documented in
  `recommendation_service` and not redesigned here.
- **Concurrency is proven at the service level.** The two-session evidence
  drives the services against PostgreSQL; the routes are exercised
  sequentially, through the real app.
- **Isolation levels other than READ COMMITTED are untested.** The application
  runs at READ COMMITTED.
- **Historical data is not repaired.** No existing override or ledger entry was
  read, rebound or rewritten. Coverage figures shown before this change may
  have attributed an approval to a later recommendation, and nothing here can
  show which ones did.
- **Pre-existing behaviour left unchanged:**
  - `get_conn` commits after the response is sent, so a commit failure is
    invisible to the client.
  - The Next.js proxy turns a non-JSON body or response into a 500.
  - The development-only legacy panel now fails closed, as described above.
- **The review lock key can collide, improbably.** It shares PostgreSQL's
  bigint advisory-lock key space with the tenant ledger lock, as the precision
  note above records. A collision would cost waiting, or a deadlock that
  PostgreSQL aborts on one side; it would never cost a wrong binding.
- **CLAUDE.md §14 KER-303 has been reconciled** (11 October 2026). AC-1's
  timestamp predicate and its claim that no `overrides.recommendation_id`
  column exists are kept as the historical record. A dated SEC-REMED-005
  supersession note at the top of that story now states the current
  behaviour. The contradiction recorded here earlier is closed.
- **No browser test and no deployment test were run.** Kerno is not claimed to
  be penetration-tested.

## §11 file reviews

Gates were checked with `ast`: docstrings, and function length counting every
line including the docstring. Magic numbers were checked by scanning the added
lines. The repository's standing style rule gives small private helpers and
test functions no docstring. Where a file has such helpers that predate this
ticket, they are named below as an explicit exemption, not hidden. Three
functions over 40 lines predate this ticket and were not touched by it; they
are named where they occur and recorded as a follow-up, not refactored inside
a security change.

### ✅ File 1 Review — migrations/versions/028_bind_overrides_to_reviewed_recommendations.py

**What this file does:** It lets the database record which recommendation a
human decided, and refuses a record that points at another company's or
another control's recommendation.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | what/why/how, the revision chain, and the design decisions |
| All functions have docstrings | ✅ | `upgrade`, `downgrade` |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | composite FK through `tenant_id`; no new table, RLS unchanged and re-verified |
| TenantContextMissingError raised on null/empty context | N/A | DDL only |

**Tests:** `test_review_binding_models.py` ✅ (13); schema parity ✅ (11, live
DB); round trip ✅ (upgrade, downgrade `b3c4d5e6`, upgrade).
**Open questions:** None — ready to proceed.
**Proceed to File 2?** Yes — all gates pass, no open questions.

### ✅ File 2 Review — src/models/override.py, src/models/recommendation.py

**What these files do:** They describe the two tables to the code exactly as
the database has them, including the new link.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | `Override.__repr__` only |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | metadata only; the composite FK is declared |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** the scoped Alembic comparison reports no drift ✅. The model unit
tests ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 3?** Yes — all gates pass, no open questions.

### ✅ File 3 Review — src/exceptions.py

**What this file does:** It names the error raised when someone decides a
recommendation that has since been replaced.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | the new class is documented; it is deliberately not a ValueError or RuntimeError subclass |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** exercised through the router (409) and the service.
**Open questions:** None — ready to proceed.
**Proceed to File 4?** Yes — all gates pass, no open questions.

### ✅ File 4 Review — src/services/recommendation_service.py

**What this file does:** It writes recommendations and decides which one is
current, and it makes a replacement and a human's decision on the same
control take turns.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | the lock-order decision is recorded |
| All functions have docstrings | ✅ | including the new ones: `acquire_control_review_lock`, `claim_recommendation_for_review`, `_assess_control`, `_replace_current_recommendation` |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ❌ (pre-existing) | `_build_rationale_prompt` (51) and `_record_generation` (53) are unchanged since `2eb3226`. `generate_recommendation`, already 51 lines at base, is now 29 after splitting at the lock |
| Tenant isolation rule followed (if DB file) | ✅ | every new query sets tenant context first and carries a `tenant_id` predicate; the by-id read gained one |
| TenantContextMissingError raised on null/empty context | ✅ | the claim and the lock helper raise before any SQL (unit tests) |

**Tests:** unit ✅. KER-401 and KER-203 live ✅. The concurrency file ✅ (7,
live DB).
**Open questions:** the two pre-existing over-length functions are a cleanup
item for a separate ticket.
**Proceed to File 5?** Yes — the only ❌ is pre-existing in functions this
ticket does not touch.

### ✅ File 5 Review — src/services/mapping_service.py

**What this file does:** The reserved LLM-only engine now takes the same turn
as generation before it replaces a recommendation.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | the public `map_control` is documented. Twelve small private helpers have none, all predating this ticket (standing-rule exemption) |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | unchanged: tenant context first |
| TenantContextMissingError raised on null/empty context | ✅ | unchanged |

**Tests:** unit ✅ (lock after the LLM call, before the supersede). KER-203
live ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 6?** Yes — all gates pass, no open questions.

### ✅ File 6 Review — src/services/override_service.py

**What this file does:** It records a human's decision only if it names the
recommendation they actually saw, and that recommendation is still the
current one.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | including `_validate_recommendation_id` |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | `_validate_override_input` split to stay under 40 |
| Tenant isolation rule followed (if DB file) | ✅ | tenant from the session; context set before the claim; the claim re-sets it |
| TenantContextMissingError raised on null/empty context | ✅ | unchanged |

**Tests:** unit ✅. KER-107 live ✅ (7). The binding and concurrency files ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 7?** Yes — all gates pass, no open questions.

### ✅ File 7 Review — src/api/schemas/overrides.py, src/api/routers/overrides.py

**What these files do:** The decision endpoint requires the recommendation id
and answers clearly when the decision cannot be recorded.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | the new `_capture_or_refuse` is documented. `_SessionContext.__init__` and `resolve_tenant_id` have none and predate this ticket (exemption) |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | status codes passed to `HTTPException`, as in every router |
| No function longer than 40 lines | ✅ | `create_override` stays under 40 because the refusal mapping moved into `_capture_or_refuse` |
| Tenant isolation rule followed (if DB file) | ✅ | tenant, actor and role come from the JWT only |
| TenantContextMissingError raised on null/empty context | ✅ | via the service and the app's 403 handler |

**Tests:** `test_overrides.py` ✅. `test_rbac_gates.py` ✅, with the role matrix
unchanged.
**Open questions:** None — ready to proceed.
**Proceed to File 8?** Yes — all gates pass, no open questions.

### ✅ File 8 Review — src/services/coverage_service.py, src/api/schemas/coverage.py

**What these files do:** A control counts as human-confirmed only when someone
decided the recommendation it currently shows.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | `_row_to_coverage_control` has none and predates this ticket (exemption) |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | positional row indexes, as before |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | unchanged: context first, explicit tenant filters |
| TenantContextMissingError raised on null/empty context | ✅ | unchanged |

**Tests:** unit ✅. The binding file ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 9?** Yes — all gates pass, no open questions.

### ✅ File 9 Review — src/services/export_service.py, src/api/schemas/export.py

**What these files do:** Each entry in an evidence pack describes one
recommendation, with its own confirmation, and never mixes two.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | the new and changed helpers are documented. `_collect_evidence`, `_collect_audit_extract` and `_record_export_audit_entry` have none and are unchanged since before this ticket (exemption) |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | the by-id read sets context and filters on `tenant_id` |
| TenantContextMissingError raised on null/empty context | ✅ | unchanged (session resolution) |

**Tests:** unit ✅. The binding file ✅, including the mid-export race.
**Open questions:** None — ready to proceed.
**Proceed to File 10?** Yes — all gates pass, no open questions.

### ✅ File 10 Review — src/services/remediation_service.py

**What this file does:** A remediation ticket quotes the reasoning for the gap
it is about, not a newer recommendation.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | `_build_issue_description` is now documented. `_find_control`, `_record_trigger_audit_entry` and `_insert_task` have none and predate this ticket (exemption) |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ❌ (pre-existing) | `flag_for_rereview` (41, including its docstring) is unchanged since before this ticket |
| Tenant isolation rule followed (if DB file) | ✅ | by-id read is tenant-scoped |
| TenantContextMissingError raised on null/empty context | ✅ | unchanged |

**Tests:** unit ✅ (by-id read, miss raises before Jira, label).
**Open questions:** `flag_for_rereview` is a cleanup item for a separate
ticket.
**Proceed to File 11?** Yes — the only ❌ is pre-existing in a function this
ticket does not touch.

### ✅ File 11 Review — src/api/routers/recommendations.py

**What this file does:** Its queue documentation now says what "open" means.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | docstring text only changed |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | unchanged |
| TenantContextMissingError raised on null/empty context | ✅ | unchanged |

**Tests:** `test_recommendations.py` ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 12?** Yes — all gates pass, no open questions.

### ✅ File 12 Review — frontend/components/RecommendationList.tsx, frontend/app/api/overrides/route.ts

**What these files do:** The review screen sends the id of the recommendation
on screen, and says plainly when a newer one has replaced it.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | both doc comments updated |
| All functions have docstrings | ✅ | the new helpers have doc comments; the component is documented by the module comment, as before |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | `CREATED_STATUS` and `STALE_RECOMMENDATION_STATUS` are named |
| No function longer than 40 lines | ✅ | `submitAction` and the route handler; the component's JSX render is long, as before |
| Tenant isolation rule followed (if DB file) | N/A | no database; the route's origin check runs before the body or cookie is read, unchanged |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** `recommendation-list.test.tsx` ✅ (12). `overrides-route.test.ts` ✅
(11). `mutation-origins.test.ts` ✅. Full Jest, `tsc`, build and ESLint ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 13?** Yes — all gates pass, no open questions.

### ✅ Files 13–14 Review — tests

**New backend test files:**
- `tests/integration/test_sec_remed_005_review_binding.py`
- `tests/integration/test_sec_remed_005_review_concurrency.py`
- `tests/integration/test_sec_remed_005_schema_parity.py`
- `tests/unit/models/test_review_binding_models.py`

**Updated backend test files:**
- `tests/integration/test_ker107_audit_ledger.py`
- `tests/unit/api/test_overrides.py`
- `tests/unit/api/test_rbac_gates.py`
- `tests/unit/models/test_dora_contract_relationship_model.py`
- `tests/unit/services/test_override_service.py`
- `tests/unit/services/test_recommendation_service.py`
- `tests/unit/services/test_coverage_service.py`
- `tests/unit/services/test_export_service.py`
- `tests/unit/services/test_remediation_service.py`
- `tests/unit/services/test_mapping_service.py`

**Frontend test files:** `frontend/__tests__/overrides-route.test.ts` (new) and
`frontend/__tests__/recommendation-list.test.tsx`.

**What these files do:** They prove that a decision always names, and only
ever confirms, the recommendation the reviewer saw. This holds under both
commit orderings, across tenants and controls, in all three readers, and in
the database itself.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | fixtures and non-obvious helpers are documented. Test functions and small private helpers have none, per the repository's test convention and the standing style rule (explicit exemption) |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | timeouts, relevance scores and lifetimes are module constants; SQL seed literals as in the other integration files |
| No function longer than 40 lines | ✅ | the two seed fixtures were split |
| Tenant isolation rule followed (if DB file) | ✅ | every seed and read sets `app.current_tenant_id`; cross-tenant cases are asserted, not assumed |
| TenantContextMissingError raised on null/empty context | ✅ | asserted for the claim and the lock helper |

**Open questions:** None — ready to proceed.
**Proceed?** Yes — all gates pass, no open questions.
