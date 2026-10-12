# Test database runbook — `kerno_test` (TEST-SAFETY-001)

This runbook covers one disposable PostgreSQL database, `kerno_test`, and one
restricted login role, also `kerno_test`. Live-database tests and test
migrations may run against this target and nothing else. They never run
against `kerno_dev` and never fall back to `DATABASE_URL`, `.env` or libpq
defaults.

**Status on 1 October 2026: provisioned, verified, live acceptance passed.**
- **Approval:** the owner approved `kerno_test@127.0.0.1:5432/kerno_test` as
  a disposable test database on 26 September 2026.
- **Provisioning:** the owner ran the assisted provisioning script and
  reported success.
- **Verification and first use:** Claude Code then verified the target
  through the safety controls and ran the first-use sequence.

The three are recorded separately in *First-use record* (step 7) and in
`NOW.md`. Without valid settings, every live-database test is still skipped
with an explicit reason, and `python -m pytest --require-live-database` and
`scripts/migrate_test_database.py` refuse to run.

Steps 1–4 are a one-time **owner/administrator action**, either by hand or
with the assisted script below. The only thing in this repository that
creates anything is that script, and only the owner runs it. Nothing in the
repository drops, recreates, resets or repairs a database, and nothing else
uses administrator credentials.

## Assisted provisioning (owner-delegated, 26 September 2026)

The owner delegated steps 1–4 to Claude Code so they would not have to type
each statement. The administrator password must stay private, so the setup
is a single inspectable script that **the owner runs in their own PowerShell
window**:

```text
cd J:\Kerno
python scripts\provision_test_database.py
```

`scripts/provision_test_database.py` does steps 1–4 and step 5's settings
file.

**Before connecting,** it refuses and changes nothing when:

- `.env.test` already exists;
- `.env.test` is not gitignored;
- a redirecting libpq variable is set;
- `KERNO_TEST_*` is set in the shell.

**The administrator password:**

- It is asked for once, at a hidden prompt.
- It is never shown, stored or logged.
- An empty password is refused, so saved passwords are never used silently.

**Before any change,** it connects as `postgres` to `127.0.0.1:5432` and:

- confirms the server;
- stops, reporting their state, if a `kerno_test` role or database already
  exists (it never drops, renames, resets or reuses one);
- shows what it will create and requires the owner to type `yes`.

**What it creates:**

- **`.env.test`:** written first, before any object, so the generated
  password can never be lost.
- **The role:** a restricted `kerno_test` role. Its password is generated and
  only its SCRAM-SHA-256 verifier is sent to the server, so the plaintext
  exists only in `.env.test`.
- **The database:** `kerno_test`, owned by that role, with PUBLIC access
  revoked.
- **The extension:** `vector`, installed through a separate short
  administrator connection to `kerno_test`.
- **The marker:** the disposable-database comment.

It checks every statement's result before running the next one.

**Afterwards,** it verifies everything step 4 lists, including that
`kerno_dev`'s access list is unchanged. It closes the administrator
connections, then logs in as the restricted `kerno_test` role to check the
target identity, the extension and the schema rights.

**If anything fails partway through,** it prints exactly what was created and
stops. It never deletes anything and never reruns over half-created objects.
Report the output instead of running it again.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | created and verified |
| 1 | stopped after starting (the output lists what exists) |
| 2 | refused before changing anything |

It is a separate process from the tests. It never installs, changes or
disables the test connection guard, and never reads `.env`. Every later
migration and test runs as the restricted `kerno_test` role, never as
`postgres`. The manual steps below remain valid and are what the script
automates.

---

## 1. Pre-checks — stop if anything already exists

Run every command in steps 1–3 **one at a time**, from a shell, as the
PostgreSQL administrator on this machine's server. Read each result before
running the next command. Each command is a separate `psql -c` call with
`ON_ERROR_STOP`, so a failure ends that command and nothing after it runs by
itself. (Don't paste the steps as one block into an interactive `psql`
session: there, an error only returns you to the prompt, and the rest of the
paste keeps running.)

```text
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "SELECT rolname FROM pg_roles WHERE rolname = 'kerno_test'"
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "SELECT datname FROM pg_database WHERE datname = 'kerno_test'"
```

Both must return `(0 rows)`. If either returns a row, **stop**. Do not drop,
rename or reuse the object; report it for review.

