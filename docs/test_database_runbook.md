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
- **The comment:** the test process reads it on every live connection and
  refuses a database that lacks it. It marks the database as the one the
  owner provisioned as disposable. It is **not** a tamper-proof
  administrator signature: `kerno_test` owns the database, so the role
  itself could also set or clear the comment.
  To withdraw approval in a way `kerno_test` cannot undo, the administrator
  runs `ALTER ROLE kerno_test NOLOGIN`. Clearing the comment
  (`COMMENT ON DATABASE kerno_test IS NULL`) also makes every run refuse.
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
   `ALTER ROLE kerno_test NOLOGIN`.
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
- **During a workflow,** the lock is proved again at each of these points:
  - every new database connection the process opens;
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

## What never happens

- Nothing creates, drops, recreates or resets `kerno_test`. Rebuilding it is
  an owner action. Repeat steps 1–4 on a new name, or drop it yourself.
- Nothing connects to `kerno_dev` from a test or test-migration process.
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
