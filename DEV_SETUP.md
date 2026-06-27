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
```

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

- Dashboard login:
  `http://localhost:8001/dashboard/login.html`

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
