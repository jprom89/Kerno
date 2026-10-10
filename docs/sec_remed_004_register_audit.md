# SEC-REMED-004: accurate audit before-state for v1 register amendments

A v1 register amendment now ledgers the committed row it actually replaced.
It reads the entry with its own `SELECT ... FOR NO KEY UPDATE`, scoped to the
tenant. It holds that lock through the UPDATE and the ledger append in the
caller's transaction, and it records the row its UPDATE stored. Ledger
timestamps are rendered in UTC.

Base: `4f32be75673d7c00b4a4f71f3a37b1babb226912` (main, PR #13 merged).
Branch: `security/sec-remed-004-register-audit`. Fix commit:
`d16093195b03cfd38440ee7c3c1546cb39133040`. Finding:
`race.register-audit-before-state`, scope SEC-REMED-004 only. No migration.

**Status, 10 October 2026:**

- **Source-established** at `75f2bf18` by the assessment, which did not
  reproduce it dynamically.
- **Reproduced** here at the service level against PostgreSQL, using two
  independent READ COMMITTED sessions on `kerno_test` against the code at
  `4f32be7`.
- **Fixed and tested** at `d160931`.
- **Pending independent review.** The branch is unmerged.

The finding's severity (low) and confidence (high) are unchanged.

## What the code did at `4f32be7`

The code matched the finding. `update_register_entry` read the row with the
same plain SELECT the getter uses, with no lock and no tenant predicate. It
then ran an UPDATE, also without a tenant predicate, and built the ledger's
after-state from the input rather than from the stored row. Two concurrent
amendments could both read A. The second waited at its UPDATE for the first
to commit B, then overwrote B with C while recording A → C. The tenant's
ledger advisory lock is taken inside `append_audit_entry`, after the state was
captured. It kept the hash chain linear around the wrong record.

The reproduction also exposed a second, smaller distortion. A before-state
read back from PostgreSQL carried the session's time zone (`+02:00`), while
an after-state built in Python carried UTC (`+00:00`). Two ledger states of
the same instant therefore read differently.

## What changed (`src/services/dora_roi_service.py`)

- **The amendment read is its own statement.** `_SELECT_ENTRY_FOR_AMENDMENT`
  filters on `register_entry_id` and `tenant_id` and ends `FOR NO KEY UPDATE`.
  `get_register_entry`, `list_register_entries` and
  `list_active_register_entries` still use plain SELECTs and take no lock.
- **The UPDATE is tenant-scoped and reports what it stored.** It adds
  `AND tenant_id = :tenant_id` and `RETURNING` every column. The ledger's
  after-state and the function's return value are that returned row. If the
  UPDATE returns nothing, which the lock and the predicate should make
  impossible, it raises rather than ledgering a value it did not store.
- **Ledger timestamps are UTC.** `_entry_to_ledger_state` converts every
  datetime to UTC before rendering it in ISO 8601, and refuses a naive one
  with `TypeError`. Every register timestamp column is `TIMESTAMPTZ`. Create
  entries are unaffected, because their timestamps were already generated in
  UTC.
- **Unchanged:**
  - tenant context is still set before any query;
  - validation still runs before any SQL;
  - a missing or other-tenant entry still returns `None`, which the route
    maps to 404, and a malformed id is still a 404 at the route;
  - the route permissions are unchanged;
  - the service still owns no transaction;
  - `append_audit_entry`, its hashing and its locking are untouched.
- **The module docstring** now records the decision and the lock order, in
  the same form as the v2 organisation and contract services.

**Guarantee, stated narrowly.** Every successful amendment ledgers the state
it actually replaced. A stale user edit is **not** rejected: it waits for the
competing amendment to commit, then wins, with an accurate trail. That is not
optimistic-concurrency protection; rejecting a stale edit would need an
expected-`updated_at` token the API does not have.

**Lock order.** Per call, the row lock is taken before the tenant's ledger
advisory lock. Before this change the UPDATE took the same row lock before
the ledger lock, so the locking read adds no new inversion. Per transaction
that order does not hold. The advisory lock is transaction-scoped, so a
caller-owned transaction that has already ledgered can wait on a row whose
holder is queued for that lock. PostgreSQL then aborts one side with
`DeadlockDetected` (40P01); the loser writes neither row nor ledger entry,
and the retry is the caller's. Every ledgered service shares this KER-107
limitation. It is retained, documented, and tested; this ticket does not
redesign it.

**Compatibility.** A PATCH response now returns the stored row. Its
`created_at` and `updated_at` are the same instants as before, now shown in
the database session's offset, as GET already shows them, rather than in UTC.

**Historical entries.** No existing ledger entry was read, rewritten or
repaired. This change prevents a recurrence. It does not show whether any
amendment recorded before it has an accurate before-state.

## Evidence

### Before the fix: the new tests against the code at `4f32be7`

`python -m pytest tests/integration/test_sec_remed_004_register_audit.py --require-live-database -m integration`
exited 1: **3 failed, 4 passed** of 7.

| Test | Before the fix |
|---|---|
| Two concurrent amendments, A → B committed first | **Failed.** The hash chain verified, yet the second amendment recorded `State A Cloud` (A) as the state it replaced. |
| Two consecutive amendments in a `+02:00` session | **Failed.** `created_at` was ledgered as `…+02:00`, not UTC. |
| Per-transaction deadlock | **Failed** on the timestamp representation only. Its business-field checks passed: the deadlock behaviour was already the same. |
| Rollback on audit failure, tenant isolation (service and route), non-locking reads | Passed. These pin requirements the change must keep. |

### After the fix

| Command | Exit | Result |
|---|---|---|
| The new integration file, with `test_ker409_register_ledger.py` and `test_frozen_filing_download.py` | 0 | 20 passed |
| `test_dora_roi_service.py`, `test_register.py` and `test_rbac_gates.py` | 0 | 132 passed |
| `python -m pytest --require-live-database -p no:cacheprovider -rfEs` | 0 | **1,596 passed**; 0 failed, errored or skipped |

All database tests ran only against `kerno_test`, through the TEST-SAFETY-001
guards.

**How the regression is synchronised.** Session one amends A → B and holds
its transaction open. Session two starts its amendment to C on a thread, and
the main thread waits, under a deadline, until `pg_blocking_pids` reports
session two blocked by session one. Only then is session one committed. Both
implementations reach that blocked state: the old one at its UPDATE, after an
unlocked read of A, and the fixed one at its locking read. So the test never
needs both writers to pass the protected read first, which would deadlock it.

Both sessions run at READ COMMITTED in `Europe/Berlin`, with `lock_timeout`
and `statement_timeout` set, and both are closed in a `finally`. The hash
chain is verified separately before the history is checked. The history is
checked in two passes: the caller-supplied fields of every before-state and
after-state (A → B, then B → C), then every field including ids and
timestamps, against rows read back separately.

The other new tests cover:

- **Audit failure:** an append that fails rolls back the amendment, leaves the
  ledger row count unchanged, and releases the row lock.
- **Tenant isolation:** a tenant A amendment of tenant B's entry returns
  `None` and leaves B's row and both ledgers unchanged. It takes no lock on
  B's row, and the same request through the PATCH route is a 404.
- **Non-locking reads:** readers neither wait for an amendment in flight nor
  hold a row lock.
- **Per-transaction deadlock:** exactly one side aborts with 40P01 and leaves
  nothing, and the winner's before-states are accurate.

The unit tests pin the statement shapes: the tenant-scoped locking read, the
tenant-scoped UPDATE with RETURNING, a getter with no lock clause, no SQL for
invalid input, and UTC rendering with a naive timestamp refused.

## Remaining limitations

- **Stale edits** are not rejected, as described above.
- **Per-transaction deadlocks** remain possible and are the caller's to
  retry.
- **Historical ledger entries** are neither verified nor repaired.
- **Other isolation levels** are untested. The application runs at READ
  COMMITTED; at REPEATABLE READ or SERIALIZABLE a concurrent committed
  amendment would surface as a serialization failure instead.
- **Create is unchanged.** It still builds its after-state from the input,
  with UTC timestamps, as before. Only amendments were in scope.
- **The route is not exercised concurrently.** The concurrency evidence is at
  the service level against PostgreSQL. The route is exercised only
  sequentially, by the KER-409 tests and the cross-tenant check here.

Kerno is not claimed to be penetration-tested.

## §11 file reviews

### ✅ File 1 Review — src/services/dora_roi_service.py

**What this file does:** It keeps a tenant's DORA register lines and writes
down who changed each one, and from what.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | the concurrency and lock-order decision was added |
| All functions have docstrings | ✅ | checked with `ast`; `_utc_isoformat` is new |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | checked with `ast` |
| Tenant isolation rule followed (if DB file) | ✅ | `set_tenant_context` before every query, plus explicit tenant predicates on the amendment's read and UPDATE |
| TenantContextMissingError raised on null/empty context | ✅ | `_guard_tenant` first, unchanged |

**Tests:** the unit file, 21 ✅, 6 of them new; the new integration file, 7 ✅; KER-409 and frozen-filing download, 13 ✅ (live DB).
**Open questions:** None — ready to proceed.
**Proceed to File 2?** Yes — all gates pass, no open questions.

### ✅ Files 2–3 Review — tests

`tests/integration/test_sec_remed_004_register_audit.py` and
`tests/unit/services/test_dora_roi_service.py`.

**What these files do:** They prove that two people changing the same
register line at once each leave a true record of what they changed, and that
nothing else about the register changed.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | helpers and test doubles are documented. Test functions have none, as in the repository's other test files and per the standing style rule. This is named here as an explicit exemption |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | named timeouts, deadlines and states |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | every raw probe sets `app.current_tenant_id` first |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** as listed under Evidence.
**Open questions:** None — ready to proceed.
**Proceed?** Yes — all gates pass, no open questions.
