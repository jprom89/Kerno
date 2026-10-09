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

**Status, 9 October 2026.** The origin/login correction at `ec33858` passed
the owner-run browser acceptance on 8 October (recorded below). The
independent review of that revision found no blocking defect and three
non-blocking items, now addressed on this branch: this status, the export
route's Fetch Metadata gate (`2f19263`, section *Export route* below) and
developer-facing configuration instructions in `DEV_SETUP.md` and the
CLAUDE.md §14 deployment note. The branch is unmerged, pending review of
that incremental diff.

## Required configuration

Set **`KERNO_TRUSTED_ORIGINS` in the Next.js server's runtime environment**.
For local development, this may live in `frontend/.env.local`; the backend's
root `.env` is not the frontend's configuration file. Do not expose it as
`NEXT_PUBLIC_*`. `DEV_SETUP.md` ("Access the app") gives the local values and
the CLAUDE.md §14 deployment note the deployed ones. For example:

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

- Existing GET/HEAD reads remain navigable without Origin, which browsers do
  not send on GET. OPTIONS does not authenticate or mutate a session.
- **Evidence-pack export (`GET /api/export`) is gated separately** by Fetch
  Metadata (section *Export route* below), because the backend appends an
  `export_generated` entry to the tenant's append-only ledger on every call
  and a cross-site top-level navigation carries the Lax session cookie.
- **Frozen-filing download (`GET /api/submissions/runs/[runId]/package`) is
  unchanged.** It returns bytes frozen at Start-run and writes nothing to the
  ledger (`src/api/routers/submissions.py` appends no entry on download), so
  a cross-site navigation to it can at most download the file to the signed-in
  user's own machine. Its authenticated download contract and RBAC remain as
  they were.
- FastAPI bearer-authenticated routes are outside this browser-cookie policy.
  In particular, HMAC-authenticated `POST /api/v1/webhooks/ingest` must not
  require a browser Origin and is unchanged by this ticket.
- There is no exception among the current Next.js POST/PATCH/DELETE handlers,
  including login (which creates a cookie before a session exists) and logout.

## Export route: Fetch Metadata policy (review finding 2.2, 9 October 2026)

`GET /api/export` is relayed to FastAPI only when the request's
`Sec-Fetch-Site` header is exactly `same-origin`, which is what the
dashboard's `ExportButton` `fetch()` sends. The check runs first in the
handler, before the session cookie is read and before `apiFetch` is called.
It needs no configuration and does not consult `KERNO_TRUSTED_ORIGINS`,
`Referer`, `Host` or the request URL.

| `Sec-Fetch-Site` | Meaning | Decision |
|---|---|---|
| `same-origin` | the dashboard's own fetch | relayed |
| `cross-site` | a navigation or request from another site | 403 |
| `same-site` | a sibling subdomain of the dashboard's site | 403 — a subdomain is not the dashboard |
| `none` | address bar, bookmark, or a link opened from outside a browser | 403 — the dashboard never exports this way |
| absent | a browser without Fetch Metadata, or a non-browser client | 403 — no fallback; nothing else is trusted in its place |
| anything else | unrecognised, differently cased, or a list | 403 |

The refusal is `403 {"detail": "a same-origin dashboard request is required"}`
with no attachment. Every response from the route, relayed or refused, carries
`Cache-Control: no-store` and `Vary: Sec-Fetch-Site`; the relayed response
keeps its `Content-Type` and the backend's `Content-Disposition` and status,
so the filename, the body and backend 403/404/429 refusals are as before, and
the backend still records the ledger entry for every permitted export.

Compatibility tradeoffs, stated so they are not rediscovered:

- Browsers without Fetch Metadata cannot use the export button. Chrome and
  Edge have sent it since 76, Firefox since 90 and Safari since 16.4; the
  Origin policy above already assumes browsers of that generation.
- Typing or bookmarking the export URL no longer works (`none`). Use the
  dashboard's button. A `Referer`-based fallback for missing metadata was
  considered and not added, to keep this route's policy one header with no
  configuration; it can be added later if a real client needs it.
