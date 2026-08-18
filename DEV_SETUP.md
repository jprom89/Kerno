# Kerno – Local Dev Quickstart

## Prereqs
- Windows
- Python 3.x on PATH (`python --version`)
- PostgreSQL 18 running locally
- Repo cloned to `J:\Kerno`
- Database `kerno_dev` created, with pgvector 0.8.3 installed and extension enabled:
  - `CREATE EXTENSION IF NOT EXISTS vector;`

## Environment

Kerno uses `python-dotenv`. Copy `.env.example` to `.env` and adjust as needed:

```powershell
cd J:\Kerno
copy .env.example .env
```

By default, `.env` should contain:

```text
DATABASE_URL=postgresql://kerno_dev:kerno_dev@localhost:5432/kerno_dev
KERNO_ENV=development
```

`KERNO_ENV=development` is required, not optional. The legacy static dashboard
and the interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are only
registered when it is set to exactly `development`; with it unset or set to
anything else, every one of those URLs returns 404. That is deliberate — see
§17 Ticket B in CLAUDE.md.

No manual `export`/`set` is required; `load_dotenv()` is wired into the app.

## Start the API (dev)

From PowerShell:

```powershell
cd J:\Kerno
python -m uvicorn src.api.app:app --reload --port 8001
```

Notes:
- `--reload` is for local dev only.
- If port 8001 is taken, pick another free port and update the URLs below.

## Access the app

The dashboard you actually want is the Next.js one, which runs as its own
application:

```powershell
cd J:\Kerno\frontend
npm run dev
```

- Dashboard login: `http://localhost:3000/login`
- Organisation: `dev-tenant` (required since KER-408 — an email is unique only
  within one organisation, so it alone does not identify an account)
- To add or amend register entries, log in as `compliance_lead@kerno.local`
  with `$DEV_SEED_PASSWORD` from your `.env`. Any role can read the register;
  only `compliance_lead` and `vciso` can write, and the server returns 403 to
  everyone else regardless of what the UI shows.

**The DORA register lives at `/dashboard/register`** (KER-410) — list, detail,
create and amend. Every addition and amendment writes a KER-107 ledger entry
attributed to the logged-in user (KER-409).

**Submission windows and runs live at `/dashboard/submissions`** (KER-411) —
open windows, start a run, run history. A malformed register or run id is a
404, not a 500 (KER-412). The legacy static dashboard at
`http://localhost:8001/dashboard/login.html` is served only when
`KERNO_ENV=development`, and it is otherwise frozen: it keeps its JWT in
localStorage and is not the surface being developed.

### Before any manual click-through, restart the API

A running `uvicorn` does not reload, so it is probably serving whatever commit
you started it on. Restart it before verifying anything by hand — a stale
process has twice reported a feature missing that was in fact present.

## Database & migrations

- Postgres: 18
- Extension: `vector` 0.8.3 installed in `kerno_dev`
- Alembic: all 14 migrations applied, current head:
  - `014_add_tenant_credentials` (revision `o0p1q2r3`)

If you need to re-run migrations:

```powershell
cd J:\Kerno
alembic upgrade head
```

## Tests

Run unit tests from the repo root:

```powershell
cd J:\Kerno
pytest
```

Expected: `180 passed, 0 failed` on `main` at commit `632b170`.

## Notes
- Port `8000` was unavailable on this machine, so local dev was validated on `8001`.
- pgvector was verified with a working similarity query in PostgreSQL.
- Browser login was validated against the seeded local account.
