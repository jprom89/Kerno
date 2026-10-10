# SEC-REMED-005: recommendation-bound human approval

Finding: `integrity.stale-recommendation-approval`, scope SEC-REMED-005 only.
Base: `2eb3226ece24d52b87c27f886b622fbe10e315e2` (main, PR #14 merged;
SEC-REMED-001 through SEC-REMED-004 are on main). Branch:
`security/sec-remed-005-recommendation-approval`.

**Status, 10 October 2026:** implementation note recorded before coding.

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
