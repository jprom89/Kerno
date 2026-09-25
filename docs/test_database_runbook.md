# Test database runbook — `kerno_test` (TEST-SAFETY-001)

This runbook covers one disposable PostgreSQL database, `kerno_test`, and one
restricted login role, also `kerno_test`. Live-database tests and test
migrations may run against this target and nothing else. They never run
against `kerno_dev` and never fall back to `DATABASE_URL`, `.env` or libpq
defaults.

**Status on 25 September 2026: NOT provisioned, NOT approved.** Until the
owner completes the steps below and records approval, every live-database test
is skipped with an explicit reason. `python -m pytest --require-live-database`
and `scripts/migrate_test_database.py` refuse to run.

Everything in steps 1–4 is a one-time **owner/administrator action**. Neither
Claude Code nor any script in this repository provisions, drops, recreates or
repairs a database, and neither uses administrator credentials.

---

## 1. Pre-checks — stop if anything already exists

Connect as the PostgreSQL administrator to the maintenance database on this
machine's server:

```text
psql -h 127.0.0.1 -p 5432 -U postgres -d postgres
```

```sql
\set ON_ERROR_STOP on
SELECT rolname FROM pg_roles    WHERE rolname = 'kerno_test';   -- must return 0 rows
SELECT datname FROM pg_database WHERE datname = 'kerno_test';   -- must return 0 rows
```

If either query returns a row, **stop**. Do not drop, rename or reuse the
object; report it for review. None of the statements below uses `IF NOT
EXISTS`, `OR REPLACE` or `DROP`, so an existing object makes them fail rather
than be overwritten.

A read-only check on 25 September 2026 found neither object.

## 2. Create the restricted login role

```sql
CREATE ROLE kerno_test LOGIN
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\password kerno_test
```

`\password` prompts for the password twice and sends only a hash, so the
password doesn't end up in the shell or psql history. Use only letters,
digits, `-` and `_`, so it can go into a URL unencoded. `migrations/env.py`
passes the URL through Python's `ConfigParser`, which misreads `%`.

Do not grant this role anything else. In particular, do not give it
`SUPERUSER`, `CREATEDB`, `CREATEROLE`, `BYPASSRLS`, `REPLICATION` or
membership in any role. The test process checks all of these on every run
and refuses a role that has any of them.

## 3. Create the database, install the extension, mark it disposable

```sql
CREATE DATABASE kerno_test OWNER kerno_test;
REVOKE ALL ON DATABASE kerno_test FROM PUBLIC;
\connect kerno_test
CREATE EXTENSION vector;
COMMENT ON DATABASE kerno_test IS 'kerno:disposable-test-database';
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
- **The comment:** this is the database-side half of the owner's approval.
  The test process reads it on every live connection and refuses a database
  that lacks it. To withdraw approval later, run
  `COMMENT ON DATABASE kerno_test IS NULL;`.
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
  `git check-ignore .env.test` must print a rule. The safety tests check this.
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

The following never authorise a test database:

- `DATABASE_URL`, which is removed from the test process at startup;
- the ordinary `.env`, whose loading is switched off with
  `PYTHON_DOTENV_DISABLED=1` before any application import;
- libpq defaults.

The URL must state host, port, database and user explicitly. The host must
be a single loopback address. Any setting that could redirect the connection
is refused, whether it appears as a URL parameter (`hostaddr`, `service`,
`options`, …) or as an environment variable (`PGHOSTADDR`, `PGSERVICE`,
`PGOPTIONS`, …).

## 6. Recording approval

Approval has three parts, and all three are required.

1. **The database comment** from step 3, set by the administrator.
2. **`KERNO_TEST_DATABASE_APPROVAL`**, which must equal the exact identity
   `kerno_test@127.0.0.1:5432/kerno_test`. If the URL points anywhere else,
   the two no longer match and every run refuses.
3. **A written record in `NOW.md`**, made by the owner or on their explicit
   instruction:
   *"`kerno_test@127.0.0.1:5432/kerno_test` approved as a disposable test
   database by <owner> on <date>."*

The code checks parts 1 and 2 on every run. Part 3 is the human record.

## 7. First use

```text
python scripts/migrate_test_database.py upgrade head      # build the schema as kerno_test
python -m pytest --require-live-database                  # full suite, live tests required
python scripts/migrate_test_database.py downgrade z1a2b3c4
python scripts/migrate_test_database.py upgrade head      # migration round trip on the disposable target
```

Every run first checks, read-only, that the live connection is what was
approved:

- the database name;
- `session_user` and `current_user`;
- a TCP loopback server address and the approved port;
- no privileged role attributes and no role memberships;
- database ownership;
- the disposable-target comment.

It then takes one session-level advisory lock,
`pg_try_advisory_lock(1262834254, 1413829460)` (the ASCII codes of `KERN` and
`TEST`), without waiting. That lock allows only one workflow at a time: one
pytest run or one migration run, never both, and never two of either.

- **A second workflow** fails immediately. Its message names the holder's pid
  and application name.
- **During a workflow,** the lock is checked again before every fixture seed
  or cleanup and before every migration step. If it is lost, the run stops.
- **The lock is released** when the workflow finishes, and PostgreSQL releases
  it anyway if the process dies.
- **Parallel pytest workers** (for example `-n` with pytest-xdist) are refused
  against the test database.

## What never happens

- Nothing creates, drops, recreates or resets `kerno_test`. Rebuilding it is
  an owner action. Repeat steps 1–4 on a new name, or drop it yourself.
- Nothing connects to `kerno_dev` from a test or test-migration process.
- Plain `alembic` and the application are unchanged. Only
  `scripts/migrate_test_database.py` targets `kerno_test`.
- `pytest --noconftest` is unsupported. It skips the boundary's normal
  bootstrap, and every database test then errors on its missing fixture.