`CREATE ROLE` and `CREATE DATABASE` below fail on an existing object instead
of replacing it. The commands after them do not have that protection:
`\password`, `REVOKE`, `CREATE EXTENSION` and `COMMENT` act on whatever
`kerno_test` exists. That is why each of them runs only after the `CREATE`
before it has succeeded.

A read-only check on 25 September 2026 found neither object.

## 2. Create the restricted login role

```text
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "CREATE ROLE kerno_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
```

Only if that printed `CREATE ROLE`:

```text
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "\password kerno_test"
```

`\password` asks for the new password twice and sends only a hash, so the
password stays out of the shell and psql history. Use only letters, digits,
`-` and `_`, so it can go into a URL without encoding. `migrations/env.py`
passes the URL through Python's `ConfigParser`, which misreads `%`.

Do not grant this role anything else. In particular, do not give it
`SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS`, `REPLICATION` or
membership in any role. The test process checks all of these on every run
and refuses a role that has any of them.

## 3. Create the database, install the extension, mark it disposable

```text
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "CREATE DATABASE kerno_test OWNER kerno_test"
```

Only if that printed `CREATE DATABASE`, run these three, one at a time:

```text
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "REVOKE ALL ON DATABASE kerno_test FROM PUBLIC"
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d kerno_test -c "CREATE EXTENSION vector"
psql -v ON_ERROR_STOP=1 -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "COMMENT ON DATABASE kerno_test IS 'kerno:disposable-test-database'"
```

- **Ownership:** `kerno_test` owns the database. It will also own every table
  it migrates, so the FORCE-RLS tests keep meaning something. Don't run the
  test suite or the migrations as `postgres`.
- **Access:** `REVOKE ALL … FROM PUBLIC` limits who can connect to the new
  database. As owner, `kerno_test` keeps its own access.
- **pgvector:** on this server pgvector can only be installed by a superuser
  (it is not a trusted extension), so the administrator installs it.
  Migration 002's `CREATE EXTENSION IF NOT EXISTS vector` then does nothing.
  No migration needs any other extension or any superuser privilege
  (checked 25 September 2026).
- **The comment:** each workflow reads it once, as part of the identity
  check on its guard connection (see step 7), and refuses a database that
  lacks it. It marks the database as the one the owner provisioned as
  disposable. It is **not** a tamper-proof administrator signature:
  `kerno_test` owns the database, so the role itself could also set or clear
  the comment.
- **Withdrawing approval:** to do it in a way `kerno_test` cannot undo, the
  administrator runs `ALTER ROLE kerno_test NOLOGIN`.
  - `NOLOGIN` only blocks new logins. It does not end sessions that are
    already connected.
  - A workflow that is already running keeps going until it finishes, unless
    the administrator also ends its sessions. The workflow's guard connection
    shows as `kerno-test-guard:pytest` or `kerno-test-guard:migrate` in
    `pg_stat_activity`.
  - Clearing the comment (`COMMENT ON DATABASE kerno_test IS NULL`) makes
    every later workflow refuse at its identity check. It does not stop one
    that has already passed that check.
- **The `public` schema:** on PostgreSQL 15 and later it belongs to
  `pg_database_owner`, so the database owner can create tables in it without
  any extra grant.

## 4. Verify (read-only, as administrator)

```sql
SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolcanlogin
FROM pg_roles WHERE rolname = 'kerno_test';                  -- f f f f f t
SELECT count(*) FROM pg_auth_members WHERE member = 'kerno_test'::regrole;   -- 0
SELECT pg_get_userbyid(datdba), datacl, shobj_description(oid, 'pg_database')
FROM pg_database WHERE datname = 'kerno_test';               -- kerno_test | {kerno_test=CTc/kerno_test} | kerno:disposable-test-database
\connect kerno_test
SELECT extname FROM pg_extension ORDER BY 1;                -- plpgsql, vector
SELECT has_schema_privilege('kerno_test', 'public', 'CREATE');   -- t
```

**Confirm that no development database changed.** None of the statements
above names `kerno_dev`. Its access list should still read exactly as it did
on 25 September 2026:

```sql
SELECT datacl FROM pg_database WHERE datname = 'kerno_dev';
-- {=Tc/kerno_dev,kerno_dev=CTc/kerno_dev}
```

### What the new role can still reach in `kerno_dev` — reported, not changed

PostgreSQL's defaults give PUBLIC `CONNECT` and `TEMPORARY` on `kerno_dev`,
so once `kerno_test` exists it could open a session there and create
temporary tables. A read-only check on 25 September 2026 found:

