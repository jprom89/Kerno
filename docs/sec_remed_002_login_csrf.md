# SEC-REMED-002: login and browser mutation origin policy

The change prevents a cross-site login request from installing an attacker's
session in a victim's browser. `frontend/lib/csrf.ts` checks an explicit
server-side allowlist before a handler parses credentials or other request
bodies, reads the session cookie, forwards to FastAPI, or sets a cookie.
Login independently requires the `application/json` media type (case
insensitive, with optional parameters). Cookie attributes and backend
authentication, tenant resolution and RBAC are unchanged.

Base: `54d9adf100bf33ec8aa8a2b859176df21f438e07`, fetched from `origin/main`.
Finding: `csrf.login-session-replacement`; scope: SEC-REMED-002 only.

## Required configuration

Set **`KERNO_TRUSTED_ORIGINS` in the Next.js server's runtime environment**.
For local development, this may live in `frontend/.env.local`; the backend's
root `.env` is not the frontend's configuration file. Do not expose it as
`NEXT_PUBLIC_*`. For example:

```text
KERNO_TRUSTED_ORIGINS=https://app.example.test,https://preview.example.test
```

These are synthetic documentation examples, not deployment values. List only
origins you control and intend to trust. A canonical origin is scheme plus
hostname and an optional non-default port, without a trailing slash, path,
query, fragment, user information or wildcard. Browser-serialized lower-case
hostnames and schemes are required; omit the default `:443`/`:80` port. Local
development must explicitly list `http://localhost:3000` (or the actual local
origin). A preview domain must be added by its exact origin, never a wildcard.

The whole configuration fails closed if missing, blank, or if any entry is
invalid (including empty comma-separated entries). Every protected request
then returns 503. An absent, blank, `null`, malformed or unlisted request
`Origin` returns 403 when configuration is valid. A trusted-origin login
without the JSON media type returns 415. None of these rejections parses the
body, contacts the authentication backend, or changes a session cookie.

There is no fallback to Host, the request URL, Forwarded, X-Forwarded-Host,
X-Forwarded-Proto, Referer, backend CORS or SameSite. Preserve the browser's
Origin through a reverse proxy. A non-browser caller of these Next.js
mutation endpoints must also supply an explicitly trusted Origin; there is
no missing-Origin exemption. Origin is a browser CSRF boundary, not a new
credential: non-browser clients can set headers and backend auth/RBAC still
decides their authority.

## Protected routes and explicit exceptions

The same guard is first in all 11 current Next.js mutation handlers:

- POST `/api/auth/login` (also requires JSON), `/api/auth/logout`;
- POST `/api/evidence`, `/api/evidence/[recordId]/links`;
- DELETE `/api/evidence/[recordId]/links/[controlId]`;
- POST `/api/overrides`, `/api/recalculate`, `/api/recommendations/generate`;
- POST `/api/register`, PATCH `/api/register/[entryId]`;
- POST `/api/submissions/runs`.

Multipart evidence and bodyless logout/recalculation remain supported for
trusted origins. This change adds no upload byte limit or other evidence-size
remediation. The route matrix test enumerates the actual exported mutations
and requires any newly introduced handler to be accounted for.

Explicit exceptions:

- Existing GET/HEAD reads and downloads remain navigable without Origin.
  Evidence-pack export and frozen-filing download have existing backend audit
  side effects; those download records do not change session identity or the
  underlying business record. Their current authenticated download contracts
  and RBAC remain unchanged. OPTIONS does not authenticate or mutate a session.
- FastAPI bearer-authenticated routes are outside this browser-cookie policy.
  In particular, HMAC-authenticated `POST /api/v1/webhooks/ingest` must not
  require a browser Origin and is unchanged by this ticket.
- There is no exception among the current Next.js POST/PATCH/DELETE handlers,
  including login (which creates a cookie before a session exists) and logout.

## Executed validation, 5 October 2026

Commands below ran in `frontend/`, except the backend command at repository
root. Node and npm were the installed `C:\Program Files\nodejs` executables.
No scan, reviewer agents, monitoring, database provisioning or real backend
server was started.

