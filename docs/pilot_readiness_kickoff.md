# PILOT-READY-001: C2 runtime-role specification and controlled-pilot release gates

Issue: [jprom89/Kerno#16](https://github.com/jprom89/Kerno/issues/16). Base:
`08bddf34bbd3903c24fb4636d148928023d14e04` (origin/main, SEC-REMED-005 merged
through PR #15). Branch: `pilot/pilot-ready-001-c2-preparation`. Written
11 October 2026.

**Status: preparation only.** This document is a specification, a validation
plan and a release checklist. Nothing in it is implemented, provisioned,
deployed or accepted:

- **C2 implementation is still held** until the ticket in §5 is approved.
- **No database was connected** in preparing this document, and no test or
  migration was run.
- **No hosting, domain or budget choice is assumed.**

## 0. Outcome, scope and how to read this

**End goal (unchanged).** A sellable, maintainable DORA operational-resilience
system of record: trustworthy register data, supporting evidence, named human
decisions, historical filing integrity, and the later reporting capabilities in
the approved DORA v2 sequence (`DORA_MODEL_V2.md` §36). The controlled pilot is
an intermediate proof point, not the final product and not a replacement for
that sequence. Only risks that affect the exposed pilot workflow become release
gates; the recorded backlog does not become a prerequisite list.

**Authority.** The order is CLAUDE.md, then NOW.md, then `DORA_MODEL_V2.md`,
then an approved ticket. Perplexity's report is supplementary input only; none
of its statuses or sequencing is used here. PR #6's older mandate is not used
either; §6 records its proposed disposition.

**Labels used below.**

| Label | Meaning |
|---|---|
| *(file:line)* | Read in source at `08bddf3` |
| **[PG]** | PostgreSQL behaviour the design relies on. Not verified here; the implementation ticket must prove it on `kerno_test` |
| **[owner]** | An owner decision this document cannot make |
| **[deploy]** | Can only be verified in a deployed environment |

## 1. C2 implementation contract

### 1.1 How the code connects today

**One `DATABASE_URL` and one identity do everything.** No second connection
setting exists anywhere in `src/`, `scripts/` or `migrations/`. The documented
default identity, `kerno_dev`, owns every table
(`018_force_rls_on_tenant_tables.py:5-9`; `.env.example:17-19,60`).

| Path | Entry | Connection | Tenant context |
|---|---|---|---|
| API requests | `src/api/dependencies.py:107-114` | `ThreadedConnectionPool(1, 10)`; `get_conn` (190-202) commits after the response; `pooled_transaction` (205-223) is used by webhook ingest only | `set_tenant_context` → `SET LOCAL app.current_tenant_id` (`src/db/rls.py:46-60`); no session-level `SET` anywhere in `src/` |
| Nightly bias job | `src/scheduler/nightly_bias_recalculation.py:374-418` | fresh `psycopg2.connect(DATABASE_URL)` per transaction (383) | dummy `PLATFORM_SCHEDULER_TENANT_ID` (86) to list tenants, then per tenant |
| Decision-log prune | `src/scheduler/prune_ai_decision_log.py:51-56,133-156` | same helper | per tenant; `DELETE … RETURNING` (`ai_decision_log_service.py:95-100`) |
| DORA submission scheduler | `src/scheduler/dora_roi_submission_scheduler.py:67-128` | **no entrypoint** | needs an RLS-bypassing connection by its own docstring (2-4) |
| Alembic | `migrations/env.py:48-54,82-87` | `DATABASE_URL`, `NullPool` | n/a (all DDL) |
| Seeds | `scripts/seed_dev_tenant.py:112`, `seed_demo_evidence.py:223`, `seed_nis2_controls.py:434` | `DATABASE_URL` | the first two are gated to `KERNO_ENV=development`; `seed_nis2_controls.py` has **no** environment gate |
| Test workflow | `scripts/migrate_test_database.py`, `tests/conftest.py` | `kerno_test` (the owner) through the TEST-SAFETY-001 guard | as in the application |

**What the runtime itself needs at the database level:**

- **No owner-only statements.** `src/` contains no `ALTER`, `CREATE`, `DROP`,
  `TRUNCATE`, `GRANT`, `SET ROLE`, trigger toggling or `SET row_security`.
- **One sequence:** USAGE on `audit_log_sequence_number_seq`. The
  `audit_log.sequence_number` BIGSERIAL (`016:147`) is filled by its default
  (`src/services/audit_log.py:59-66`).
- **UPDATE privilege where row locks are taken [PG]:** `FOR SHARE` on
  `recommendations` (`recommendation_service.py:247-253`) and
  `FOR NO KEY UPDATE` on `dora_register_entries` (`dora_roi_service.py:207-215`).
- **The `vector` type,** used by the pool wrapper's cast
  (`dependencies.py:33-38,51-53`).
- **Transaction-scoped advisory locks** for the ledger and the review lock;
  PUBLIC can take these by default **[PG]**.

**Test fixtures do need owner powers, by design.** Examples:

- `ALTER TABLE … DISABLE/ENABLE TRIGGER` (`tests/conftest.py:482-485,524-527`);
- deleting append-only rows during teardown;
- seeding the global tables.

Those stay on the owner connection (§2).

### 1.2 Pre-context reads, and why FORCE alone does not fix them

These reads run before any tenant context exists. C2 must keep each one working
without leaving them open to bulk reads.

| # | Path | Read | Policy today |
|---|---|---|---|
| B1 | `POST /api/v1/auth/login` | `SELECT u.user_id, u.tenant_id, u.password_hash, u.role, u.is_active FROM users u JOIN tenants t … WHERE t.tenant_slug = :tenant_slug AND u.email = :email` (`auth_service.py:52-57`, run at 173-176 with no context) | `users`: ENABLE, not FORCE, **context-optional** (`019:95-104`). `tenants`: **no RLS** (no ENABLE in any migration) |
| B2 | `POST /api/v1/webhooks/ingest` | `SELECT tenant_id, signing_secret, is_active FROM webhook_registrations WHERE id = :id` (`webhook_service.py:85-89`, 360-365); HMAC is checked in Python (224-227) | ENABLE, not FORCE, context-optional (`022:77-90`) |
| B3 | `GET /trust-center/{slug}/status` (anonymous) | `SELECT tenant_id, trust_center_public FROM tenants WHERE tenant_slug = :tenant_slug` (`trust_center.py:73-77,99`) | none |
| B4 | Cron tenant list | `SELECT tenant_id FROM tenants WHERE is_active = true` (`nightly_bias_recalculation.py:298-300`) | none |
| — | `GET /register/windows`, `GET /submissions/windows`, ingest `control_ref` lookup | global reference tables (`dora_reporting_windows`, `dora_submission_windows`, `compliance_controls`) | none; global data, grants only |

**The context-optional policy** is:

```sql
tenant_id = NULLIF(current_setting('app.current_tenant_id', TRUE), '')::uuid
OR current_setting('app.current_tenant_id', TRUE) IS NULL
OR current_setting('app.current_tenant_id', TRUE) = ''
```

It has no `FOR` clause and no `WITH CHECK`. It is TRUE for every row whenever
no context is set. Consequences:

- **FORCE changes nothing for a non-owner role [PG].** A role that is neither
  owner nor BYPASSRLS is already subject to the policy once RLS is enabled.
  FORCE only binds the owner, and binds it to this same always-open policy.
- **Pre-context reads stay unrestricted for any role.** With no context set, a
  query sees every tenant's `password_hash` and every `signing_secret`.
  Pre-context writes are open too, because `USING` doubles as the write check
  **[PG]**.
- **The 019 docstring's reason for not forcing is doubtful.** It says FORCE
  would make login impossible (`019:10-16`, repeated in CLAUDE.md §12 KER-202
  decision 2). On the policy text, FORCE plus this policy would still admit the
  login read. Neither claim has been tested live.
- **The webhook secret's compensating control is ineffective.** Migration 022
  lists "behind RLS" as compensating for the plaintext secret (`022:19-21`), but
  RLS does not protect it today: the owner bypasses RLS, and every other role
  sees all rows before context is set.

**A bootstrap design that actually closes the gap has to do both of these:**

1. Stop giving the runtime role direct pre-context access to credential
   columns.
2. Make the `users` and `webhook_registrations` policies strict.

### 1.3 Identities

Recommended. Names are placeholders; one set per database.

| Identity | Test (`kerno_test`) | Pilot environment | Attributes | Purpose |
|---|---|---|---|---|
| **Owner / migrator** | `kerno_test` (exists, unchanged) | e.g. `kerno_owner` **[owner/deploy]** | LOGIN; no SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB or REPLICATION; owns the database and every schema object | Alembic, the helper functions, seeds and reference data, owner-only maintenance, test fixtures |
| **Runtime** | `kerno_test_app` (new) | e.g. `kerno_app` **[owner/deploy]** | LOGIN; NOSUPERUSER, NOBYPASSRLS, NOCREATEROLE, NOCREATEDB, NOREPLICATION; **zero role memberships**, including predefined `pg_*` roles; **owns no object**; CONNECT on its one database only (no TEMP) | The API process. Also the nightly job if that job is ever re-enabled |
| Prune job | not created now | later, if needed | as runtime, plus `SELECT, DELETE` on `ai_decision_log` only | Retention prune. Not needed for the pilot: the first rows become prunable 180 days after the first decision |

Design rules:

- **No group or grant-holder role.** Grants go directly to the runtime login.
  This keeps "zero memberships" true for both identities, so the guard can
  require it.
- **No `SET ROLE` design.** The runtime identity must be a real login. Making it
  reachable from the owner would need a membership in one direction or the
  other.
- **The `kerno_test` owner must not gain** CREATEROLE, any membership or any
  bypass. Acceptance tests are never run as `postgres`.

### 1.4 Runtime privilege matrix

The matrix is derived from the wired routes and jobs, not from the service-only
DORA v2 modules. Abbreviations: S = SELECT, I = INSERT, U = UPDATE,
D = DELETE; a column list in parentheses means a column-level grant.

| Table | Runtime grants | RLS / FORCE after C2 | Notes |
|---|---|---|---|
| `tenants` | S(`tenant_id`, `tenant_slug`, `trust_center_public`, `is_active`); U(`trust_center_public`) | none (unchanged) | The legacy credential columns `email` and `password_hash` (`014:34-36`) are **never** granted. No I or D. |
| `users` | **none** | ENABLE, strict policy, not FORCE (see §1.5) | Login reads it only through helper H1 |
| `webhook_registrations` | S(`id`, `tenant_id`, `source_system`, `created_at`, `is_active`); I(`id`, `tenant_id`, `source_system`, `signing_secret`); U(`signing_secret`) | ENABLE, strict policy, not FORCE | The `signing_secret` column is never selectable; ingest reads it only through helper H2 |
| `webhook_ingest_dedup` | S; I(`tenant_id`, `source_system`, `external_ref`); U(`received_at`) | E/F, unchanged | `ON CONFLICT DO UPDATE` privileges per **[PG]** |
| `context_records` | S, I | E/F | |
| `control_evidence_links` | S, I, U(the link columns written in `evidence_service.py:68-103`) | E/F, with the `EXISTS` policy | The policy subquery runs as the runtime role against `context_records` **[PG]** |
| `compliance_controls` | S | none (global) | |
| `control_crosswalks` | none | none | No live reader |
| `recommendations` | S, I, U(`is_superseded`) | E/F | U also satisfies `FOR SHARE` **[PG]** |
| `overrides` | S, I | E/F | |
| `audit_log` | **S, I** + USAGE on `audit_log_sequence_number_seq` | E/F + triggers | No U, D or TRUNCATE: those fail with "permission denied" before any trigger runs |
| `ai_decision_log` | **S, I** | E/F + triggers | No D for the API role (prune identity only); no U |
| `retrieval_bias` | S | E/F | S for the coverage calibration date (`coverage_service.py:199-200`). I and U only if the recalculation endpoint or job stays mounted (excluded from the pilot, §4.3) |
| `tenant_embeddings` | none (S only if recalculation stays) | E/F | |
| `remediation_routing_rules`, `remediation_tasks` | none while remediation is excluded; otherwise S, and S/I/U(`closed_at`, `re_review_flagged_at`) | E/F | |
| `dora_register_entries` | S, I, U(the columns in `dora_roi_service.py:175-194`) | E/F | U also satisfies `FOR NO KEY UPDATE` **[PG]** |
| `dora_reporting_windows`, `dora_submission_windows` | S | none (global) | |
| `dora_submission_runs` | S, I, U(`status`, `validation_overall_status`, `validation_issue_count`, `entry_count`, `frozen_package_json`, `updated_at`) | E/F | Exact list from `_UPDATE_RUN`, `dora_roi_submission_service.py:174-183` (insert at 163-172) |
| DORA v2 tables (6) | **none** until a routed ticket grants them | E/F | No route imports these services; fail closed |
| `alembic_version` | none | — | |

**Shared grants:**

- CONNECT on the database; no TEMP.
- USAGE on schema `public`, no CREATE.
- USAGE on schema `kerno_bootstrap` and EXECUTE on exactly the two helpers
  below.

**Never granted to the runtime:**

- TRUNCATE, which ignores RLS **[PG]**; TRIGGER; REFERENCES; MAINTAIN (PG17+);
  CREATE on any schema or database;
- ownership of any object;
- membership in any role, including predefined `pg_*` roles.

**Effective privileges, not just named grants.** The implementation must verify
these for the runtime identity **[PG]**:

- `has_table_privilege` / `has_column_privilege` / `has_sequence_privilege` /
  `has_function_privilege` / `has_schema_privilege` / `has_database_privilege`
  match the matrix exactly, in both directions.
- PUBLIC holds EXECUTE on neither helper, because new functions grant EXECUTE
  to PUBLIC by default.
- PUBLIC has no CREATE on `public`. This is the PG15+ default; the server is
  recorded as PostgreSQL 18.4 (`NOW.md:237`).
- The runtime has CONNECT only on its own database. PUBLIC CONNECT is already
  revoked on `kerno_test` (`provision_test_database.py:246-249`) but not on
  `kerno_dev` (§2.3).
- `pg_auth_members` shows no membership, and `pg_shdepend` shows no owned
  object.

**How grants are applied.** No GRANT, REVOKE or default privilege exists in any
migration today.

- **Single source.** One declarative matrix, applied by migration 029 and by
  every later table-creating migration through a shared helper.
- **Grantee.** The role named in a per-database setting `kerno.runtime_role`,
  set by the database owner with `ALTER DATABASE … SET` **[PG]**.
- **Unset setting (owner-mode environments such as current development):** the
  migration creates the helpers and the strict policies but skips grants, with
  a notice.
- **Invalid runtime role:** if the setting names a role that is the owner, or
  has elevated attributes or memberships, the migration raises.
- **No default privileges.** `ALTER DEFAULT PRIVILEGES` is not used, so a new
  table never inherits grants silently.
- **Matrix test.** A live test compares the effective privileges of the runtime
  identity with the matrix for every table, in both directions.

### 1.5 Bootstrap design

**H1 — login resolution.** It replaces `auth_service.py:52-57`, and only that
SQL constant changes.

```sql
CREATE FUNCTION kerno_bootstrap.resolve_login(p_tenant_slug text, p_email text)
RETURNS TABLE (user_id uuid, tenant_id uuid, password_hash text, role text, is_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT u.user_id, u.tenant_id, u.password_hash, u.role, u.is_active
  FROM public.users u JOIN public.tenants t ON t.tenant_id = u.tenant_id
  WHERE t.tenant_slug = p_tenant_slug AND u.email = p_email
  LIMIT 1
$$;
REVOKE ALL ON FUNCTION kerno_bootstrap.resolve_login(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION kerno_bootstrap.resolve_login(text, text) TO <runtime>;
```

- **Owner:** the owner/migrator identity.
- **Callable surface:** exact equality on the already-normalised slug and email
  (`auth_service.py:168-169`) only. No pattern matching, no lookup by email
  alone or by tenant, at most one row. An unknown slug and an unknown email
  both return an empty set, so the dummy-verify path (177-185) and the uniform
  401 are unchanged.
- **Why the hash leaves the database:** scrypt verification runs in Python
  (`auth_service.py:93-115`).
- **What it removes:** the runtime can no longer select `users` at all. It
  learns one hash only by naming the exact slug and email, so it cannot
  enumerate credentials.
- **`tenants.is_active` is still not checked by login,** as today
  (`auth_service.py:52-57`). Whether it should be is a separate decision; the
  offboarding procedure in §4.2 uses `users.is_active`.

**H2 — webhook verification material.** It replaces `webhook_service.py:85-89`.

```sql
CREATE FUNCTION kerno_bootstrap.webhook_verification(p_registration_id uuid)
RETURNS TABLE (tenant_id uuid, signing_secret text, is_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT r.tenant_id, r.signing_secret, r.is_active
  FROM public.webhook_registrations r WHERE r.id = p_registration_id
$$;
-- REVOKE … FROM PUBLIC; GRANT EXECUTE … TO <runtime>
```

- It takes the same input as today. The id is already canonicalised as a UUID
  (`webhook_service.py:348-357`).
- It returns the same three columns, so the caller changes only its SQL string.
- It exposes no listing, no lookup by tenant or source, and no other column.

**Helper requirements, both functions [PG]:**

- SECURITY DEFINER, owned by the owner/migrator identity.
- `SET search_path = pg_catalog, pg_temp`, with every object schema-qualified.
- Fixed SQL: no dynamic SQL.
- No `set_config` call, so the helper cannot leak tenant context into the
  caller.
- The `kerno_bootstrap` schema is owned by the owner, has no CREATE for anyone
  else, and PUBLIC privileges on it are revoked.

**Policies.** `users` and `webhook_registrations` change from context-optional
to strict, with explicit `WITH CHECK`:

```sql
tenant_id = NULLIF(current_setting('app.current_tenant_id', TRUE), '')::uuid
```

No context, or an empty context, then yields no rows and no error. Because the
runtime holds no grant on `users`, that policy is defence in depth there. For
`webhook_registrations`, the management endpoints, which set context first
(`webhook_service.py:144,174`; `src/api/webhooks.py:176`), keep working.

**FORCE on these two tables: recommended *not* to add it.** This deliberately
refines the backlog wording "FORCE on users and webhook_registrations"
(`NOW.md:402-403`; CLAUDE.md §17).

- **What FORCE would add:** for a non-owner runtime, FORCE adds nothing
  **[PG]**. It binds only owner sessions: migrations, seeds and fixtures. The
  owner can turn it off again (`ALTER TABLE … NO FORCE`).
- **What it would break:** the helpers execute as that owner. Under FORCE plus a
  strict policy they would see nothing before context is set, which breaks
  login and ingest.
- **What keeping FORCE would cost:** either a BYPASSRLS owner (superuser-granted,
  rejected here) or a second NOLOGIN "definer" role. That role would need
  column-scoped SELECT plus a role-targeted policy (`FOR SELECT TO <definer>
  USING (true)`), which is one more admin-created role.

*Alternative B*, if the owner prefers FORCE on all tenant tables, is that
definer-role design. It is stronger only against owner-session mistakes, and it
costs one extra role and one extra policy per table.

**`tenants`.** C2 leaves it without RLS and controls it with the column grants
in §1.4:

- the trust-center slug read (B3) and the cron list (B4) keep working;
- the visibility update keeps working;
- the legacy credential columns become unreadable.

*Residual:* the runtime can list every tenant's id and slug, though no
credentials. Adding RLS on `tenants` with helpers for slug and tenant-list
resolution is a later option, not part of minimal C2.

### 1.6 Migration, scheduler, seed and maintenance boundaries

| Activity | Identity | Change needed |
|---|---|---|
| API process | runtime | `DATABASE_URL` carries the runtime identity. **A startup identity gate** (new): outside development the app refuses to start if its connection is superuser, BYPASSRLS, CREATEROLE or CREATEDB, has a membership, or owns the database or any table in `public`. In development it warns. |
| Alembic | owner | `migrations/env.py` prefers a new `KERNO_MIGRATION_DATABASE_URL`. The app never receives the owner URL. |
| Helpers, policies, grants | owner | Migration 029 (§5) |
| Reference data (`compliance_controls`, filing windows) | owner | Owner-run load. `seed_nis2_controls.py` should gain an explicit target and environment confirmation before it is used for a pilot database. |
| Pilot tenant and user creation | owner | **New bounded script** (§4.2 G9). No tenant-registration path exists in `src/`. `seed_dev_tenant.py` is development-only, sets no slug (`seed_dev_tenant.py:25-31` versus `021:104-107`), and still writes the legacy credential columns. |
| Nightly bias job | runtime, if kept | Excluded from the pilot (§4.3). It needs only runtime grants plus the `tenants` column grant. |
| Retention prune | prune identity (later) | Not needed within 180 days of the first decision. **[owner]** decides before then. |
| DORA submission scheduler | — | Stays unwired. If it is wired later, it loops per tenant over B4 rather than using a BYPASSRLS identity. |
| Backups and restores | provider/admin | See gate G5. A logical dump run as the owner meets FORCE-RLS tables **[PG]**, so backups need provider physical backups or a BYPASSRLS/superuser dump identity. Restores must re-verify RLS, FORCE, policies and grants. |
| Test fixtures, trigger tests | owner (`kerno_test`) | Unchanged (§2) |

### 1.7 Smallest change set

1. **Migration 029** (owner, reversible): the `kerno_bootstrap` schema, H1, H2,
   the two strict policies, and grants per §1.4 to `kerno.runtime_role`.
2. **Two SQL-constant swaps** to call the helpers (`auth_service.py:52-57`,
   `webhook_service.py:85-89`). No other registration read selects
   `signing_secret`: the GET already names its columns
   (`src/api/webhooks.py:103-107`), and rotation returns only `id`.
3. **`KERNO_MIGRATION_DATABASE_URL`** in `migrations/env.py`, and the startup
   identity gate.
4. **Guard support for `kerno_test_app`** (§2.3), the runtime provisioning
   script, and the runbook update.
5. **Runtime-identity live tests** (§2.4–2.5) and the privilege-matrix test.

Nothing else changes: no other policy, no FORCE flag, no ledger code, no route
behaviour.

### 1.8 What C2 does and does not guarantee

| After C2 | True? |
|---|---|
| The application cannot disable the ledger or AI-log triggers, alter or drop policies, toggle RLS or FORCE, truncate, or run DDL | **Yes**: it is not the owner **[PG]** |
| The application cannot UPDATE or DELETE `audit_log` rows, or UPDATE `ai_decision_log` | **Yes**: no grant, a second layer under the triggers |
| The application cannot read `users` or webhook signing secrets in bulk, and cannot read the legacy `tenants` credentials | **Yes**: no grant, or a column grant; the exact-key helpers are the only path |
| Tenant isolation holds even if the application sets the wrong tenant context | **No.** `app.current_tenant_id` is a setting any session may `SET` (`rls.py:43,57-60`). RLS protects against forgotten context, not against a compromised runtime credential or injected SQL that sets another tenant's id. |
| Webhook secrets are confidential from the runtime | **No.** A compromised runtime can list registration ids under any context it sets, and then call H2. Encrypting secrets at rest stays backlog. |
| All application data is immutable | **No.** Ordinary tables stay writable within the grants. Only the ledger tables gain a privilege layer. |
| "Tamper-evident" may be extended to the AI log | **No.** CLAUDE.md §17 Ticket D still applies: no hash chain. "Database-enforced append-only for every non-owner role" becomes accurate; "tamper-resistant" still needs another layer. |

### 1.9 Preserved invariants

- **Authenticated identity.** Actor and tenant still come from the verified JWT
  (`dependencies.py:117-169`, `overrides.py:50-68`).
- **All 19 FORCE-RLS tables and their strict policies** are unchanged.
- **The KER-107 hash-chained ledger and its advisory lock** are unchanged.
- **SEC-REMED-001:** bounded webhook intake before any connection lease.
- **SEC-REMED-002:** browser-origin checks and the export Fetch Metadata gate.
- **SEC-REMED-003:** evidence authentication and limits before multipart
  parsing.
- **SEC-REMED-004:** the register amendment lock, the stored after-state and
  UTC ledger timestamps.
- **SEC-REMED-005:** recommendation-bound decisions, the review lock and the
  composite FK.
- **The DORA v2 model.** Its tables get no grant and no new access path.

## 2. Guarded validation plan

### 2.1 Two connections, two jobs

| Work | Connection | Notes |
|---|---|---|
| Migrations, including 029 and its round trip | owner (`kerno_test`), guarded wrapper, purpose `migrate` | Unchanged |
| Fixtures: seeding tenants, users, registrations, controls and windows; teardown with trigger toggles | owner (`db_connection`) | Unchanged |
| Owner-as-attacker tests (`test_audit_append_only.py:126-145`, the `test_ai_decision_log_append_only.py` and `test_ker203` trigger toggles) | owner | Unchanged; they assert owner capability by design |
| Application requests under C2 | **runtime** (`kerno_test_app`) | The real FastAPI app, with its pool built from the runtime target |
| Negative SQL probes | **runtime** | Direct statements on a runtime session |

The existing green suite, all run as owner, stays **regression evidence**. It
is not proof of the non-owner boundary.

### 2.2 What existing approvals cover

| Need | Covered today? |
|---|---|
| Owner-side DDL, grants on objects `kerno_test` owns, `GRANT CONNECT` on `kerno_test`, `ALTER DATABASE kerno_test SET kerno.runtime_role` | **Yes.** These are database- and object-owner rights; `kerno_test` gains no attribute **[PG]**. |
| Creating `kerno_test_app` and setting its password | **No.** This needs SUPERUSER or CREATEROLE, which `kerno_test` must not have. It is the owner action in §3. |
| The guard accepting a second identity | **No.** The guard requires `session_user = current_user = database owner = kerno_test` (`tests/_database_safety.py:399-418`) and compares a single endpoint (`:632-653`). A code change needs approval. |
| Confirming the new role can log in over `127.0.0.1` | **Unknown.** `pg_hba.conf` was never examined (`docs/test_database_runbook.md:239-241`). The provisioning script verifies it by logging in. |

### 2.3 Guard support for the runtime identity

Specified here; not implemented by this issue.

1. **Separate settings.** Add `KERNO_TEST_RUNTIME_DATABASE_URL` and
   `KERNO_TEST_RUNTIME_DATABASE_APPROVAL`.
   - Both or neither must be present, from the same source as the owner pair.
   - The `.env.test` allow-list grows from exactly two keys to exactly four
     (`_database_safety.py:58-60,241-266`).
   - Child-process stripping (`test_database_safety_processes.py:44-55`) and
     provisioning preflight cover the new names.
2. **Fixed identity allow-list:** `TEST_ROLE_NAME = "kerno_test"` and
   `TEST_RUNTIME_ROLE_NAME = "kerno_test_app"`.
   - The runtime URL must name the same literal loopback host, port and
     database as the owner URL.
   - Its approval must equal `kerno_test_app@<host>:<port>/kerno_test`.
3. **Runtime identity check** on the first runtime connection, after the
   workflow lock is held:
   - database `kerno_test`;
   - `session_user = current_user = kerno_test_app`;
   - database owner `kerno_test`;
   - the disposable marker present; loopback; the same port;
   - every elevated attribute false; zero memberships;
   - **owns nothing** (`pg_shdepend` owner dependencies = 0);
   - no CREATE on the database or on `public`.
4. **Same workflow lock.** The runtime session proves the guard session holds
   the TEST-SAFETY lock in the same database:

   ```sql
   SELECT count(*) FROM pg_locks
   WHERE locktype = 'advisory' AND classid = 1262834254 AND objid = 1413829460
     AND objsubid = 2 AND pid = <guard backend pid> AND granted
     AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
   ```

   It must return 1.
5. **Authorisation.** The runtime endpoint is allowed only after step 3 passes,
   and only for purpose `pytest`. The migration wrapper never authorises it.
   Every runtime connect still re-proves the lock (`:630`).
6. **`DATABASE_URL` stays the owner URL** (`:741`). The runtime URL is reachable
   only as `state.runtime_target`, never through a variable the application
   reads, so existing tests are unchanged.
7. **Redaction** covers both URLs and both passwords. Refusal messages never
   echo URL-derived values.
8. **Fail closed:**
   - runtime pair absent → runtime tests skip, or fail under
     `--require-live-database`;
   - pair present but invalid → the whole run fails at configure time;
   - identity or lock proof fails → the run stops;
   - lock lost → refused.

**Why the runtime identity cannot reach `kerno_dev` or production:**

- the database name is a fixed constant;
- the host is a literal loopback, and redirecting URL keys and libpq variables
  are refused at parse time and again at connect time;
- both role names are fixed;
- the live check requires the disposable marker and `kerno_test` as database
  owner;
- the session must see the guard's lock in its own database.

*Hygiene recommendation* **[owner]**: `kerno_dev` keeps PUBLIC CONNECT
(`.env.example:17-19`). A new cluster-wide login could therefore connect to it,
though it holds no table privilege there. Revoking `CONNECT … FROM PUBLIC` on
`kerno_dev` is the owner's call. Nothing here touches `kerno_dev`.

### 2.4 Positive tests (runtime identity, live `kerno_test`)

- **Login through H1.** A valid user gets a token. A wrong password, unknown
  slug, unknown email and inactive user each get the same 401.
  - `/me` works with no database read.
- **Register.** Create and PATCH a register entry, each with a ledger append.
  This exercises sequence USAGE and the `FOR NO KEY UPDATE` grant.
- **Submissions.** Start a run, read it, and download the frozen package
  (`filing_package_downloaded` ledger entry).
- **Evidence.** Upload `.txt` and `.pdf` files, link, unlink (a soft delete
  through U(`removed_at`)), and list.
- **Recommendations.** Generate with the LLM mocked; the review lock and
  `FOR SHARE` must work under U(`is_superseded`). Then approve, edit and reject
  through the SEC-REMED-005 binding, with ledger appends.
- **Read paths.** Coverage summary and controls; evidence-pack export with its
  ledger append.
- **Webhooks, only while they stay mounted outside the pilot.** Registration
  create and rotate under context; ingest verification through H2.
- **Ledger.** `verify_audit_chain` is valid for each tenant after the
  workflow.

### 2.5 Negative tests (runtime identity)

| Probe | Expected |
|---|---|
| `SELECT * FROM users` with no context, and with tenant context | permission denied (no grant) |
| `SELECT signing_secret FROM webhook_registrations` | permission denied (column) |
| `SELECT email, password_hash FROM tenants` | permission denied (column) |
| Webhook registration rows with no context, or empty context | 0 rows (strict policy) |
| Tenant A context reading any FORCE-RLS table's tenant B rows | 0 rows |
| `UPDATE` / `DELETE` / `TRUNCATE audit_log` | permission denied, not the trigger's "append-only" message |
| `UPDATE` / `DELETE ai_decision_log` | permission denied |
| `ALTER TABLE … DISABLE TRIGGER`, `NO FORCE`, `DISABLE ROW LEVEL SECURITY`, `CREATE`/`DROP POLICY` | must be owner |
| `CREATE TABLE public.x`, `CREATE FUNCTION kerno_bootstrap.x` | permission denied |
| `SET ROLE kerno_test` | permission denied |
| H1 with case or whitespace variants, `%`, `_`, or an email without the slug | no row |
| H1 and H2 | at most one row; `has_function_privilege('public', …, 'EXECUTE')` is false |
| Privilege-matrix test | effective privileges equal §1.4 exactly, in both directions |
| An ordinary strict-policy table on a pooled connection that reports context `''` | the existing pre-existing behaviour is documented, not changed (the pool-reset backlog item) |

The append-only tests that expect the trigger's message keep running on the
owner connection.

### 2.6 What each kind of evidence proves

| Evidence | What it proves |
|---|---|
| Real PostgreSQL runtime validation | §2.4–2.5 on `kerno_test` as `kerno_test_app`; migration 029 apply, round trip and re-apply through the guarded wrapper; the privilege matrix |
| Mocked | LLM responses (template or mocked client) and Jira, which is excluded from the pilot |
| Static | This specification, the matrix's derivation from source, and code review of the helpers |
| Deployment-only **[deploy]** | Role creation and `pg_hba` in the pilot database, TLS, platform body limits, backups and restores, alerting |

### 2.7 Minimal provisioning request

Run by the owner only, after approving the §5 ticket. The implementation ticket
writes a reviewed script, e.g. `scripts/provision_test_runtime_role.py`, which
reuses the provisioning helpers. It runs in the owner's own shell, with the
administrator password typed at a hidden prompt, and executes exactly:

```sql
CREATE ROLE kerno_test_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD '<SCRAM-SHA-256 verifier>';
```

The script must:

- refuse if `kerno_test_app` already exists, or if `kerno_test` or its marker is
  missing;
- grant no membership, in either direction;
- verify the attributes, zero memberships and zero owned objects;
- confirm a login over loopback;
- write only the two runtime settings to the gitignored settings file, without
  overwriting the existing pair.

Everything else is done by `kerno_test`, the owner, through migration 029 and
the guarded wrapper:

```sql
GRANT CONNECT ON DATABASE kerno_test TO kerno_test_app;
ALTER DATABASE kerno_test SET kerno.runtime_role = 'kerno_test_app';
```

## 3. Minimum owner approval and action

1. **Approve the §5 ticket scope** as written, or amended.
2. **Approve, in writing, a second identity `kerno_test_app`** confined to the
   already-approved disposable `kerno_test`. This mirrors the 26 September
   approval record (`NOW.md:214-220`).
3. **Run the one-time role creation** (§2.7) after the reviewed script exists.
   Claude Code does not see the administrator password.
4. **Not blocking that ticket, but required before a pilot:**
   - choose the hosting provider and region, the domain, and a budget
     **[owner]**;
   - say who creates the pilot database's owner and runtime roles (a managed
     PostgreSQL administrator, typically) **[owner/deploy]**;
   - choose the backup provider and retention **[owner]**;
   - make the partner-data handling decisions in §4.4 **[owner]**.

## 4. Controlled-pilot release gates

### 4.1 Pilot workflow in scope

An explicitly supported workflow a buyer can be shown end to end:

1. Sign in with organisation slug, email and password; sign out.
2. Maintain the DORA register (the v1 register UI): create, edit and list ICT
   third-party entries, each ledgered.
3. Upload evidence (`.txt`, `.md`, `.csv`, `.pdf`) and link or unlink it to
   controls.
4. View coverage, read-only.
5. Run "Analyse" for a recommendation, then approve, edit or reject it as a
   named human, bound to the displayed recommendation.
6. Start a submission run and download the **frozen internal JSON package**,
   which is not an official ESA/xBRL submission.
7. Export the evidence pack.

### 4.2 Gate checklist

Status values:

- **Evidenced** — already shown by recorded evidence at a named SHA.
- **Validate** — needs a check, with no code change.
- **Bounded** — needs a small, separately approved implementation.
- **Owner** — needs a decision.

| # | Gate | Status | What is already there, and what is required |
|---|---|---|---|
| G1 | **C2 runtime identity** in the pilot database | Bounded + [deploy] | §5 ticket. The pilot runs only as a non-owner runtime identity, enforced by the startup gate. |
| G2 | **HTTPS and origins** | Evidenced in code; Validate [deploy]; Bounded (headers) | **In code:** startup refuses placeholder `ALLOWED_ORIGINS` outside development (`app.py:63-87`); empty origins fail closed (90-99); Next.js mutations require `KERNO_TRUSTED_ORIGINS` (503 otherwise, `lib/csrf.ts:42-62`); the session cookie is `secure` only when `NODE_ENV=production` (`auth/login/route.ts:41-47`). **Validate on the host:** HTTPS on both origins; `KERNO_ENV` ≠ development; `KERNO_ENABLE_DOCS` unset; exact `ALLOWED_ORIGINS`, `FRONTEND_URL`, `KERNO_TRUSTED_ORIGINS`; the cookie observed as Secure. **Bounded:** there are no security headers (`next.config.ts` is empty; no HSTS or CSP), so add them or confirm the platform sets them. The host and domain are **[owner]**. |
| G3 | **Release and dependency reproducibility** | Bounded | `pyproject.toml:37` requires `pypdf`, but **`uv.lock` has no `pypdf` entry**. `uv.lock` pins fastapi 0.139.0 while the recorded runtime has 0.138.1 (CLAUDE.md §17). There is no Dockerfile, CI or deploy manifest, and no pinned Python version (`pyproject.toml:9` `>=3.11`). **Required:** regenerate and commit `uv.lock`; a recorded build recipe installing from the lock on a pinned Python; and the release SHA recorded with the deployment. The frontend `package-lock.json` already pins exactly (next 16.2.10). |
| G4 | **Applied schema, including migration 028** (and 029 after C2) | Evidenced on `kerno_test`; Validate [deploy] | 028 apply and round trip on `kerno_test` are recorded in `docs/sec_remed_005_recommendation_approval.md`, with the final full run at `9c3b097` at head `c4d5e6f7`. **Validate in the pilot database:** a single head equal to the release SHA's head; a read-only catalog check of RLS/FORCE flags, policies, triggers and the runtime privilege matrix. |
| G5 | **Backup and isolated restore** | Owner + Validate | Nothing exists in the repo. **Required:** a backup method and schedule; one restore into an isolated database; then, on the restore, verify the migration head, RLS/FORCE and grants, `verify_audit_chain` for every tenant, and a frozen package byte-identical to the original download. Logical dumps run as the owner meet FORCE-RLS tables **[PG]**: use provider physical backups or a dedicated BYPASSRLS/superuser dump identity, never the runtime. |
| G6 | **Evidence upload limits** on the exposed path | Evidenced (size); Bounded (rest) | **Evidenced:** authentication before body, total/file/part limits, and a counted body (SEC-REMED-003, `evidence_upload_intake.py:86-232`). **Bounded:** a reception deadline (none today, unlike the webhook's); an upload concurrency cap; PDF page/decoded-output/time limits (`evidence_intake.py:123-135` reads every page); extraction moved before the pool lease (today it runs after `get_conn`, `evidence.py:146,159`); and the PDF error text kept out of the 422 detail (`evidence_intake.py:134`). **Validate [deploy]:** the hosting body limit; Vercel's documented 4.5 MB would be below the 10 MiB limit (`NOW.md:324-326`). |
| G7 | **Login abuse and pool exhaustion** | Bounded | Login is anonymous, has no rate limit, and runs scrypt (~16 MiB, `constants.py:296-301`) while holding a pooled connection (`auth_service.py:173-187`; pool max 10, `dependencies.py:113`). **Required:** a login rate limit keyed on the real client address (requests arrive via the Next.js server, so the client address must be forwarded and trusted explicitly), and pool-exhaustion behaviour that is a controlled 503, not a 500. |
| G8 | **LLM path** | Owner + Bounded | "Analyse" sends each linked record's title, source system and the first 600 characters of its body to Mistral's EU endpoint, without anonymisation (`recommendation_service.py:654-718`; `llm_client.py:52,73`). With `KERNO_LLM_MODEL` unset, the template rationale is used and nothing leaves (`recommendation_service.py:592-594`). **[owner]:** LLM on or off for partner data (§4.4). **Bounded if on:** an LLM client timeout (none configured, `llm_client.py:73`). Holding the pool connection across the call stays backlog, mitigated by the 10/min limit. |
| G9 | **Pilot tenant and user provisioning** | Bounded | No tenant-registration path exists, and the development seed sets no slug. **Required:** an owner-run, auditable script that creates one tenant with an explicit slug and named users with scrypt hashes and roles, without writing the legacy `tenants.email/password_hash`. **Offboarding procedure:** set `users.is_active = false`; `tenants.is_active` is not checked by login. |
| G10 | **Excluded surfaces unreachable** | Bounded | Every router is mounted in every environment (`app.py:191-213`). **Required:** a pilot serving profile that does not mount the excluded routers (§4.3), with a test asserting their paths return 404 under it, and the excluded UI controls removed (the Recalculate button). |
| G11 | **Operational alerts** | Bounded + Owner | There is no health endpoint (`GET /` is a redirect or static JSON, `app.py:215-231`), no logging configuration (`LOG_LEVEL` is read by nothing), and no monitoring hook. **Required:** an unauthenticated health check that touches the database without tenant data; error logging with the existing correlation IDs; and uptime plus error-rate alerts to the owner. The tooling is **[owner]**. |
| G12 | **Filing language** | Bounded | The UI says "Download filing" and shows internal validation status (`FilingDownloadButton.tsx:81`, `SubmissionRunView.tsx:51-61`). **Required:** copy stating that this is Kerno's internal frozen package and not an official ESA/xBRL submission, plus the same wording in demo material (NOW.md "Do not claim"). |
| G13 | **Filing-download proxy gate** | Bounded (small) | The Next.js package proxy has no `Sec-Fetch-Site` gate, though the backend writes a ledger entry on success (`submissions/runs/[runId]/package/route.ts:16-31`; `submissions.py:100-102`). Mirror the export route's gate. |
| G14 | **Synthetic browser-to-database walkthrough** before real data | Validate [deploy] | On the deployed pilot stack, with two synthetic tenants, run every §4.1 step through a browser. Then confirm: the ledger entries and chain per tenant; tenant B sees none of tenant A's data; the excluded paths return 404; the session cookie is Secure. This is recorded with the release SHA. |

**Accepted for the pilot and documented, not gates:**

- 24-hour JWTs with no revocation. *Mitigation:* rotating `KERNO_JWT_SECRET`
  revokes every token.
- Unpaginated lists at pilot data volume.
- The connection held across the LLM call (10/min).
- `get_conn` committing after the response.
- The KER-107 per-transaction deadlock limitation.

### 4.3 Excluded from the pilot

Exclusion is enforced on the serving path (G10), not by hiding UI.

| Surface | Why excluded |
|---|---|
| `/api/v1/remediation/*` (Jira) | Outside the pilot workflow. Sends rationale to Jira with global credentials. |
| `/api/v1/webhooks/*` (registration, rotation, ingest) | Outside the pilot workflow. Plaintext secrets. Anonymous ingest has no rate limit. |
| `/trust-center/{slug}/status`, `/api/v1/trust-center/visibility` | Not part of the pilot ("no Trust Center polish", NOW.md). Anonymous surface. |
| `/api/v1/ai-decisions` | Not used by the UI; unpaginated |
| `/api/v1/panel/*` | Legacy dashboard consumer only |
| `/api/v1/scheduler/run-recalculation` and the nightly bias job | Reserved learning-loop machinery (NOW.md "Do not claim") |
| `/api/v1/register/windows` | Anonymous; no UI caller |
| `/docs`, `/redoc`, `/openapi.json`; legacy `/dashboard` | Already off outside development unless `KERNO_ENABLE_DOCS=1` (`app.py:113-125,169-175,262-268`). Validate that it is unset. |
| Retention prune job | Not needed during the first 180 days; decide before then |
| DORA v2 organisations/contracts/hierarchy, import, projection, validation, xBRL, field provenance | No API today, and they stay in their `DORA_MODEL_V2.md` §36 order (§4.5) |

### 4.4 Partner-data handling decisions (owner, before real data)

None of these is resolved here, and no partner document was collected:

- **Access.** Who can reach the pilot database, backups and logs. Which
  identities exist (the owner, the runtime, the provider administrator).
- **Retention and deletion.**
  - Evidence text is kept indefinitely in `context_records.body`; originals
    are not stored (`evidence_intake.py:11-13`).
  - Ledger entries are append-only and kept forever.
  - `ai_decision_log` is kept for 180 days.
  - Decide what happens to each at pilot end, including in backups.
- **External services.**
  - **Mistral** (EU endpoint) receives evidence excerpts when Analyse runs
    (G8). Choosing an EU endpoint does not by itself settle the processing
    terms, subprocessor listing or retention at the provider.
  - The **hosting provider and its subprocessors** need the same decision.
  - **Jira** is excluded.
- **Isolation claims.** RLS is a correctness control inside one database, not a
  data-handling guarantee (§1.8). Whether a dedicated database or project per
  partner is required is **[owner]**.

### 4.5 Kept in the DORA v2 sequence (not pilot work)

- **Official reporting.** Projection (V2-008), versioned validation (V2-009) and
  the official reporting package (V2-010) follow the mandatory real-data
  checkpoint (`DORA_MODEL_V2.md` §36–§37).
- **Data entry.** Import staging and provenance is V2-004; field-level evidence
  provenance is V2-011.
- **Next domain slice.** Contract costs (§7.4) remains the next DORA domain
  slice listed in NOW.md and is not implemented here.
- **Frozen package.** The current frozen JSON stays "valid transitional
  infrastructure" (`DORA_MODEL_V2.md` §29). It must not be presented as
  regulator-accepted output.

## 5. Next implementation ticket: acceptance criteria

**Proposed:** PILOT-READY-002, "C2 runtime role". It requires explicit approval
before work starts.

1. **Migration 029** (`d5e6f7g8` style id, parent `c4d5e6f7`, reversible),
   applied as the owner:
   - It creates the `kerno_bootstrap` schema and H1 and H2 exactly as in §1.5:
     SECURITY DEFINER, pinned `search_path`, schema-qualified, exact match, at
     most one row, EXECUTE revoked from PUBLIC.
   - It replaces the `users` and `webhook_registrations` policies with the
     strict `NULLIF` form, with `USING` and `WITH CHECK`, without FORCE.
   - It applies §1.4 to the role named in `kerno.runtime_role`. It skips grants
     with a notice when the setting is unset, and raises when the named role is
     the owner or has elevated attributes or memberships.
   - It changes no other policy, no FORCE flag and no table. Its downgrade
     restores the 019 and 022 policies and removes the helpers, the schema and
     every grant.
2. **Code**:
   - `auth_service` and `webhook_service` call the helpers;
   - behaviour is otherwise unchanged, including the uniform 401s and dummy
     verification;
   - no runtime code selects `webhook_registrations.signing_secret` or reads
     `users` directly.
3. **Configuration**:
   - `migrations/env.py` prefers `KERNO_MIGRATION_DATABASE_URL`;
   - the API startup identity gate refuses an owner, elevated or member
     identity outside development, and warns in development;
   - `.env.example` documents both URLs, with the owner/runtime split for
     development.
4. **Test guard** per §2.3:
   - fixed `kerno_test_app`;
   - the same endpoint apart from the user;
   - the runtime identity check, including owns-nothing;
   - the same-lock proof;
   - purpose `pytest` only;
   - `DATABASE_URL` remains the owner;
   - offline unit tests for every refusal;
   - runbook updated.
5. **Runtime provisioning script** per §2.7, with offline tests. It is run by
   the owner only, after written approval.
6. **Live evidence on `kerno_test`**, all through the guards:
   - 029 upgrade, downgrade and upgrade through the wrapper;
   - §2.4 and §2.5 on the runtime identity;
   - the privilege-matrix test in both directions;
   - the full suite with `--require-live-database` at the tested SHA, with
     **zero skips** including the runtime tests;
   - `kerno_dev` untouched.
7. **Preserved:** every invariant in §1.9. The existing owner-role suite stays
   green. No DORA v2 table gains access.
8. **Documentation:**
   - CLAUDE.md §17 C2 row and NOW.md updated after merge;
   - the C2 decision record: no FORCE on the two bootstrap tables, and why;
   - the claims in §1.8 adopted verbatim.

**Out of scope for that ticket:** G2–G14, `tenants` RLS, webhook secret
encryption, pool reset, the prune identity, the DORA submission scheduler, and
any deployment.

**Later bounded tickets.** Each is proposed and each needs its own approval:

- serving profile and exclusions (G10);
- intake and login limits (G6, G7);
- reproducibility (G3);
- operations and health (G11);
- pilot tenant provisioning (G9);
- filing language and proxy gate (G12, G13);
- an LLM timeout if the LLM stays on (G8).

Validation and owner items: G2, G4, G5 and G14, and the §4.4 decisions.

## 6. PR #6 disposition

[jprom89/Kerno#6](https://github.com/jprom89/Kerno/pull/6), "Keep Kerno; name
the first product Kerno Register". Verified 11 October 2026:

- open, draft, not mergeable with current main;
- head `000cd0c753d5a27580abc68f74eaddcf4134663e`, base `5bd6552`;
- 5 files, +125/−18.

It mixes two concerns:

- **(A) Naming.**
  - The company stays Kerno, and the first product is "Kerno Register".
  - The page title and description change in `frontend/app/layout.tsx`.
  - The login aria-label, heading and subtitle change in
    `frontend/app/login/page.tsx`.
  - The navigation wordmark changes in `frontend/components/NavHeader.tsx`.
  - **None of this text is on main.** Main still shows "Kerno Compliance
    Copilot" and "NIS2/DORA control coverage dashboard".
- **(B) Older architecture and priority instructions.**
  - These are substantial additions to NOW.md and CLAUDE.md: a "Register-first"
    mandate, separate FinancialEntity/ICTProvider masters, and a combined
    ICTServiceRelationship junction.
  - They also add a four-increment sequence ending in the ITS compiler, ESA
    checks and xBRL, plus a template-mapping prerequisite before migrations.

**Proposed disposition. Not yet acted on; PR #6 remains open:**

- **Supersede it; do not merge it as-is.** Its (B) content is overtaken by
  `DORA_MODEL_V2.md`, which became authoritative through DORA-V2-000 the same
  day, about 1.5 hours after PR #6's last commit:
  - a single `dora_organizations` with roles (§5);
  - service arrangements and usages instead of one junction (§9, §13);
  - the §36 sequence with its mandatory real-data checkpoint;
  - no architecture built around "116 checks" (§28).
- **Keep the naming intent (Kerno / Kerno Register) for separate
  carry-forward.** Those UI changes are **not** on main.
- **Any branding change** should be a small, separately approved PR from
  current main, containing only the still-needed naming strings and any
  necessary tests or documentation.
- **Technical identifiers stay as they are:** `KERNO_*` variables, the
  `kerno_session` cookie, package names (`pyproject.toml` `kerno`), and
  historical ticket IDs. PR #6's net diff touches none of them; its reverted
  first commit had renamed them.
- **It does not block C2 preparation.** PR #6 is not an unfinished security or
  DORA prerequisite: it contains documentation and three UI strings, and
  touches no auth, RLS, migration, service or test code.

This task does not close PR #6, delete its branch, open a replacement, or
implement branding.

## 7. Status reconciliation

**Verified merges, recorded in NOW.md by this change:**

- **DORA-V2-002B** merged via PR #10 at `75f2bf18`, 3 October 2026 (reviewed
  head `778153fb`). This is also the revision the security assessment analysed.
- **SEC-REMED-005** merged via PR #15 at `08bddf34`, 10 October 2026 UTC
  (reviewed head `646e8ec2`).

The execution totals stay attributed to their tested SHAs.

**Stale lines noticed and deliberately not changed** (outside the narrow
correction):

- `NOW.md:282-284` says `kerno_dev` is at `a2b3c4d5`, "the same head as `main`".
  Main has been at `c4d5e6f7` since PR #15, and at `b3c4d5e6` since PR #10.
  `kerno_dev`'s actual revision was not checked.
- The four SEC-REMED docs each still say "pending independent review"
  (for example `docs/sec_remed_005_recommendation_approval.md:26`).
- Several migration and model docstrings describe `tenants` as having RLS
  (`021:28-30`, `trust_center.py:20-22`, `nightly_bias_recalculation.py:82-85`),
  but no migration enables it.
- `004:9-10` recommends an INSERT-only production grant on `audit_log`. The
  runtime also SELECTs it, so §1.4 grants S and I.

## 8. Local `AGENTS.md` (untracked; reported, not changed)

The owner checkout's untracked `AGENTS.md` presents itself as the constitution
("AGENTS.md — this file") and has the same version header as CLAUDE.md. It
differs from tracked CLAUDE.md in ways relevant here:

- **SEC-REMED-005 supersession.** It lacks the dated note in §14 KER-303, so it
  still presents the timestamp-based queue predicate as current. That
  contradicts migration 028 and NOW.md.
- **SEC-REMED-002 note.** It lacks the Vercel `KERNO_API_URL` /
  `KERNO_TRUSTED_ORIGINS` deployment note in §14.

Its C2 text is identical to CLAUDE.md's, and neither file has pilot text. An
agent reading `AGENTS.md` instead of CLAUDE.md would therefore follow
superseded KER-303 instructions. Refreshing that local file is the owner's
call.

## 9. Sources read

- **Authority:** `CLAUDE.md` (§0, §3, §7, §11, §12 KER-202/201, §13
  KER-203/204/205, §14, §15, §17), `NOW.md`, `DORA_MODEL_V2.md` (§1–§5, §7, §9,
  §12–§17, §21–§24, §27–§37), `docs/test_database_runbook.md`.
- **Source:** `src/api/dependencies.py`, `src/api/app.py`, `src/db/rls.py`,
  `src/services/{auth,webhook,audit_log,recommendation,coverage,export,
  evidence_intake,evidence_upload_intake,dora_roi,dora_roi_submission,
  ai_decision_log,llm_client,jira_client}*`, `src/api/{routers/*,webhooks,
  trust_center}.py`, `src/scheduler/*`.
- **Migrations:** `migrations/versions/001`–`028`, `migrations/env.py`.
- **Scripts and tests:** `scripts/*`, `tests/_database_safety.py`,
  `tests/conftest.py`, and the safety unit tests.
- **Frontend:** `frontend/app/**`, `frontend/lib/*`, `frontend/next.config.ts`.
- **Build and configuration:** `pyproject.toml`, `uv.lock`, `.env.example`.
- **PR #6:** branch `origin/cursor/rename-grunnbok-971b`, diff against
  `5bd6552`.
- **Not read:** `.env`, `.env.test` and `.env.local`.

Every statement about PostgreSQL behaviour is marked **[PG]** and is to be
proven in the implementation ticket.