- `USAGE` on `kerno_dev`'s `public` schema, but not `CREATE`;
- no table or view granted to PUBLIC;
- no `SECURITY DEFINER` functions;
- 121 functions executable by PUBLIC, all running with invoker rights.

The role therefore cannot read or change `kerno_dev`'s data. In any case, the
test code refuses every connection to anything but `kerno_test` before libpq
is even called.

If you want to close the gap, that is your decision. It was not made here:

```sql
REVOKE CONNECT, TEMPORARY ON DATABASE kerno_dev FROM PUBLIC;   -- only after confirming nothing else relies on it
```

Server authentication rules (`pg_hba.conf`) were not examined or changed. If
`kerno_test` cannot log in over `127.0.0.1`, report it rather than editing
authentication.

## 5. Local settings — on the owner's machine, never committed

Create `J:\Kerno\.env.test` containing exactly these two lines:

```text
KERNO_TEST_DATABASE_URL=postgresql://kerno_test:<password>@127.0.0.1:5432/kerno_test
KERNO_TEST_DATABASE_APPROVAL=kerno_test@127.0.0.1:5432/kerno_test
```

- **It stays out of git.** `.gitignore` already covers `.env.*`, and
  `git check-ignore -v .env.test` must print the matching rule
  (`.gitignore:31:.env.*`). The safety tests check this.
- **Only these two keys.** Any other key fails every run, even a harmless one.
  Never copy lines from `.env`.
- **Keeping the password out of the file (optional).** Omit the password from
  the URL and put it in libpq's password file instead:
  `%APPDATA%\postgresql\pgpass.conf`, line
  `127.0.0.1:5432:kerno_test:kerno_test:<password>`.

### How the settings are chosen

| Situation | What the test process uses |
|---|---|
| Either `KERNO_TEST_*` variable is set in the environment | Both must be set there, and `.env.test` is not read at all |
| Neither is set, and `.env.test` exists | Both are read from `.env.test` (a byte-order mark is tolerated) |
| Neither is set, and there is no `.env.test` | No test database. Live tests skip, and every connection attempt is refused |
| Anything is set but invalid (empty, unparseable, wrong target, extra keys) | The run fails. It never skips and never falls back to anything else |
| `KERNO_TEST_ENV_FILE` is set | That path is read instead of `.env.test`, under exactly the same rules. It exists so the safety tests can point child processes at a missing file. The gitignore check covers only the default `.env.test`, so don't point it at a tracked file |

The following never authorise a test database:

- `DATABASE_URL`, which is removed from the test process at startup;
- the ordinary `.env`, whose loading is switched off with
  `PYTHON_DOTENV_DISABLED=1` before any application import;
- libpq defaults.

The URL must state host, port, database and user explicitly.

- **Host:** exactly `127.0.0.1` or `::1`. The name `localhost` is refused:
  libpq may resolve it to either address on each connection, so the verified
  guard session and a later working connection could reach different
  listeners.
- **Port:** written as a plain number, such as `5432`.
- **Redirecting settings:** anything that could redirect the connection is
  refused, whether it is a URL parameter (`hostaddr`, `service`, `options`, …)
  or an environment variable (`PGHOSTADDR`, `PGSERVICE`, `PGOPTIONS`, …).
- **Refusal messages:** these never repeat a value taken from the URL, so a
  mistyped URL can't print its password.

## 6. Recording approval

Approval has three parts, and all three are required.

1. **The database comment** from step 3, set by the administrator. As noted
   there, the `kerno_test` role could also set it. Withdraw approval with
   `ALTER ROLE kerno_test NOLOGIN`. That blocks new logins but does not end
   sessions already connected (step 3 explains how to handle those).
2. **`KERNO_TEST_DATABASE_APPROVAL`**, which must equal the exact identity
   `kerno_test@127.0.0.1:5432/kerno_test`. If the URL points anywhere else,
   the two no longer match and every run refuses.
3. **A written record in `NOW.md`**, made by the owner or on their explicit
   instruction:
   *"`kerno_test@127.0.0.1:5432/kerno_test` approved as a disposable test
   database by <owner> on <date>."*

The code checks part 2 whenever a process starts, and part 1 once per
workflow, when it acquires its guard connection. Part 3 is the human record.

With assisted provisioning, the script writes parts 1 and 2 on the owner's
behalf, and only when the owner runs it and types `yes`. The owner's approval
itself was given in writing on 26 September 2026 and is recorded in
`NOW.md`, separately from the provisioning and verification results.