| Layer | Exact command | Exit | Result |
| --- | --- | --- | --- |
| Focused helper/routes | `npm test -- --runInBand csrf.test.ts auth-routes.test.ts mutation-origins.test.ts` | 0 | 3 suites, 126 tests passed, no failures or skips |
| Complete Jest suite | `npm test -- --runInBand` | 0 | 15 suites, 182 tests passed, no failures or skips |
| TypeScript | `node node_modules/typescript/bin/tsc --noEmit` | 0 | Passed |
| Production build | `npm run build` with `NEXT_TELEMETRY_DISABLED=1` | 0 | Passed; existing middleware-to-proxy deprecation warning |
| Browser harness syntax | `node --check frontend/scripts/csrf-browser.mjs` (repository root) | 0 | Syntax only; does not establish browser acceptance |
| Real backend regression | `C:\Python314\python.exe -m pytest --require-live-database` | 1 | Could not start: `No module named pytest`; zero backend tests executed |

The route tests use actual Next.js handlers with synthetic requests and a
mocked authentication backend. They demonstrate no credential forwarding,
body parsing or Set-Cookie on rejected origins/media types, including
misleading origin suffixes, missing/null origins and existing victim cookies.
The trusted-origin JSON login test passes with the expected credential payload,
redacted response body and unchanged production cookie flags. Sibling tests
preserve backend permission refusals and trusted multipart/bodyless operations.

### Browser acceptance: PENDING

`node scripts/csrf-browser.mjs` was attempted with the installed Playwright
1.62.1 and both installed Edge and Chrome, using new disposable profiles and
loopback-only test servers. Both runs exited 1 **before browser assertions**:

- Edge: target crashed during launch; Windows encryption/access errors and
  GPU-process failure (`-1073741790`).
- Chrome: GPU-process failure (`-1073741790`, access denied), followed by
  `GPU process isn't usable` and a 30000 ms local CDP connection timeout.

No browser assertion passed. The initial Edge attempt used Playwright's
default automation arguments with its Chromium sandbox explicitly enabled;
the retained harness launches the installed browser directly with normal
browser security defaults. It supplies only headless/first-run/profile/local
debugging arguments and does not disable the browser sandbox, web security,
site isolation, certificate verification or origin checks. No browser or
system infrastructure was installed to bypass these failures.

Missing prerequisite: an execution account/environment in which an installed
Chromium browser can start its normal sandboxed processes using a fresh
disposable profile. The current tool account cannot do that reliably.

The retained harness is **browser/frontend validation with a stubbed backend,
not full-stack authentication validation**. Once browser startup is available,
its assertions cover a real cross-site text/plain login while signed out and
signed in, an opaque `Origin: null` form, trusted-origin login and cookie flags,
rejection of trusted-origin text/plain login, cross-site logout rejection, and
legitimate logout. It checks that rejected logins do not reach the stub and
that the synthetic victim cookie and dashboard identity remain unchanged.
These assertions are supplied for execution, not reported as completed.

Example PowerShell setup (use actual existing paths; nothing is installed):

```powershell
$env:KERNO_PLAYWRIGHT_MODULE = '<absolute path to an existing playwright module>'
$env:KERNO_BROWSER_EXECUTABLE = '<absolute path to an installed Chromium browser>'
$env:KERNO_BROWSER_WORK_DIR = '<absolute path to a disposable writable work directory>'
node scripts/csrf-browser.mjs
```

Run from a clean isolated frontend checkout with dependencies installed and
`npm run build` completed. The harness refuses frontend `.env` files, starts
the real production Next.js frontend with only synthetic configuration, and
uses `http://localhost:<frontend-port>` versus
`http://127.0.0.1:<attack-port>`. The distinct host sites, rather than merely
their ports, provide the cross-site test; the harness asserts
`Sec-Fetch-Site: cross-site`. All listeners, including the disposable browser's
debugging connection, bind to loopback. Normal loopback secure-context behavior
is used; this is not deployment HTTPS validation. No PostgreSQL is involved.

### Real-backend acceptance: BLOCKED

The available Python interpreter lacks pytest, psycopg2, SQLAlchemy, dotenv
and FastAPI. The test workflow did not start, read database credentials or
connect to any database. No application migrations or provisioning were run.
Once the already-provisioned environment is available, run the repository's
unchanged `python -m pytest --require-live-database` workflow against only
owner-approved `kerno_test`, preserving every TEST-SAFETY-001 guard. Do not
substitute `kerno_dev`, administrator access or an unguarded regression run.

Implementation and route/build checks are complete; the ticket is **not fully
closed**. Browser acceptance and real-backend regression remain as above.