- Only this route changed. No other GET is restricted, and nothing about
  authentication, tenant resolution or export-role authorization moved.

Validation of the export gate (9 October 2026, commit `2f19263`, Claude
Code in the owner's interactive Windows session):

| Exact command | Exit | Actual result |
| --- | --- | --- |
| `npx jest --runInBand export-route csrf.test auth-routes mutation-origins` (frontend/) | 0 | 4 suites, 148 tests passed |
| `npx jest --runInBand` (frontend/) | 0 | 16 suites, **204 tests passed**, 0 failed or skipped (182 before this change) |
| `node node_modules/typescript/bin/tsc --noEmit` | 0 | passed |
| `npm run build` with `NEXT_TELEMETRY_DISABLED=1` | 0 | passed; existing middleware-to-proxy deprecation warning |
| `node --test scripts/csrf-browser-resources.test.mjs` | 0 | 13 passed (11 before; two new exit-without-close cases) |
| `node --check scripts/csrf-browser.mjs` | 0 | syntax passed |
| `node scripts/csrf-browser.mjs` from a clean detached worktree at `2f19263` | 0 | Chrome 154.0.8037.98; **6 of 6 checks passed**, including the new export check; `originalFailure: null`; cleanup errors: none; profile retained |

The route tests prove, with the real handler: every non-`same-origin` value
and a missing header are refused with no `cookies()` call, no backend call
and no attachment; a `same-origin` fetch keeps the body, status,
`Content-Disposition` and bearer; backend refusals are relayed. The harness
check proves, in a real browser signed in as the synthetic victim: clicking a
cross-site link to the export is refused (`Sec-Fetch-Site: cross-site`) and
the stub export endpoint is not reached; a direct navigation to the URL is
refused (`none`); the victim cookie is unchanged; then the real export button
on `/dashboard/controls?category=…` succeeds with `same-origin`, reaches the
stub exactly once, and returns the attachment header. The five pre-existing
login/logout checks are unchanged and still pass.

How the harness ran, so the result can be reproduced: the worktree had no
frontend `.env*` file; `node_modules` was a directory junction to the owner
checkout's existing dependencies (nothing installed); Turbopack refuses to
build through that junction, so the production build output was produced
in the owner checkout at the same commit with `npm run build` and copied
into the worktree — the source files are identical by Git. Playwright was
the already installed module of a sibling project (`KERNO_PLAYWRIGHT_MODULE`),
used only for `connectOverCDP`; the browser is the installed Chrome, launched
by the harness with its normal security defaults. A first run before the
commit passed all six checks but exited 1 on cleanup: `stopOwned` waited for
the browser child's `close` event, which Chrome's helper processes (holding
the inherited stderr pipe) delayed past the 5 s bound even though the browser
had exited (verified: no process from the disposable profile remained). The
helper now ends its wait at `exit` as well, which the two new resource tests
cover; the recorded run above is after that change. This remains browser/
frontend validation with a stubbed backend, not full-stack authentication
validation, and it is a Chrome-only run.

## Continuation and validation, 8 October 2026

The accepted authentication correction is unchanged. This continuation changes
only the browser harness and its resource helper/tests, plus this document and
the SEC-REMED-002 status in `NOW.md`. No application routes, database guards,
dependency manifests or lockfiles changed.

The harness registers child ownership and error listeners at launch, attempts
each cleanup independently, and bounds process/listener/connection cleanup to
five seconds per resource. It reports the original test failure separately
from cleanup failures. It only terminates handles it created; it never searches
for processes by name. A profile used by a launched browser is **retained**, even
after parent exit, because descendant release cannot be proven. A never-used
profile may be removed only after checking its real parent path. Timed-out owned
child handles/pipes are unreferenced so they cannot indefinitely hold the test
runner open. Acceptance functions and browser security flags were compared
against the reviewed commit and are unchanged.

### Source and execution environment

All new test results below ran against local validation commit
`1ea531ef367321238808991883926e0c8ffac851`, tree
`8750a65ac658104407bcc83cfd0fd185c55038d7`, based on the reviewed
`0ba9940cea7c080754148c148ee02a4bcd2ea3df`. The published harness commit is
`13ba3e02f1849216028c9c3f0dbbfa37aa636538` with that **identical Git tree**;
the local validation commit is preserved on a local backup branch. Publication
used the existing GitHub connection after CLI push failed before authentication
(`cannot spawn sh`, exit 1). The later evidence commit changes only documentation.

Execution was in the restricted Windows tool account, in the existing isolated
`work/Kerno-sec-remed-002` checkout, with Node 24.14.1, npm 11.11.0 and the
existing Next.js 16.2.10 frontend dependencies. The owner checkout `J:\Kerno`
and its untracked `AGENTS.md` were preserved. No new browser was launched in
this continuation.

### Unit/route tests and frontend checks

Commands ran from `frontend/`, using `C:\Program Files\nodejs\node.exe` and
`C:\Program Files\nodejs\npm.cmd`. These are new runs, not reused October 5
totals.

| Exact command | Exit | Actual result |
| --- | --- | --- |
| `node --test scripts/csrf-browser-resources.test.mjs` | 0 | 11 passed; 0 failed, skipped or cancelled; harmless doubles only |
| `node --check scripts/csrf-browser.mjs` | 0 | Syntax passed; no browser execution |
| `npm test -- --runInBand` | 0 | 15 suites, 182 tests passed; 0 failed or skipped, including helper/route tests |
| `node node_modules/typescript/bin/tsc --noEmit` | 0 | Passed |
| `npm run build` with `NEXT_TELEMETRY_DISABLED=1` | 0 | Passed; existing middleware-to-proxy deprecation warning |

The 11 resource tests cover synchronous/asynchronous launch errors, already
closed and unrelated handles, hanging exit waits, close/kill/listener failures,
continued cleanup after failures, profile retention, original-error identity,
unexpected cleanup exceptions and cleanup-only failures. No real server or
browser is started by these tests.

### Real backend regression: PASSED in the recorded environment

Read-only discovery checked the known Kerno checkout environment paths and
Python launcher metadata. Neither checkout had a virtual environment. The
owner's Python 3.14 user-package directory was **inaccessible by Windows ACL**,
including after a read grant; it was not established to be empty. The registered
WindowsApps Python 3.11 executable was also access-denied. This corrects the
earlier inference that the machine had no usable project dependencies.

With the owner's explicit approval, a new `.venv` was created **only in this
isolated checkout**, using `C:\Python314\python.exe -m venv .venv` (exit 0).
`<project python> -m pip install --no-cache-dir -e '.[dev]'` initially exited 1
on Windows path length inside Mistral. Repeating it with the same interpreter's
Windows extended path (`\\?\C:\...\.venv\Scripts\python.exe`) exited 0;
no system path-limit setting or global package was changed. A metadata probe
verified `src`, `config`, `tests` and `tests._database_safety` resolve from this
checkout and the editable installation points here, not an older checkout.

Python was 3.14.3, pytest 9.1.1, psycopg2-binary 2.9.13, python-dotenv 1.2.4,
FastAPI 0.143.0, Mistral 3.1.0 and pypdf 6.19.0. Both runs below used the same
source SHA above and the unchanged TEST-SAFETY-001 workflow, with
`KERNO_TEST_ENV_FILE=J:\Kerno\.env.test`. The existing loader alone read that
file; its contents were not copied or displayed.

| Exact command from repository root | Exit | Actual result |
| --- | --- | --- |
| `<project python> -m pytest --require-live-database` | 1 | 1,464 passed, 13 failed, 49 setup errors, 0 skipped, 225 warnings; 141.43 s |
| `<project python> -m pip install --no-cache-dir 'SQLAlchemy==2.0.51'` | 0 | Local environment aligned with the version already in `uv.lock`; no manifest/lock changes |
| `<project python> -m pip check` | 0 | No broken requirements |
| `<project python> -m pytest --require-live-database` | 0 | **1,526 passed, 0 failed, 0 errors, 0 skipped**, 225 warnings; 226.91 s |

The first run's 13 failures were 12 schema-parity tests and one database-safety
unit test: freshly resolved SQLAlchemy 2.1.4 tried to import `psycopg`, whereas
the installed driver and safety guard are psycopg2. All 49 setup errors were
Windows access denials on the shared pytest temporary directory. The second
run used SQLAlchemy **2.0.51 from this repository's existing lockfile** and a
fresh task-owned temporary directory, set through `TEMP`, `TMP` and `TMPDIR`.
No test, assertion, skip condition, connection guard or approval was disabled.
No alternative database driver was installed to evade the guard. The successful
result applies to this recorded dependency environment; an unconstrained fresh
install that selects SQLAlchemy 2.1.4 still has the recorded incompatibility.

The terminal reported `kerno_test@127.0.0.1:5432/kerno_test`; the existing guard
verified target identity and held/re-proved its exclusive lock. No migrations,
provisioning, password recovery, administrator connection or application backend
server was run. Existing offline safety/provisioning unit tests used their
synthetic doubles and refusal probes. Warnings include Python/SlowAPI and
Alembic deprecations and short synthetic test-HMAC keys; none were suppressed.

Private supporting logs, package metadata, exact interpreter/temp paths and the
one owner-run browser command are in the assessment workspace's
`outputs/SEC-REMED-002/` directory, outside the checkout. Original assessment
artifacts were not changed.

### Browser/frontend acceptance: owner-run, 8 October 2026 — PASSED

The restricted tool context could not launch the harness (its syntax was
checked, exit 0, but it was not executed there), so the owner ran the
supplied revision-pinned PowerShell command in a normal, non-administrator
PowerShell window. The owner reported:

- tested source: `ec33858d0352eabc2f3f37318e85b2c262d45b51`, the login/origin
  correction before the export gate;
- browser: Chrome 154.0.8037.98;
- build, harness and overall exit codes: 0;
- the cross-site login checks (signed out and signed in), the legitimate
  login and production cookie-flag check, the opaque `null`-Origin form
  check, and the logout checks (cross-site refused, legitimate succeeds)
  passed;
- cleanup reported no errors; the disposable profile the browser used was
  retained, as designed.

The result is attributed to the owner's run and its artifacts
(`execution.json`, `browser-acceptance.log` in the assessment workspace's
`outputs/SEC-REMED-002/`, outside the checkout); the reviewer of `ec33858`
did not have those artifacts and did not execute the run. It is
**browser/frontend validation with a stubbed backend**, not full-stack
authentication validation; the browser log also records unrelated background
browser activity, so the browser as a whole was not network-isolated. The
run applies to `ec33858`; the export gate added afterwards has its own run,
recorded in the *Export route* section above.

## Historical validation, 5 October 2026

The following records the earlier implementation attempt retained in reviewed
commit `0ba9940cea7c080754148c148ee02a4bcd2ea3df`. Current results are above.

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

Use the one inspected owner-run command in the current private handoff, rather
than repeating the failed tool-account launch.

Run from a clean isolated frontend checkout with dependencies installed and
`npm run build` completed. The harness refuses frontend `.env` files, starts
the real production Next.js frontend with only synthetic configuration, and
uses `http://localhost:<frontend-port>` versus
`http://127.0.0.1:<attack-port>`. The distinct host sites, rather than merely
their ports, provide the cross-site test; the harness asserts
`Sec-Fetch-Site: cross-site`. All listeners, including the disposable browser's
debugging connection, bind to loopback. Normal loopback secure-context behavior
is used; this is not deployment HTTPS validation. No PostgreSQL is involved.

### Historical real-backend attempt: could not start

At that time the tested interpreter could not import pytest or the backend
dependencies. That attempt did not start the workflow or connect to a database.
It did not establish that no other project environment existed. The October 8
discovery, approved local environment and completed regression supersede this
blocker; the historical exit code remains recorded above.