## 7. First use

```text
python scripts/migrate_test_database.py upgrade head      # build the schema as kerno_test
python -m pytest --require-live-database                  # full suite, live tests required
python scripts/migrate_test_database.py downgrade z1a2b3c4
python scripts/migrate_test_database.py upgrade head      # migration round trip on the disposable target
```

Each workflow first opens one dedicated **guard connection**. On that
connection it checks, read-only against the catalogs, that the live database
is what was approved:

- the database name;
- `session_user` and `current_user`;
- a TCP loopback server address and the approved port;
- no privileged role attributes and no role memberships;
- database ownership;
- the disposable-target comment.

This catalog check runs once per workflow, on the guard connection only.

The guard connection then takes one session-level advisory lock,
`pg_try_advisory_lock(1262834254, 1413829460)` (the ASCII codes of `KERN` and
`TEST`), without waiting, and holds it until the workflow ends. That lock
allows only one workflow at a time: one pytest run or one migration run,
never both, and never two of either.

After that, every **working connection** the process opens (the fixtures,
the tests' own sessions, SQLAlchemy, the pools) gets two cheaper checks:

- **Parameters:** its effective connection parameters are compared with the
  approved target (host, port, database, role, and no redirecting
  parameters).
- **Lock:** the guard connection re-checks that the lock is still held.

The catalog query is **not** rerun for each working connection. Pinning the
host to a literal loopback address and refusing redirecting parameters and
environment variables is what makes the parameter comparison sufficient.

- **A second workflow** fails immediately. Its message names the holder's pid
  and application name.
- **During a workflow,** the lock is proved again at each of these points:
  - every new working connection the process opens (a lock re-check on
    the guard connection, not a new catalog check);
  - before `db_connection` seeds and before it cleans up;
  - before every migration step and after the last one;
  - at the end of the pytest session.

  If a check fails, the process stops. A pytest run then ends as interrupted
  (exit 2), and the migration wrapper exits 3. A loss at the final check is
  never reported as success.
- **Where there are no checks:** between two checks, work runs on connections
  that are already open. That includes the cleanup of fixtures layered on top
  of `db_connection`, which runs on `db_connection`'s connection just before
  its own final check. A lock lost in that window is caught at the next
  check, not the moment it happens.
- **The lock is released** when the workflow ends, on every exit path, and
  PostgreSQL releases it anyway if the process dies.
- **Parallel pytest workers** (for example `-n` with pytest-xdist) are refused
  against the test database.

The migration wrapper's exit codes:

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | a migration failed |
| 2 | bad command or destination, or configuration missing or refused |
| 3 | target refused, busy, or exclusivity lost |

### First-use record (1 October 2026)

| What | Who | Result |
|---|---|---|
| Provisioning | the owner (reported, not observed by Claude Code) | `DONE: kerno_test@127.0.0.1:5432/kerno_test provisioned and verified.`, exit 0 |
| Target verification | Claude Code, read-only through the guard connection as `kerno_test` | every step 7 identity condition held; extensions `plpgsql` and `vector` 0.8.3; empty `public` schema |
| `migrate_test_database.py upgrade head` | Claude Code | exit 0; base to `a2b3c4d5` in 26 steps |
| `pytest --require-live-database` | Claude Code | exit 0; 1340 passed, 0 failed, 0 skipped |
| Exclusion (migration holding) | Claude Code | a pytest run refused (exit 2), a second migration refused (exit 3) |
| Exclusion (pytest holding) | Claude Code | `downgrade z1a2b3c4` refused (exit 3), a second pytest refused (exit 2) |
| `downgrade z1a2b3c4`, then `upgrade head` | Claude Code | exit 0, then exit 0; `a2b3c4d5` to `z1a2b3c4` and back |
| Affected live and schema-parity tests after the round trip | Claude Code | exit 0; 224 passed, 0 skipped (9 files) |

In both exclusion directions, every refusal named the holder and came before
any change. After each release, the next workflow ran. `NOW.md` records the
details.

## 8. Runtime identity `kerno_test_app` (C2, PILOT-READY-002)

**Status: implemented on the unmerged branch `pilot/pilot-ready-002-c2-runtime`;
nothing below has been run.** No role has been created, no grant applied and
migration 029 has not been applied to `kerno_test`. Every live step waits for
the owner's written approval (issue #18, Checkpoint A).

The C2 tests run the application as a second, non-owner login on the same
database, so that row-level security, the column grants and the ledger's
privilege layer are tested as they bind a non-owner. The owner role
`kerno_test` keeps doing everything it does today: migrations, fixtures,
seeding, cleanup, and the owner-as-attacker trigger tests.

### Settings — `.env.test.runtime`, never committed

The administrator's provisioning script writes `J:\Kerno\.env.test.runtime`
once, with exclusive create, containing exactly:

```text
KERNO_TEST_RUNTIME_DATABASE_URL=postgresql://kerno_test_app:<password>@127.0.0.1:5432/kerno_test
KERNO_TEST_RUNTIME_DATABASE_APPROVAL=kerno_test_app@127.0.0.1:5432/kerno_test
```

- `.env.test` is never rewritten; the owner's two keys stay where they are.
- **One source.** The pair comes from the environment or from this file.
  Values in both, one variable without the other, or any other key in the
  file fail the run. `KERNO_TEST_RUNTIME_ENV_FILE` overrides the path, under
  the same rules, so the safety tests can point children at a missing file.
- **Same endpoint, fixed role.** The runtime URL must name the owner URL's
  literal host, port and database, and log in as exactly `kerno_test_app`;
  the approval must equal that identity.
- **Never the application's variable.** `DATABASE_URL` stays the owner URL. The
  runtime URL reaches only the runtime fixtures, so no owner code path can
  pass for runtime evidence.
- **Only pytest reads it.** The migration wrapper (including `grant-runtime`
  and `revoke-runtime`) never reads these settings and never connects as the
  runtime.
- `KERNO_MIGRATION_DATABASE_URL` (Alembic's owner URL) is refused in a test or
  migration process if it names anything but the approved owner target, and
  is otherwise pinned to it.

### Two checks before any runtime connection

1. **Static, at pytest startup:** the rules above. Nothing here depends on the
   database, so correct settings always pass.
2. **Live, on the first runtime connection, after the workflow lock is held:**
   on a one-off connection as `kerno_test_app`, read-only — database
   `kerno_test`; `session_user = current_user = kerno_test_app`; database
   owner `kerno_test`; the disposable comment; a loopback address and the
   approved port; no elevated attribute; no membership in either direction;
   no owned object; no CREATE on the database or `public` and no TEMP; and
   the owner's guard connection (by pid) visibly holding the workflow lock in
   this same database. Only then are runtime connections allowed, only for
   the pytest workflow, and each one still re-proves the lock.

| Situation | Outcome |
|---|---|
| Runtime pair absent, no `--require-runtime-identity` | Each runtime test is skipped as "runtime identity not configured". Owner tests run. This is an owner regression run, **not** runtime acceptance |
| Runtime pair absent, with `--require-runtime-identity` | Usage error before any test (nonzero) |
| Unsafe or incomplete settings, an unsafe live identity, or a lock that is not proved | The run stops. Never a skip, never an owner fallback |
| Settings valid but the login is refused (role missing, wrong password, CONNECT not granted yet) | Each runtime test fails as a "runtime identity setup failure"; the run is nonzero with or without the flag |
| Final acceptance | `--require-live-database --require-runtime-identity`; both identities verified and **no test skipped at all** |

`--require-runtime-identity` is accepted only together with
`--require-live-database`. Configuration and zero skips are not enough under
it: the run must also have held the owner's exclusive session and verified the
runtime identity live, or it fails and the summary names the missing proof. A
selection containing no runtime test therefore cannot pass under both flags.
Nothing is opened at the end of a run to supply the proof.

### Setup sequence (pending approval; run from the recorded branch SHA)

1. **Approvals, in writing (owner):** the identity `kerno_test_app` confined to
   `kerno_test`; the proposed no-FORCE exception on `users` and
   `webhook_registrations`; the H1/H2 credential residual for synthetic test
   use; the reviewed script revision.
2. **Migration 029 and its round trip (owner, guarded):**

   ```text
   python scripts/migrate_test_database.py upgrade head
   python scripts/migrate_test_database.py downgrade c4d5e6f7
   python scripts/migrate_test_database.py upgrade head
   python -m pytest --require-live-database
   ```

   No runtime settings exist yet, so runtime tests skip by name (row 1).
3. **Create the role (administrator, own PowerShell console, hidden prompt):**

   ```text
   cd J:\Kerno
   python scripts\provision_test_runtime_role.py
   ```

   It refuses, changing nothing, if `.env.test.runtime` or any runtime
   variable (including `KERNO_TEST_RUNTIME_ENV_FILE`) already exists, the
   file would be tracked, a redirecting libpq variable is set, the
   administrator is not a superuser (on PostgreSQL 16+ a CREATEROLE creator is
   implicitly made an ADMIN member of the role it creates), the owner role,
   database, database owner or marker is not as provisioned, PUBLIC holds
   CONNECT on `kerno_test`, or `kerno_test_app` already exists. It then writes `.env.test.runtime` first, runs exactly
   `CREATE ROLE kerno_test_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
   NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD '<SCRAM verifier>'`, verifies the
   attributes, zero memberships in either direction, zero owned objects, no
   CONNECT yet, and that the stored verifier is exactly the one sent, and does
   **not** try a runtime login. On a partial failure it reports what
   exists and deletes nothing (file only: delete the file once the role is
   confirmed absent; role created but unverified: the owner chooses `ALTER
   ROLE kerno_test_app NOLOGIN` or an administrator `DROP ROLE`).
4. **Grant (owner, guarded):**

   ```text
   python scripts/migrate_test_database.py grant-runtime
   ```

   One transaction as `kerno_test`, under the workflow lock: refuses unless
   029 is applied and the role is valid; then `GRANT CONNECT ON DATABASE
   kerno_test TO kerno_test_app` and `CALL
   kerno_bootstrap.apply_runtime_grants('kerno_test_app', 'full')`, which
   also writes the one-row record `kerno_bootstrap.runtime_assignment`; the
   record is checked before commit. (The kickoff proposed recording the role
   and profile with `ALTER DATABASE … SET kerno.runtime_role/profile`; a
   non-superuser owner cannot set or reset custom placeholder settings, so
   029 uses the owner-only record instead.) Between step 3 and this step any
   pytest run ends as a runtime-setup failure (row 4).
5. **Verify the runtime identity:**

   ```text
   python -m pytest tests/security/test_runtime_identity_boundary.py --require-live-database --require-runtime-identity
   ```

   `pg_hba.conf` is never edited; a refused login is reported, not worked
   around.
6. **Final acceptance:**

   ```text
   python -m pytest --require-live-database --require-runtime-identity
   ```

   Zero failures, errors and skips, recorded against the branch SHA, then
   independent review before any merge.

**Withdrawal:** owner `python scripts/migrate_test_database.py revoke-runtime`
(calls `kerno_bootstrap.revoke_runtime_grants`, which clears the record, and
revokes CONNECT, in one transaction); administrator `ALTER ROLE kerno_test_app
NOLOGIN`; owner deletes `.env.test.runtime`. Nothing in the repository drops
the role.

The migration wrapper's exit code 2 now also covers an unmet `grant-runtime`
or `revoke-runtime` precondition (029 missing, the role missing or invalid);
exit 1 covers a failed grant change, which is rolled back whole.

## What never happens

- **Nothing in the repository drops, recreates or resets `kerno_test`.**
  The only thing that creates it is `scripts/provision_test_database.py`, run
  by the owner, and it refuses if the role or database already exists.
  Rebuilding is an owner action: the owner drops it and repeats steps 1–4 (or
  the script) under the same name, `kerno_test`.
- **The name is fixed.** The test process accepts no other database or role
  name, so moving to a different name needs a separately approved code
  change. Renaming the database, or setting up a copy under another name,
  just makes every run refuse.
- Nothing connects to `kerno_dev` from a test or test-migration process.
- **The runtime role is never altered or dropped by the repository.** Only
  `scripts/provision_test_runtime_role.py`, run by an administrator, creates
  `kerno_test_app`, and it refuses if the role already exists. Grants change
  only through `grant-runtime` and `revoke-runtime`.
- Plain `alembic` and the application are unchanged. Only
  `scripts/migrate_test_database.py` targets `kerno_test`.
- **Unsupported pytest options:** `pytest --noconftest`, and `--confcutdir`
  set below `tests/`, both skip the boundary's bootstrap. Every database test
  then errors on its missing fixture.
- **What the guard covers:** it wraps `psycopg2.connect`, which is how every
  connection in this repository is opened. The lower-level entry points are
  not wrapped: `psycopg2._connect`, constructing
  `psycopg2.extensions.connection` directly, SQLAlchemy `creator=`, and other
  PostgreSQL drivers. A safety test fails if any of them appears in `src/`,
  `tests/`, `scripts/` or `migrations/`.
