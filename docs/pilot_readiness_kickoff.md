# PILOT-READY-001: C2 runtime-role specification and controlled-pilot release gates

Issue: [jprom89/Kerno#16](https://github.com/jprom89/Kerno/issues/16). Base:
`08bddf34bbd3903c24fb4636d148928023d14e04` (origin/main, SEC-REMED-005 merged
through PR #15). Branch: `pilot/pilot-ready-001-c2-preparation`. First written
11 October 2026 at `78a88e9`, then corrected the same day after review.

**Status: preparation only.** This document is a specification, a validation
plan and a release checklist. Nothing in it is implemented, provisioned,
deployed or accepted:

- **The PILOT-READY-002 contract in §5 is proposed, not approved.**
- **C2 implementation is still held.**
- **No database was connected** in preparing this document, and no test or
  migration was run.
- **No hosting, domain or budget choice is assumed.**
- **Every risk acceptance, pilot exclusion and exception below is labelled
  proposed until the owner approves it.**

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
| **Proposed** | Not approved. Needs an explicit owner decision before it takes effect |

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
without leaving the tables open to bulk reads.

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

**What the design in §1.5 does about it.** It removes the runtime role's
*direct* table access to credential columns and makes the two policies strict.
It does **not** stop the runtime from obtaining credential material: the helpers
that replace direct access still return it, one exactly-keyed row per call.
§1.5 and §1.8 state that residual precisely; accepting it is an owner decision.

### 1.3 Identities

Recommended. Names are placeholders; one set per database.

| Identity | Test (`kerno_test`) | Pilot environment | Attributes | Purpose |
|---|---|---|---|---|
| **Owner / migrator** | `kerno_test` (exists, unchanged) | e.g. `kerno_owner` **[owner/deploy]** | LOGIN; no SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB or REPLICATION; owns the database and every schema object | Alembic, the helper functions, the grant procedure, seeds and reference data, owner-only maintenance, test fixtures |
| **Runtime** | `kerno_test_app` (new) | e.g. `kerno_app` **[owner/deploy]** | LOGIN; NOSUPERUSER, NOBYPASSRLS, NOCREATEROLE, NOCREATEDB, NOREPLICATION; **zero role memberships**, including predefined `pg_*` roles; **owns no object**; CONNECT on its one database only (no TEMP) | The API process. Also the nightly job, if that job is ever re-enabled |
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

### 1.4 Runtime privilege matrix, by serving profile

**The rule:** the runtime's effective privileges must cover every route that is
mounted, and nothing more. Today every router is mounted in every environment
(`src/api/app.py:191-213`), so the grants depend on which routers are served.

| Profile | Routers mounted | Used by |
|---|---|---|
| **Full** | Every router mounted today | PILOT-READY-002 (C2). No mounted route loses a grant, so "no route behaviour change" is true |
| **Pilot** | Only the routers the supported pilot workflow uses (§4.1) | Applied only together with, or after, the serving profile that unmounts the other routers (gate G10). **Never before**, because applying pilot grants while the other routers are still mounted would break them silently |

**Ordering:**

1. **C2** (PILOT-READY-002) ships the **full** profile and is tested with every
   router mounted.
2. **The serving-profile ticket** (G10) introduces the pilot router set and
   applies the **pilot** grant set in the same change. It also adds a startup
   check that the app's serving profile matches the database's recorded
   runtime profile (§1.4, "How grants are applied").
3. **A pilot deployment goes live only after both.** The owner may combine the
   two tickets; the pilot grant set must not land before the serving profile.

Abbreviations: S = SELECT, I = INSERT, U = UPDATE, D = DELETE; a column list in
parentheses means a column-level grant.

| Table | Pilot profile | Added only in the full profile (for features excluded from the pilot) | RLS / FORCE after C2 |
|---|---|---|---|
| `tenants` | **none** | S(`tenant_id`, `tenant_slug`, `trust_center_public`, `is_active`); U(`trust_center_public`): Trust Center (B3) and nightly job (B4) | none (unchanged) |
| `users` | none | none | ENABLE, strict policy, not FORCE (**proposed exception**, §1.5) |
| `webhook_registrations` | none | S(`id`, `tenant_id`, `source_system`, `created_at`, `is_active`); I(`id`, `tenant_id`, `source_system`, `signing_secret`); U(`signing_secret`) | ENABLE, strict policy, not FORCE (**proposed exception**) |
| `webhook_ingest_dedup` | none | S; I(`tenant_id`, `source_system`, `external_ref`); U(`received_at`) | E/F, unchanged |
| `context_records` | S, I | — | E/F |
| `control_evidence_links` | S, I, U(the link columns written in `evidence_service.py:68-103`) | — | E/F, with the `EXISTS` policy; its subquery runs as the runtime role against `context_records` **[PG]** |
| `compliance_controls` | S | — | none (global) |
| `control_crosswalks` | none | none | none (no live reader) |
| `recommendations` | S, I, U(`is_superseded`) | — | E/F. The U grant also satisfies `FOR SHARE` **[PG]** |
| `overrides` | S, I | — | E/F |
| `audit_log` | S, I + USAGE on `audit_log_sequence_number_seq` | — | E/F + triggers. No U, D or TRUNCATE: those fail with "permission denied" before any trigger runs |
| `ai_decision_log` | I (the insert has no `RETURNING`, `ai_decision_log_service.py:65-71`) | S: `GET /api/v1/ai-decisions` | E/F + triggers. No U, and no D (that belongs to a later prune identity) |
| `retrieval_bias` | S (the coverage summary's calibration date, `coverage_service.py:199-200`) | I, U(`bias_vector`, `override_count`, `last_recalculated_at`): recalculation endpoint and job | E/F |
| `tenant_embeddings` | none | S: recalculation | E/F |
| `remediation_routing_rules` | none | S | E/F |
| `remediation_tasks` | none | S, I, U(`closed_at`, `re_review_flagged_at`) | E/F |
| `dora_register_entries` | S, I, U(the columns in `dora_roi_service.py:175-194`) | — | E/F. The U grant also satisfies `FOR NO KEY UPDATE` **[PG]** |
| `dora_reporting_windows` | none | S: `GET /api/v1/register/windows` | none (global) |
| `dora_submission_windows` | S | — | none (global) |
| `dora_submission_runs` | S, I, U(`status`, `validation_overall_status`, `validation_issue_count`, `entry_count`, `frozen_package_json`, `updated_at`), the exact list from `_UPDATE_RUN` (`dora_roi_submission_service.py:174-183`; insert at 163-172) | — | E/F |
| DORA v2 tables (6) | none | none | E/F. No route imports these services; fail closed |
| `alembic_version` | none | none | — |

**Grants shared by both profiles, plus the full-profile extra:**

- CONNECT on the database; no TEMP.
- USAGE on schema `public`, no CREATE.
- USAGE on schema `kerno_bootstrap` and EXECUTE on H1.
- **Full profile only:** EXECUTE on H2.

**Tenant-isolation boundary of the `tenants` access the full profile
retains.** `tenants` has no RLS. With full-profile grants, the runtime can read
every tenant's id, slug, visibility flag and active flag, and can set *any*
tenant's `trust_center_public`. The only thing that confines the visibility
update to the caller's tenant is the application's
`WHERE tenant_id = :tenant_id` with the JWT tenant (`trust_center.py:79-84,
123-127`). That is application-enforced, not database-enforced. The pilot
profile grants nothing on `tenants`: login reaches it only inside H1, as the
owner.

**Never granted to the runtime, in either profile:**

- TRUNCATE, which ignores RLS **[PG]**; TRIGGER; REFERENCES; MAINTAIN (PG17+);
  CREATE on any schema or database;
- ownership of any object;
- membership in any role, including predefined `pg_*` roles.

**Effective privileges, not just named grants.** These checks are specified
in §2.5. They inspect the runtime role's effective privileges, which include
anything granted to PUBLIC, and separately read the ACLs for PUBLIC entries.
PUBLIC is the universal ACL grantee, not a role that can log in.

**How grants are applied.** No GRANT, REVOKE or default privilege exists in any
migration today.

- **Migration 029 (owner)** creates an owner-only procedure,
  `kerno_bootstrap.apply_runtime_grants(p_role name, p_profile text)`. It
  implements the two columns of the matrix above.
  - It refuses a role that does not exist, cannot log in, is the owner, has
    elevated attributes or memberships, or owns objects.
  - It refuses any profile other than `full` or `pilot`.
  - EXECUTE on it is revoked from PUBLIC. The migration itself grants nothing
    to anyone.
- **A separate guarded owner operation applies the grants.** On `kerno_test`
  this is the new wrapper subcommand `grant-runtime` (§2.7, step 5); in the
  pilot environment it is an owner-run step. It records the role and profile
  as database settings, `kerno.runtime_role` and `kerno.runtime_profile`, set
  with `ALTER DATABASE … SET` **[PG]**.
- **Later table-creating migrations** call the procedure when those settings
  are present. A new session reads database-level settings **[PG]**.
- **No default privileges.** `ALTER DEFAULT PRIVILEGES` is not used, so a new
  table never inherits grants silently.
- **Per-profile matrix tests** compare effective privileges with the matrix in
  both directions.

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
- **Interface:** exact equality on the already-normalised slug and email
  (`auth_service.py:168-169`); no pattern matching; no lookup by email alone or
  by tenant; at most one row. The column types match `users` exactly
  (`019:71-80`).
- **Unknown inputs:** an unknown slug and an unknown email both return an empty
  set, so the dummy-verify path (177-185) and the uniform 401 are unchanged.
- **Why the hash leaves the database:** scrypt verification runs in Python
  (`auth_service.py:93-115`).

**What H1 guarantees, and what it does not:**

- **Guaranteed:** the runtime has **no direct access to `users`** (no grant).
  It cannot select the table, list accounts or match emails by pattern. It can
  obtain a row only by supplying an exact slug and email pair to H1. One call
  returns at most one row, but one statement can call H1 once per row of a
  list of pairs the caller supplies.
- **Not guaranteed:** H1's own output stays available to the runtime, one row
  per call. For any slug and email it supplies, H1 returns that account's id,
  tenant, role, active flag and **password hash**. The runtime can call it
  repeatedly, and nothing in the database limits or records those calls.
- **Consequence:** a compromised runtime credential, or injected SQL running as
  the runtime, can collect the hashes of every account it can name. Slugs are
  not secret, and emails can be known or guessed. scrypt (`constants.py:296-314`)
  slows offline cracking but does not prevent it.
- **Residual risk, owner decision (proposed for acceptance):** accept this for
  the pilot, or require a design that never returns the hash, such as
  verification inside the database. That alternative is outside this
  specification and is not designed here.
- **`tenants.is_active` is still not checked by login,** as today
  (`auth_service.py:52-57`). Offboarding is described in §4.2 (G9) and its
  limits in "Proposed risk acceptances".

**H2 — webhook verification material.** It replaces `webhook_service.py:85-89`.
It is needed **only in the full profile**; the pilot profile does not grant it,
because webhooks are a proposed pilot exclusion.

```sql
CREATE FUNCTION kerno_bootstrap.webhook_verification(p_registration_id uuid)
RETURNS TABLE (tenant_id uuid, signing_secret text, is_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT r.tenant_id, r.signing_secret, r.is_active
  FROM public.webhook_registrations r WHERE r.id = p_registration_id
$$;
-- REVOKE … FROM PUBLIC; GRANT EXECUTE … TO <runtime> (full profile only)
```

- It takes the same input as today. The id is already canonicalised as a UUID
  (`webhook_service.py:348-357`).
- It returns the same three columns, so the caller changes only its SQL string.
- It exposes no listing, no lookup by tenant or source, and no other column.

**What H2 guarantees, and what it does not:**

- **Guaranteed:** the runtime can never select `signing_secret` directly (a
  column grant), so ordinary queries and the registration GET never return it.
- **Not guaranteed:** in the full profile, the runtime can list registration
  ids under any tenant context it sets (it holds S(`id`) and controls the
  setting), then call H2 for each. **Every signing secret therefore remains
  obtainable by a compromised runtime.** H2 removes bulk pre-context reads; it
  does not make secrets confidential from the runtime.
- **Residual risk, owner decision (proposed for acceptance, full profile
  only).** Encrypting secrets at rest stays backlog.

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

No context, or an empty context, then yields no rows and no error. The runtime
holds no grant on `users`, so that policy is defence in depth there. In the
full profile, the `webhook_registrations` management endpoints keep working
because they set context first (`webhook_service.py:144,174`;
`src/api/webhooks.py:176`).

**Proposed exception, requiring owner approval: no FORCE on these two
tables.** `users` and `webhook_registrations` would stay ENABLE without
FORCE, deliberately departing from the backlog wording "FORCE on users and
webhook_registrations" (`NOW.md:402-403`; CLAUDE.md §17). The FORCE flags on
the other 19 tenant tables are unchanged.

- **What FORCE would add:** for a non-owner runtime, nothing **[PG]**. It binds
  only owner sessions (migrations, seeds and fixtures), and the owner can turn
  it off again with `ALTER TABLE … NO FORCE`.
- **What it would break:** the helpers execute as that owner. Under FORCE and a
  strict policy, they would see nothing before context is set, so login and
  ingest would fail.
- **What keeping FORCE would cost:** either a BYPASSRLS owner (superuser-granted;
  rejected here) or **Alternative B**: a second NOLOGIN "definer" role with
  column-scoped SELECT and a role-targeted policy
  (`FOR SELECT TO <definer> USING (true)`). That costs one extra admin-created
  role and one extra policy per table, and is stronger only against
  owner-session mistakes.

Neither the exception nor Alternative B is implemented by this preparation
task.

**`tenants`.** C2 leaves it without RLS. The pilot profile grants nothing on
it. The full profile's column grants keep B3, B4 and the visibility update
working, and leave the legacy credential columns unreadable. Their isolation
boundary is stated in §1.4. Adding RLS on `tenants` is a later option, not part
of minimal C2.

### 1.6 Migration, scheduler, seed and maintenance boundaries

| Activity | Identity | Change needed |
|---|---|---|
| API process | runtime | `DATABASE_URL` carries the runtime identity. **A startup identity gate** (new): outside development, the app refuses to start if its connection is superuser, BYPASSRLS, CREATEROLE or CREATEDB, has a membership, or owns the database or any table in `public`. In development it warns. The serving-profile ticket adds the profile-match check (§1.4). |
| Alembic | owner | `migrations/env.py` prefers a new `KERNO_MIGRATION_DATABASE_URL`, falling back to `DATABASE_URL`. That fallback only succeeds in owner-mode development; with a runtime `DATABASE_URL`, the first DDL fails. The app never receives the owner URL. Guarded-workflow handling is in §2.3. |
| Helpers, policies, grant procedure | owner | Migration 029 (§5) |
| Applying grants | owner | `grant-runtime` on `kerno_test`; an owner-run step in the pilot environment (§1.4) |
| Reference data (`compliance_controls`, filing windows) | owner | Owner-run load. `seed_nis2_controls.py` should gain an explicit target and environment confirmation before it is used for a pilot database. |
| Pilot tenant and user creation | owner | A new bounded script (gate G9). No tenant-registration path exists in `src/`. `seed_dev_tenant.py` is development-only, sets no slug (`seed_dev_tenant.py:25-31` versus `021:104-107`), and still writes the legacy credential columns. |
| Nightly bias job | runtime, full profile, if kept | A proposed pilot exclusion (§4.3) |
| Retention prune | prune identity (later) | Not needed within 180 days of the first decision. **[owner]** decides before then. |
| DORA submission scheduler | — | Stays unwired. If it is wired later, it loops per tenant over B4 rather than using a BYPASSRLS identity. |
| Backups and restores | provider/admin | See gate G5. A logical dump run as the owner meets FORCE-RLS tables **[PG]**, so backups need provider physical backups or a BYPASSRLS/superuser dump identity. Restores must re-verify RLS, FORCE, policies and grants. |
| Test fixtures, trigger tests | owner (`kerno_test`) | Unchanged (§2) |

### 1.7 Smallest change set (PILOT-READY-002, full profile)

1. **Migration 029** (owner, reversible): the `kerno_bootstrap` schema, H1, H2,
   the two strict policies, and the `apply_runtime_grants` procedure.
2. **Two SQL-constant swaps** to call the helpers (`auth_service.py:52-57`,
   `webhook_service.py:85-89`). No other registration read selects
   `signing_secret`: the GET already names its columns
   (`src/api/webhooks.py:103-107`), and rotation returns only `id`.
3. **`KERNO_MIGRATION_DATABASE_URL`** in `migrations/env.py`, with guard
   handling, and the startup identity gate.
4. **Guard support for `kerno_test_app`** (§2.3), the `grant-runtime` and
   `revoke-runtime` wrapper subcommands, the runtime provisioning script, and
   the runbook update.
5. **Runtime-identity live tests** (§2.4–2.5), using a runtime pool, and the
   full-profile privilege-matrix test.

Nothing else changes: no other policy, no FORCE flag, no ledger code, no
mounted route's behaviour. The pilot grant set and router set belong to the
serving-profile ticket.

### 1.8 What C2 does and does not guarantee

| After C2 | True? |
|---|---|
| The application cannot disable the ledger or AI-log triggers, alter or drop policies, toggle RLS or FORCE, truncate, or run DDL | **Yes**: it is not the owner **[PG]** |
| The application cannot UPDATE or DELETE `audit_log` rows, or UPDATE `ai_decision_log` | **Yes**: no grant, a second layer under the triggers |
| The application cannot SELECT from `users`, cannot SELECT `webhook_registrations.signing_secret`, and cannot read the legacy `tenants` credential columns | **Yes**: direct table or column access is denied |
| Credential material is unavailable to a compromised runtime | **No.** H1 returns the password hash of any account whose slug and email are supplied, as often as it is called. In the full profile, H2 returns the secret of any registration id, and the runtime can list those ids under any context it sets. The residual is described in §1.5; accepting it is an owner decision. |
| Tenant isolation holds even if the application sets the wrong tenant context | **No.** `app.current_tenant_id` is a setting any session may `SET` (`rls.py:43,57-60`). RLS protects against forgotten context, not against a compromised runtime credential or injected SQL that sets another tenant's id. |
| The visibility flag on `tenants` is tenant-isolated by the database (full profile) | **No.** Only the application's `WHERE tenant_id` confines it (§1.4). |
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
| Applying and revoking runtime grants | owner, guarded wrapper (`grant-runtime` / `revoke-runtime`), purpose `migrate` | New. Never connects as the runtime |
| Fixtures: seeding tenants, users, registrations, controls and windows; teardown with trigger toggles | owner (`db_connection`) | Unchanged |
| Owner-as-attacker tests (`test_audit_append_only.py:126-145`, the `test_ai_decision_log_append_only.py` and `test_ker203` trigger toggles) | owner | Unchanged; they assert owner capability by design |
| Application requests under C2 | **runtime**, through a real runtime pool (§2.4) | Real FastAPI app and real JWTs |
| Negative SQL probes | **runtime** | Direct statements on a runtime session |

The existing green suite, all run as owner, stays **regression evidence**. It
is not proof of the non-owner boundary.

### 2.2 What existing approvals cover

| Need | Covered today? |
|---|---|
| Owner-side DDL; grants on objects `kerno_test` owns; `GRANT CONNECT` on `kerno_test`; `ALTER DATABASE kerno_test SET …` | **Yes.** These are database- and object-owner rights; `kerno_test` gains no attribute **[PG]**. |
| Creating `kerno_test_app` and setting its password | **No.** This needs SUPERUSER or CREATEROLE, which `kerno_test` must not have. It is the administrator action in §2.7 step 3. |
| The guard accepting a second identity | **No.** The guard requires `session_user = current_user = database owner = kerno_test` (`tests/_database_safety.py:399-418`) and compares a single endpoint (`:632-653`). The change needs approval. |
| Confirming the new role can log in over `127.0.0.1` | **Unknown.** `pg_hba.conf` was never examined (`docs/test_database_runbook.md:239-241`). Step 6 of §2.7 is the first runtime login, and it reports a refusal rather than editing authentication. |

### 2.3 Guard support for the runtime identity

Specified here; not implemented by this issue.

1. **Separate settings in a separate file.**
   - **Names:** `KERNO_TEST_RUNTIME_DATABASE_URL` and
     `KERNO_TEST_RUNTIME_DATABASE_APPROVAL`, read from the environment or from
     a new gitignored file `.env.test.runtime` (`.gitignore:31` already ignores
     `.env.*`).
   - **One-source rule:** both or neither must be present, from one source.
   - **The existing `.env.test` stays exactly as it is,** two keys under the
     current allow-list (`_database_safety.py:58-60,241-266`), so the owner
     credentials are never rewritten.
   - **Child processes:** child-process stripping
     (`test_database_safety_processes.py:44-55`) and provisioning preflight
     cover the new names.
2. **Fixed identity allow-list:** `TEST_ROLE_NAME = "kerno_test"` and
   `TEST_RUNTIME_ROLE_NAME = "kerno_test_app"`.
   - The runtime URL must name the same literal loopback host, port and
     database as the owner URL.
   - Its approval must equal `kerno_test_app@<host>:<port>/kerno_test`.
3. **Two levels of validation, so setup has no circular dependency.**
   - **Static (pytest bootstrap only, when the runtime pair is present):** URL
     form, the fixed name, the same endpoint as the owner, and the approval.
     None of this depends on database state, so a correctly written pair always
     passes it.
   - **Live (only on the first runtime connection in a pytest run, after the
     workflow lock is held):**
     - database `kerno_test`;
     - `session_user = current_user = kerno_test_app`;
     - database owner `kerno_test`;
     - the disposable marker present; loopback; the same port;
     - every elevated attribute false; zero memberships;
     - owns nothing (`pg_shdepend` owner dependencies = 0);
     - no CREATE on the database or on `public`;
     - the same-lock proof (item 4).
   - **The migration wrapper never reads `.env.test.runtime`** and never
     connects as the runtime. It applies grants with the fixed role-name
     constant (§2.7 step 5).
4. **Same workflow lock.** The runtime session proves the guard session holds
   the TEST-SAFETY lock in the same database:

   ```sql
   SELECT count(*) FROM pg_locks
   WHERE locktype = 'advisory' AND classid = 1262834254 AND objid = 1413829460
     AND objsubid = 2 AND pid = <guard backend pid> AND granted
     AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
   ```

   It must return 1.
5. **Authorisation.** The runtime endpoint is allowed only after the live
   check passes, and only for purpose `pytest`. Every runtime connect still
   re-proves the lock (`:630`).
6. **`DATABASE_URL` stays the owner URL** (`:741`). The runtime URL is reachable
   only as `state.runtime_target`, never through a variable the application
   reads, so existing tests are unchanged.
7. **Redaction** covers both URLs and both passwords. Refusal messages never
   echo URL-derived values.
8. **One behaviour table for runtime configuration.** It governs every setup
   and acceptance instruction in this document (§2.7, §5).
   - **New flag:** `--require-runtime-identity`.
   - **Unchanged:** `--require-live-database` keeps its current meaning, the
     owner target.

   | Situation | Outcome |
   |---|---|
   | **Runtime pair absent**, without `--require-runtime-identity` | Each runtime test is skipped explicitly, with the reason "runtime identity not configured". Owner tests run unchanged. This is a **setup-stage owner regression run, not runtime acceptance**. |
   | **Runtime pair absent**, with `--require-runtime-identity` | Nonzero failure |
   | **Unsafe or incomplete configuration, unsafe identity, or a lock that is not proved.** This covers: one runtime variable without the other; a malformed pair; values from two sources; a wrong endpoint, role or approval; the wrong database, owner or marker; elevated attributes, a membership or owned objects; the workflow lock unproved or lost; a redirecting setting | **Fails closed**: the run stops. No skip, and never an owner fallback |
   | **Runtime settings supplied and statically valid, but not ready.** The role does not exist, password authentication fails, or CONNECT is not yet granted | A reported **runtime-setup failure** with a nonzero outcome, with or without `--require-runtime-identity`. Owner tests may still execute and report, but they cannot make the failed run succeed |
   | **Final acceptance** | `--require-live-database --require-runtime-identity`, with both owner and runtime targets verified and **zero skips** |
9. **`KERNO_MIGRATION_DATABASE_URL` in guarded workflows.**
   - `bootstrap_test_process` and the migration wrapper treat it like
     `DATABASE_URL`, but more strictly.
   - **An inherited value whose endpoint differs from the approved owner
     target is refused** (exit before any connection), not silently
     overridden. That includes a value naming the runtime user.
   - A matching or absent value is set to the owner URL.
   - `.env` loading stays disabled in the test process
     (`_database_safety.py:677-692`).
   - The guarded `psycopg2.connect` still authorises every connection Alembic
     opens, so there is no unguarded fallback.
   - **Offline tests:** a conflicting ambient value is refused; a value naming
     `kerno_dev` is refused; a matching value is accepted; an absent value is
     set; child processes strip it.

**What the guard restricts, and what it does not:**

- **The guard restricts where the test process connects.** The fixed database
  name, literal loopback host, fixed role names, re-checked redirecting
  settings, the disposable marker and the same-lock proof mean the test
  process cannot open a runtime (or owner) connection to `kerno_dev` or to any
  non-loopback host.
- **The guard does not restrict the credential.** PostgreSQL decides where
  `kerno_test_app` may log in. In source, `kerno_dev` is created without
  revoking PUBLIC CONNECT (`.env.example:17-19`). Anyone holding the
  `kerno_test_app` password could therefore connect to `kerno_dev` with
  another client. There it would hold no table privileges, unless objects
  there grant to PUBLIC, but it could read catalogs and use PUBLIC function
  privileges.
- **No `kerno_dev` change is authorised.** Revoking PUBLIC CONNECT there is
  recorded as an owner option, not a step in this plan.

### 2.4 Positive tests (runtime identity, live `kerno_test`, full profile)

**Supplying a real runtime pool to the application.** A `runtime_app`
fixture:

1. builds `psycopg2.pool.ThreadedConnectionPool(1, N, dsn=state.runtime_target.url)`.
   Every connection goes through the guarded `psycopg2.connect`, which
   authorises the runtime endpoint only after the live check;
2. monkeypatches `src.api.dependencies._pool` **and** `_get_pool`. Both
   `get_conn` and `pooled_transaction` (`dependencies.py:107-114,190-223`) then
   lease from the runtime pool, even if the global is reset. The SEC-REMED-001
   tests already replace `_pool` this way with an owner pool
   (`tests/integration/test_sec_remed_001_webhook_intake.py:64-70`);
3. does **not** override `get_conn`, `get_tenant_id`, `get_role` or
   `get_reviewer_id`. Requests carry real JWTs, so the production dependencies,
   commit-after-response, rollback and `putconn` paths all run;
4. wraps `getconn` so that each new physical connection is checked once with
   `SELECT session_user`, and fails the test unless it is `kerno_test_app`;
5. after the test, asserts the module still holds the runtime pool, and closes
   it.

**Owner work stays visibly separate.**

- Owner fixtures keep `db_connection` for seeding and for reading results.
- `DATABASE_URL` stays the owner URL. Code that bypasses the pool (the cron
  entrypoints, ad-hoc helper sessions) therefore runs as owner and never counts
  as runtime evidence.
- A runtime test of a cron path sets the runtime URL for that call only.

**Scenarios:**

- **Login through H1.** A valid user gets a token. A wrong password, unknown
  slug, unknown email and inactive user each get the same 401. `/me` works
  with no database read.
- **Register.** Create and PATCH a register entry, each with a ledger append.
  This exercises sequence USAGE and the `FOR NO KEY UPDATE` grant.
- **Submissions.** Start a run, repeat Start-run, read the run, and download the
  frozen package (`filing_package_downloaded` ledger entry).
- **Evidence.** Upload `.txt` and `.pdf` files, link, unlink (a soft delete
  through U(`removed_at`)), and list.
- **Recommendations.** Generate with the LLM mocked; the review lock and
  `FOR SHARE` must work under U(`is_superseded`). Then approve, edit and reject
  through the SEC-REMED-005 binding, with ledger appends.
- **Read paths.** Coverage summary and controls; evidence-pack export with its
  ledger append.
- **Full-profile features.** Every route still mounted in PILOT-READY-002 works
  under the full grants:
  - webhook registration create, get and rotate, and ingest verification
    through H2;
  - Trust Center status and visibility;
  - remediation trigger and close-callback (Jira mocked);
  - manual recalculation;
  - `GET /api/v1/ai-decisions`;
  - `GET /api/v1/register/windows`;
  - the panel.
- **Ledger.** `verify_audit_chain` is valid for each tenant after the
  workflow.

The serving-profile ticket repeats the pilot scenarios under the **pilot**
profile and asserts that every excluded path returns 404.

### 2.5 Negative and privilege tests (runtime identity)

| Probe | Expected (what the design guarantees) |
|---|---|
| `SELECT * FROM users`, with no context and with tenant context | permission denied (no grant) |
| `SELECT signing_secret FROM webhook_registrations` | permission denied (column) |
| `SELECT email, password_hash FROM tenants` | permission denied (column) |
| Webhook registration rows with no context, or an empty context | 0 rows (strict policy) |
| Tenant A context reading any FORCE-RLS table's tenant B rows | 0 rows |
| `UPDATE` / `DELETE` / `TRUNCATE audit_log` | permission denied, not the trigger's "append-only" message |
| `UPDATE` / `DELETE ai_decision_log` | permission denied |
| `ALTER TABLE … DISABLE TRIGGER`, `NO FORCE`, `DISABLE ROW LEVEL SECURITY`, `CREATE`/`DROP POLICY` | must be owner |
| `CREATE TABLE public.x`, `CREATE FUNCTION kerno_bootstrap.x` | permission denied |
| `SET ROLE kerno_test` | permission denied |
| `CALL kerno_bootstrap.apply_runtime_grants(…)` | permission denied |
| H1 with case or whitespace variants, `%`, `_`, or an email without the slug | no row |
| H1 for an exact, existing slug and email | **returns that account's row, including the hash.** This pins the documented residual (§1.5) rather than hiding it |
| H1 and H2, per call | never more than one row (a statement may still call them once per supplied key) |
| **ACL entries for PUBLIC** | `aclexplode(coalesce(proacl, acldefault('f', proowner)))` on H1, H2 and the grant procedure has no row with `grantee = 0` and `privilege_type = 'EXECUTE'`. `acldefault` matters: a NULL ACL means PUBLIC holds EXECUTE. Likewise, the ACLs of `kerno_bootstrap`, `public` and the database have no PUBLIC CREATE or TEMP, and no PUBLIC CONNECT on `kerno_test` |
| **Effective privileges of the runtime** | `has_table_privilege` / `has_column_privilege` / `has_sequence_privilege` / `has_function_privilege` / `has_schema_privilege` / `has_database_privilege` for `kerno_test_app` equal the full-profile matrix exactly, in both directions. These functions already count PUBLIC-derived privileges **[PG]** |
| Memberships and ownership | `pg_auth_members` shows none; `pg_shdepend` shows no owned object |
| An ordinary strict-policy table on a pooled connection reporting context `''` | the pre-existing behaviour is documented, not changed (the pool-reset backlog item) |

The append-only tests that expect the trigger's message keep running on the
owner connection.

### 2.6 What each kind of evidence proves

| Evidence | What it proves |
|---|---|
| Real PostgreSQL runtime validation | §2.4–2.5 on `kerno_test` as `kerno_test_app`; migration 029 apply, round trip and re-apply through the guarded wrapper; `grant-runtime`; the privilege matrix |
| Mocked | LLM responses (template or mocked client) and Jira |
| Static | This specification, the matrix's derivation from source, and code review of the helpers and the grant procedure |
| Deployment-only **[deploy]** | Role creation and `pg_hba` in the pilot database, TLS, platform body limits, backups and restores, alerting |

### 2.7 Setup and activation sequence (`kerno_test`)

**Two different PRs.** This preparation PR (draft PR #17, documentation only)
authorises none of the steps below. They belong to the later PILOT-READY-002
implementation PR, which proceeds in this order:

1. **Approval of scope.** The owner approves the exact implementation and test
   scope, including the §3 decisions.
2. **Script review.** The provisioning script is written and reviewed on the
   implementation branch before the administrator runs it.
3. **Setup and acceptance from an unmerged branch.** The guarded setup and the
   acceptance tests (steps 1–7 below) run from a recorded implementation-branch
   SHA while the implementation PR is still unmerged. The evidence is recorded
   against that SHA.
4. **Independent review** of the code and of the actual results.
5. **Merge only on explicit owner authorisation.**

Non-test deployment is separate and is not authorised by either PR.

Each step below names who runs it and which guarded operation performs it.
Each step stops at its first failure and reports exactly what completed.
Outcomes follow the behaviour table in §2.3 item 8. **No step undoes anything
automatically:** no `DROP ROLE`, no revoke, no downgrade and no file deletion
happen on failure.

1. **Approvals (owner, in writing).**
   - the `kerno_test_app` identity, confined to the approved disposable
     `kerno_test` (mirroring the 26 September record, `NOW.md:214-220`);
   - the proposed no-FORCE exception (§1.5);
   - the H1/H2 residual for test use.
2. **Apply migration 029 (owner; guarded wrapper, purpose `migrate`).**
   - Commands: `python scripts/migrate_test_database.py upgrade head`, then
     `downgrade c4d5e6f7` and `upgrade head` for the round trip.
   - No runtime role is involved; the migration grants nothing.
   - Then `python -m pytest --require-live-database`, with no runtime settings
     present anywhere. The owner suite must pass, and the runtime tests are
     skipped explicitly as "runtime identity not configured" (§2.3 item 8,
     row 1).
   - This is a setup-stage owner regression run, **not** runtime acceptance.
3. **Create the role (administrator, in the owner's own shell).**
   - Command: `python scripts/provision_test_runtime_role.py`. It reuses the
     existing provisioning helpers, and the administrator password is typed at
     a hidden prompt.
   - **Preflight:** `kerno_test`, its database and its marker exist;
     `kerno_test_app` does not; no runtime settings exist in the environment or
     in `.env.test.runtime`; that file would be gitignored.
   - **Writes `.env.test.runtime` first,** with exclusive create and only the
     runtime pair. The password comes from `token_urlsafe(32)` and is never
     printed or logged.
   - **Then executes exactly:**

     ```sql
     CREATE ROLE kerno_test_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
       NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD '<SCRAM-SHA-256 verifier>';
     ```

     The server receives only the verifier.
   - **Verifies as administrator:** the attributes, zero memberships in either
     direction, and zero owned objects.
   - **Does not try a runtime login.** CONNECT is not yet granted, because
     PUBLIC CONNECT is revoked on `kerno_test`
     (`provision_test_database.py:246-249`).
4. **Report partial states, owner decides.** No automatic recovery:

   | State | What the owner does |
   |---|---|
   | File written, role absent | Delete the file after confirming the role is absent |
   | Role created, verification failed | Decide between `ALTER ROLE … NOLOGIN` and an administrator `DROP ROLE` |

   The existing `.env.test` is never touched.
5. **Owner prerequisites (owner; guarded wrapper subcommand
   `python scripts/migrate_test_database.py grant-runtime`, purpose
   `migrate`).**
   - **Checks, as `kerno_test`:** the head includes 029, and the fixed role
     `kerno_test_app` exists with the expected attributes (a catalog read).
   - **Then, in one transaction:**

     ```sql
     GRANT CONNECT ON DATABASE kerno_test TO kerno_test_app;
     ALTER DATABASE kerno_test SET kerno.runtime_role = 'kerno_test_app';
     ALTER DATABASE kerno_test SET kerno.runtime_profile = 'full';
     CALL kerno_bootstrap.apply_runtime_grants('kerno_test_app', 'full');
     ```

     That `ALTER DATABASE … SET` is transactional is **[PG]**, to verify.
   - **Never connects as the runtime and never reads `.env.test.runtime`.**
   - **This is the step that makes a positive runtime login possible.** Between
     step 3 and this step, any pytest run ends as a reported runtime-setup
     failure with a nonzero outcome (§2.3 item 8, row 4), not as a safety
     refusal and not as a skip. No pytest run is needed in that window; this
     wrapper subcommand does not read the runtime settings.
6. **Verify the runtime identity (guarded pytest).**
   - Command:
     `python -m pytest tests/security/test_runtime_identity_boundary.py --require-live-database --require-runtime-identity`.
   - The first runtime connection runs the static checks, then the live
     identity checks and the same-lock proof, then a positive login. The
     negative and privilege probes of §2.5 follow.
   - Outcomes follow §2.3 item 8:
     - an unsafe configuration, an unsafe identity or an unproved lock fails
       closed;
     - a login, authentication or CONNECT refusal is a reported runtime-setup
       failure with a nonzero outcome;
     - `pg_hba.conf` is not edited.
7. **Final acceptance.**
   - Command:
     `python -m pytest --require-live-database --require-runtime-identity`.
   - Both the owner and runtime targets are verified, there are zero skips,
     and the result is recorded against the implementation-branch SHA it ran
     at.
   - That evidence goes to independent review before any merge (sequence
     above).

**Withdrawal:**

- **Owner:** `python scripts/migrate_test_database.py revoke-runtime` revokes
  the runtime's grants and CONNECT and resets both database settings.
- **Administrator:** `ALTER ROLE kerno_test_app NOLOGIN`.
- **Owner:** deletes `.env.test.runtime`.

**Credentials.**

- The administrator and runtime passwords are typed or generated only in the
  owner's shell. Claude Code never sees them.
- The runtime password exists in plaintext only in the gitignored
  `.env.test.runtime`, written once with exclusive create.

## 3. Minimum owner approval and action

1. **Approve the §5 ticket scope** as written, or amended. It is currently
   proposed only.
2. **Decide the proposed items:**
   - the no-FORCE exception for `users` and `webhook_registrations`, or
     Alternative B (§1.5);
   - acceptance of the H1 residual, and of the H2 residual in the full profile
     (§1.5, §1.8);
   - the profile ordering in §1.4.
3. **Approve, in writing, the second identity `kerno_test_app`**, confined to
   the already-approved disposable `kerno_test`.
4. **Run the one-time role creation** (§2.7 step 3) after the reviewed script
   exists. Everything else in §2.7 is run as `kerno_test` through guarded
   workflows.
5. **Before a pilot (not blocking that ticket):**
   - approve or amend the proposed gate list (§4.2), the proposed pilot
     exclusions (§4.3) and the proposed risk acceptances;
   - choose the hosting provider and region, the domain, and a budget
     **[owner]**;
   - say who creates the pilot database's owner and runtime roles (a managed
     PostgreSQL administrator, typically) **[owner/deploy]**;
   - choose the backup method and retention **[owner]**;
   - make the partner-data decisions in §4.4 **[owner]**.

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

### 4.2 Gate checklist (proposed)

The gate list itself is proposed until the owner approves it. "Required when"
keeps hosting-dependent and optional-capability conditions explicit; a gate
whose condition does not hold does not apply.

Status values:

- **Evidenced** — already shown by recorded evidence at a named SHA.
- **Validate** — needs a check, with no code change.
- **Bounded** — needs a small, separately approved implementation.
- **Owner** — needs a decision.

| # | Gate | Required when | Status | What is there, and what is required |
|---|---|---|---|---|
| G1 | **C2 runtime identity** (full profile, then pilot profile) in the pilot database | Always | Bounded + [deploy] | §5 ticket, then the pilot grants with G10. The pilot runs only as a non-owner runtime identity, enforced by the startup gate. |
| G2 | **HTTPS and origins** | Always. The headers item applies unless the platform already sets them | Evidenced in code; Validate [deploy]; Bounded (headers, conditional) | **In code:** startup refuses placeholder `ALLOWED_ORIGINS` outside development (`app.py:63-87`); empty origins fail closed (90-99); Next.js mutations require `KERNO_TRUSTED_ORIGINS` (503 otherwise, `lib/csrf.ts:42-62`); the session cookie is `secure` only when `NODE_ENV=production` (`auth/login/route.ts:41-47`). **Validate on the host:** HTTPS on both origins; `KERNO_ENV` ≠ development; `KERNO_ENABLE_DOCS` unset; exact `ALLOWED_ORIGINS`, `FRONTEND_URL` and `KERNO_TRUSTED_ORIGINS`; the cookie observed as Secure. **Headers:** `next.config.ts` sets none (no HSTS or CSP); add them only if the chosen platform does not. Host and domain are **[owner]**. |
| G3 | **Release and dependency reproducibility** | Always. The build recipe's form depends on hosting | Bounded + Owner | `pyproject.toml:37` requires `pypdf`, but **`uv.lock` has no `pypdf` entry**. `uv.lock` pins fastapi 0.139.0 while the recorded runtime has 0.138.1 (CLAUDE.md §17). There is no pinned Python (`pyproject.toml:9` `>=3.11`). **Required:** a regenerated, committed `uv.lock`; a recorded install-from-lock recipe on a pinned Python, in whatever form the host takes **[owner]**; and the release SHA recorded with the deployment. The frontend `package-lock.json` already pins exactly. |
| G4 | **Applied schema, including migration 028** (and 029 after C2) | Always | Evidenced on `kerno_test`; Validate [deploy] | 028 apply and round trip on `kerno_test` are recorded in `docs/sec_remed_005_recommendation_approval.md`, with the final full run at `9c3b097` at head `c4d5e6f7`. **Validate in the pilot database:** a single head equal to the release SHA's head; a read-only catalog check of RLS/FORCE flags, policies, triggers and the runtime privilege matrix for the pilot profile. |
| G5 | **Backup and isolated restore** | Always. The method depends on hosting | Owner + Validate | Nothing exists in the repo. **Required:** a backup method and schedule **[owner]**, and one restore into an isolated database. On the restore, verify the migration head, RLS/FORCE and grants, `verify_audit_chain` for every tenant, and a frozen package byte-identical to its earlier download. Logical dumps run as the owner meet FORCE-RLS tables **[PG]**: use provider physical backups or a dedicated BYPASSRLS/superuser dump identity, never the runtime. |
| G6 | **Evidence upload limits** on the exposed path | Deadline and concurrency cap: always. PDF limits: only if PDF upload is enabled for the pilot; the owner may restrict the pilot to text formats. Body-limit check: only if the proxy runs on a platform with a request-body limit | Evidenced (size); Bounded; Validate [deploy] | **Evidenced:** authentication before body, total/file/part limits and a counted body (SEC-REMED-003, `evidence_upload_intake.py:86-232`). **Bounded:** a reception deadline (none today, unlike the webhook's); an upload concurrency cap; and, if PDF stays in: PDF page, decoded-output and time limits (`evidence_intake.py:123-135` reads every page), extraction moved before the pool lease (`evidence.py:146,159`), and the PDF error text kept out of the 422 detail (`evidence_intake.py:134`). **Validate:** the platform body limit; Vercel's documented 4.5 MB would be below 10 MiB (`NOW.md:324-326`). |
| G7 | **Login abuse and pool exhaustion** | Always (login is anonymous) | Bounded | Login has no rate limit and runs scrypt (~16 MiB, `constants.py:296-301`) while holding a pooled connection (`auth_service.py:173-187`; pool max 10, `dependencies.py:113`). **Required:** a login rate limit keyed on the real client address (requests arrive through the Next.js server, so forwarding must be explicit and trusted), and pool exhaustion answered with a controlled 503, not a 500. |
| G8 | **LLM path** | Only if the LLM is enabled for partner data | Owner + Bounded | "Analyse" sends each linked record's title, source system and the first 600 characters of its body to Mistral's EU endpoint, without anonymisation (`recommendation_service.py:654-718`; `llm_client.py:52,73`). With `KERNO_LLM_MODEL` unset, the template rationale is used and nothing leaves (`recommendation_service.py:592-594`). **[owner]:** on or off (§4.4). **If on:** an LLM client timeout (none configured, `llm_client.py:73`). |
| G9 | **Pilot tenant and user provisioning; offboarding** | Always | Bounded | No tenant-registration path exists, and the development seed sets no slug. **Required:** an owner-run, auditable script that creates one tenant with an explicit slug and named users with scrypt hashes and roles, without writing the legacy `tenants.email/password_hash`. **Offboarding:** set `users.is_active = false`. This blocks **future** logins only (H1 returns the flag and login answers 401, `auth_service.py:183-185`). Tokens already issued stay valid; see "Proposed risk acceptances". `tenants.is_active` is not checked by login. |
| G10 | **Serving profile: excluded surfaces unreachable, pilot grants applied** | Always | Bounded | Every router is mounted in every environment (`app.py:191-213`). **Required, in one change:** a pilot serving profile that does not mount the proposed exclusions (§4.3); the pilot grant set (§1.4); the startup profile-match check; a test asserting the excluded paths return 404 and the pilot scenarios pass under pilot grants; and the excluded UI control removed (the Recalculate button). |
| G11 | **Operational alerts** | Always. The tooling depends on hosting | Bounded + Owner | There is no health endpoint (`GET /` is a redirect or static JSON, `app.py:215-231`), no logging configuration (`LOG_LEVEL` is read by nothing), and no monitoring hook. **Required:** a health check that touches the database without tenant data; error logging with the existing correlation IDs; and uptime and error-rate alerts to the owner. Tooling is **[owner]**. |
| G12 | **Filing language** | Always | Bounded | The UI says "Download filing" and shows internal validation status (`FilingDownloadButton.tsx:81`, `SubmissionRunView.tsx:51-61`). **Required:** copy stating that this is Kerno's internal frozen package and not an official ESA/xBRL submission, and the same wording in demo material. |
| G13 | **Filing-download proxy gate** | Recommended; the owner may instead accept the risk | Bounded (small) | The Next.js package proxy has no `Sec-Fetch-Site` gate, though the backend writes a ledger entry on success (`submissions/runs/[runId]/package/route.ts:16-31`; `submissions.py:100-102`). Mirror the export route's gate. |
| G14 | **Synthetic browser-to-database walkthrough** before real data | Always | Validate [deploy] | On the deployed pilot stack, with two synthetic tenants, run every §4.1 step through a browser. Confirm: the ledger entries and chain per tenant; tenant B sees none of tenant A's data; excluded paths return 404; the session cookie is Secure. Recorded with the release SHA. |
| G15 | **Evidence identity versus exact historical content and original files** | Always | Validate + Owner | Kerno keeps extracted text plus a SHA-256 of that text (`content_hash`), not the original file (`evidence_intake.py:11-13`). No application path updates a record's text (no `UPDATE context_records` in `src/`), but no database rule prevents the owner from doing so. A recommendation snapshot records evidence identity (id, title, score), not content (CLAUDE.md §15 KER-405 #4, on hold). **Before real data:** agree with the partner whether extracted text plus hash is acceptable pilot evidence, or whether they keep the originals; demo language says "extracted text", not "original document". Durable evidence versions and provenance stay in the existing architecture (`DORA_MODEL_V2.md` §21–§22, V2-011); no storage platform is added here. |
| G16 | **Repeat Start-run replaces the stored package** | Always | Validate + Owner | A repeat Start-run for the same window and reporting year finds the existing run (`_find_existing_run`, `dora_roi_submission_service.py:395-411`) and overwrites its `frozen_package_json` and counts (467-485). The run's ledger entry records status and counts, not the package bytes or a hash (`_run_to_ledger_state`, 255-265). The earlier package is not recoverable from Kerno unless it was downloaded. **Before real data:** demonstrate this, and either agree a procedure (download and keep each package before re-running) or accept it. Immutable Register snapshots and package versioning are V2-006/V2-010 (`DORA_MODEL_V2.md` §24, §29) and are not built here. |
| G17 | **Representative scoring judgments checked with a qualified reviewer** | Only if scores or recommendations are presented to the partner as meaningful | Validate | Uncurated evidence links default to 0.5 and produce a flat partial/medium result (NOW.md "Do not claim"; CLAUDE.md §15 pre-demo action 1). **Before scores are shown as meaningful:** a qualified compliance reviewer checks a representative sample of the partner's controls and records where they agree and disagree. No scoring change is authorised; calibration remains KER-403, report-only. |
| G18 | **Representative performance measurements before optimisation** | Always (measure; optimise only if needed) | Validate [deploy] | Measure on the deployed pilot stack with representative data: login latency, upload and PDF extraction for representative documents, Analyse with the LLM on or off, export size and time, list endpoints at expected counts, and behaviour at expected concurrency against the 10-connection pool. Record the numbers with the release SHA; an optimisation ticket only follows a measured shortfall. |

**Proposed risk acceptances (each needs owner approval; not gates):**

- **Session lifetime and offboarding.** Tokens last up to 24 hours
  (`JWT_EXPIRY_SECONDS`, `constants.py:291`). Request validation checks only
  the token's signature and expiry and the claims inside it
  (`dependencies.py:117-169`), never `users` or `tenants`. So:
  - a deactivated user, or a user whose role was changed, keeps the old
    token's access until it expires;
  - nothing revokes a single token.
- **Proposed emergency procedure** to end every session at once: replace
  `KERNO_JWT_SECRET` in the backend's runtime configuration, then restart or
  redeploy **every** API process. The secret is read from the process
  environment on each request (`dependencies.py:100-104`), so editing a
  configuration file does not change a running process. Every user must then
  sign in again: the browser's cookie is rejected with 401, and the dashboard
  redirects to login (`frontend/app/dashboard/layout.tsx:23-27`).
- **Unpaginated lists** at pilot data volume.
- **The pooled connection held across the LLM call,** if the LLM is on
  (10/min limit).
- **`get_conn` committing after the response.**
- **The KER-107 per-transaction deadlock limitation.**

### 4.3 Proposed pilot exclusions (owner approval required)

Exclusion is enforced on the serving path (G10), not by hiding UI. Until G10
lands, the full profile serves every surface below.

| Surface | Why proposed for exclusion |
|---|---|
| `/api/v1/remediation/*` (Jira) | Outside the pilot workflow. Sends rationale to Jira with global credentials. |
| `/api/v1/webhooks/*` (registration, rotation, ingest) | Outside the pilot workflow. Plaintext secrets, the H2 residual, and anonymous ingest with no rate limit. |
| `/trust-center/{slug}/status`, `/api/v1/trust-center/visibility` | Not part of the pilot ("no Trust Center polish", NOW.md). An anonymous surface, and the unprotected `tenants` grants (§1.4). |
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
    are not stored (`evidence_intake.py:11-13`; G15).
  - Ledger entries are append-only and kept forever.
  - `ai_decision_log` is kept for 180 days.
  - Decide what happens to each at pilot end, including in backups.
- **External services.**
  - **Mistral** (EU endpoint) receives evidence excerpts when Analyse runs
    (G8). Choosing an EU endpoint does not by itself settle the processing
    terms, subprocessor listing or retention at the provider.
  - The **hosting provider and its subprocessors** need the same decision.
  - **Jira** is a proposed exclusion.
- **Isolation claims.** RLS is a correctness control inside one database, not a
  data-handling guarantee (§1.8). Whether a dedicated database or project per
  partner is required is **[owner]**.

### 4.5 Kept in the DORA v2 sequence (not pilot work)

- **Official reporting.** Projection (V2-008), versioned validation (V2-009) and
  the official reporting package (V2-010) follow the mandatory real-data
  checkpoint (`DORA_MODEL_V2.md` §36–§37).
- **History.** Immutable Register snapshots (V2-006) and package versioning
  stay in that order (G16).
- **Data entry and provenance.** Import staging and provenance is V2-004;
  field-level evidence provenance is V2-011 (G15).
- **Next domain slice.** Contract costs (§7.4) remains the next DORA domain
  slice listed in NOW.md and is not implemented here.
- **Frozen package.** The current frozen JSON stays "valid transitional
  infrastructure" (`DORA_MODEL_V2.md` §29). It must not be presented as
  regulator-accepted output.

## 5. Next implementation ticket: acceptance criteria (proposed)

**PILOT-READY-002, "C2 runtime role (full profile)". Proposed, not approved.**
It requires explicit approval, including the §3 decisions, before work starts.

1. **Migration 029** (`d5e6f7g8` style id, parent `c4d5e6f7`, reversible),
   applied as the owner:
   - It creates the `kerno_bootstrap` schema, H1 and H2 exactly as in §1.5:
     SECURITY DEFINER, pinned `search_path`, schema-qualified, exact match, at
     most one row, EXECUTE revoked from PUBLIC.
   - It creates the owner-only `apply_runtime_grants(role, profile)` procedure
     implementing both columns of §1.4. The procedure validates the role and
     the profile, and EXECUTE on it is revoked from PUBLIC.
   - It replaces the `users` and `webhook_registrations` policies with the
     strict `NULLIF` form, with `USING` and `WITH CHECK`, and **without FORCE,
     subject to the owner approving that exception**.
   - The migration itself grants nothing to any role. Later table-creating
     migrations call the procedure when `kerno.runtime_role` is set.
   - It changes no other policy, no FORCE flag and no table.
   - Its downgrade restores the 019 and 022 policies, removes the helpers, the
     procedure and the schema, and revokes runtime grants.
2. **Guarded owner subcommands** `grant-runtime` and `revoke-runtime` in
   `scripts/migrate_test_database.py`, as in §2.7 steps 5 and "Withdrawal":
   - purpose `migrate`, holding the workflow lock;
   - the fixed role name; profile `full` in this ticket;
   - one transaction; refusal if 029 is absent or the role is missing or
     invalid;
   - never connects as the runtime.
3. **Code:**
   - `auth_service` and `webhook_service` call the helpers;
   - behaviour is otherwise unchanged, including the uniform 401s and dummy
     verification;
   - no runtime code selects `webhook_registrations.signing_secret` or reads
     `users` directly.
4. **Configuration:**
   - `migrations/env.py` prefers `KERNO_MIGRATION_DATABASE_URL`;
   - the guard refuses a conflicting inherited value and sets it to the owner
     URL (§2.3 item 9);
   - the API startup identity gate refuses an owner, elevated or member
     identity outside development, and warns in development;
   - `.env.example` documents the owner and runtime URLs.
5. **Test guard** per §2.3:
   - the separate `.env.test.runtime`;
   - fixed `kerno_test_app`, on the same endpoint apart from the user;
   - static validation at bootstrap; live validation on first use, including
     owns-nothing and the same-lock proof;
   - `--require-runtime-identity` and every row of the behaviour table in
     §2.3 item 8, each with an offline unit test;
   - purpose `pytest` only; `DATABASE_URL` remains the owner;
   - offline unit tests for every refusal and for the
     `KERNO_MIGRATION_DATABASE_URL` cases;
   - the runbook updated with §2.7.
6. **Runtime provisioning script** per §2.7 step 3, with offline tests. It is
   reviewed on the implementation branch, and the administrator runs it only
   after written approval.
7. **Runtime pool fixture** per §2.4. No dependency override of `get_conn`; a
   per-connection `session_user` check; no runtime evidence from owner paths.
8. **Live evidence on `kerno_test`,** all through the guards, following §2.7.
   It runs from a recorded implementation-branch SHA while the implementation
   PR is still unmerged:
   - 029 upgrade, downgrade and upgrade;
   - `grant-runtime`;
   - the runtime identity check;
   - §2.4 (full profile) and §2.5, including the PUBLIC ACL and
     effective-privilege checks and the documented H1 residual;
   - final acceptance per §2.3 item 8: the full suite with
     `--require-live-database --require-runtime-identity` at that SHA, both
     targets verified, **zero skips**;
   - `kerno_dev` untouched;
   - independent review of the code and of these results, then merge only on
     explicit owner authorisation.
9. **Preserved:** every invariant in §1.9. The existing owner-role suite stays
   green. No DORA v2 table gains access. Every route mounted at the tested SHA
   keeps working under the full-profile grants.
10. **Documentation:**
    - CLAUDE.md §17 C2 row and NOW.md updated after merge;
    - the decision record of the approved FORCE choice, and of the accepted or
      rejected H1/H2 residuals;
    - the claims in §1.8 adopted verbatim.

**Out of scope for that ticket:**

- the pilot grant set and router set (the serving-profile ticket, G10);
- G2–G9 and G11–G18;
- `tenants` RLS; webhook secret encryption; a no-hash login design;
- pool reset; the prune identity; the DORA submission scheduler;
- any deployment.

**Later tickets.** Each is proposed and each needs its own approval:

- the serving profile with the pilot grants (G10, completing G1);
- intake and login limits (G6, G7);
- reproducibility (G3);
- operations and health (G11);
- pilot tenant provisioning (G9);
- filing language and the proxy gate (G12, G13);
- an LLM timeout if the LLM stays on (G8).

**Validation and owner items:** G2, G4, G5, G14 to G18, and the §4.4
decisions.

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

**Verified merges, recorded in NOW.md:**

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
  `tests/conftest.py`, the safety unit tests, and
  `tests/integration/test_sec_remed_001_webhook_intake.py`.
- **Frontend:** `frontend/app/**`, `frontend/lib/*`, `frontend/next.config.ts`.
- **Build and configuration:** `pyproject.toml`, `uv.lock`, `.env.example`.
- **PR #6:** branch `origin/cursor/rename-grunnbok-971b`, diff against
  `5bd6552`.
- **Not read:** `.env`, `.env.test` and `.env.local`.

Every statement about PostgreSQL behaviour is marked **[PG]** and is to be
proven in the implementation ticket.
