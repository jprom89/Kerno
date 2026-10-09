# SEC-REMED-003: bounded evidence intake before multipart processing

Both upload entry points now authenticate first and bound the request second.
Only after both do they process the multipart body. `POST /api/evidence`
(Next.js) and `POST /api/v1/evidence` (FastAPI) each count the bytes that
actually arrive against a total limit before anything parses them. The backend
then reads at most `MAX_EVIDENCE_UPLOAD_BYTES + 1` file bytes. The 10 MiB file
limit, deduplication, tenant and RBAC checks, evidence linking and the
same-transaction ledger entry are unchanged.

Base: `94b3a018035311e2fd3c80d98add945aad1b2a02`, fetched from `origin/main`
(PR #12 merged). Branch: `security/sec-remed-003-evidence-intake`.
Implementation commits:

- `5804458d2d48f7e6fff41dd214dca36014c37690` is the fix and its tests.
- `41ee7a65fb319bfbbe86e6deeb3d1ade1441fdda` splits the upload form's handler
  to meet the 40-line rule and adds doc comments. It changes frontend files
  only.

Finding: `resource-exhaustion.evidence-buffering`, scope SEC-REMED-003 only.

**Status, 9 October 2026:**

- **Source-established** at `75f2bf18` by the assessment. That assessment did
  not reproduce it dynamically.
- **Reproduced at route level** here against the unfixed code at `94b3a01`.
- **Fixed and tested** at `5804458` and `41ee7a6`. A real production
  Next.js server check passed at both, with a stub backend over loopback.
- **Not covered:** this was not a browser test or a deployment test.
- **Pending independent review.** The branch is unmerged.

The finding's original severity (medium) and confidence (medium) are unchanged.
Its limitations are unchanged too: hosting and proxy limits were never
measured, and no uncontrolled exhaustion test was run.

## What the code did at `94b3a01`

The FastAPI, Starlette and Next.js rows were checked against the installed
source. undici ships inside the Node binary, so its row was checked by
running it on Node 24.14.1 instead.

| Path | Behaviour |
|---|---|
| Next.js `POST /api/evidence` | After the SEC-REMED-002 origin check it called `request.formData()`. On Node 24.14.1 that pulled every chunk of an instrumented stream and held the file in memory before resolving. This happened before any session lookup. The proxy never verified the cookie; `apiFetch` only forwarded it. |
| FastAPI `POST /api/v1/evidence` | The endpoint declared `File()`/`Form()` parameters. FastAPI 0.138.1's request handler calls `await request.form()` *before* `solve_dependencies`, so the body was parsed and spooled before the token was decoded. Starlette's defaults applied: 1,000 files, 1,000 fields and 1 MiB per non-file field, with files spooled to disk past 1 MiB and no total limit. The handler's `file.file.read()` then read the whole spool, and only `extract_text` compared it with 10 MiB. |
| Starlette 1.3.1 cleanup | The parser closes its spooled files only when it raises its own `MultiPartException` or an `OSError`. A file part whose closing boundary never arrives produces a form without that file, and the file stays open. Any other exception leaves every file open. |
| Next.js 16.2.10 body handling | A route handler receives the Node request as a lazy stream (`NextRequestAdapter.fromNodeNextRequest`). The only buffering Next.js does itself is the middleware body clone (`server/body-streams.js`). That clone buffers without backpressure and silently truncates at 10 MB. It runs only for paths the middleware matches, here `/dashboard/:path*`. |

**One difference from the finding's wording.** The finding says an
authenticated user can additionally reach the backend's complete-file read. On
the direct FastAPI path, the multipart parse and disk spool also happened for
**anonymous** callers, because parsing preceded authentication. The
complete-file read itself still required an upload role.

## What changed

**Next.js `POST /api/evidence`** (`frontend/app/api/evidence/route.ts`,
`frontend/lib/upload-intake.ts`), in order:

1. **Origin check (SEC-REMED-002), unchanged.** 503 when unconfigured, 403 when
   the origin is untrusted.
2. **Session.** No cookie gives a 401 without a backend call. Otherwise the
   token goes to `GET /api/v1/auth/me`. A backend 401 gives a 401; any other
   failure gives a 502. Cookie presence alone is never treated as
   authentication. The backend remains the authority on the upload itself.
3. **Headers.** 415 unless the media type is `multipart/form-data`, matched
   case-insensitively. 400 without a boundary, or with a Content-Length that is
   not plain ASCII digits. 413 when the declared length exceeds
   `EVIDENCE_UPLOAD_MAX_BODY_BYTES`.
4. **Body.** The raw body is read and counted chunk by chunk. A 413 is returned
   at the first chunk that takes the total past the limit, and reading stops. A
   stream that fails (client gone) gives a 400. The reader is released either
   way.
5. **Forward.** The received bytes go to FastAPI **unparsed**, with the
   caller's own Content-Type and `request.signal`. A client that leaves
   mid-forward cancels the backend request, and the proxy returns 400.

Every refusal in steps 2–4 carries `Connection: close`, because the body was
not read in full. Without it, Node keeps reading and discarding whatever the
client still sends. The route never parses or rebuilds the form. It buffers
up to the limit; it does not stream. The old module comment claiming
untouched streaming with no limit is corrected.

**FastAPI `POST /api/v1/evidence`** (`src/api/evidence_upload_intake.py`,
`src/api/routers/evidence.py`). FastAPI resolves dependencies in declaration
order, so the endpoint's parameter order is the boundary:

1. Token, then role. 401 or 403 before any body byte is read.
2. `receive_evidence_upload`:
   - **Headers.** The same checks as the proxy: 415, 400 or 413, each with
     `Connection: close`.
   - **Body.** Received and counted against the same total; 413 with
     `Connection: close`, or 400 on disconnect.
   - **Parse.** Only the complete body is parsed, by Starlette's parser
     limited to 1 file, 2 fields and `EVIDENCE_UPLOAD_MAX_FIELD_BYTES` per
     field. A violation is a 400.
   - **Shape.** A misshapen form gets a 422 with a string detail: missing
     file, a file sent as text, missing or empty `record_type`, an unknown or
     repeated field.
   - **File.** At most `MAX_EVIDENCE_UPLOAD_BYTES + 1` file bytes are read; a
     413 above the limit.
   - **Cleanup.** Every spooled file the parser created is closed in a
     `finally`, whatever the outcome.
3. `get_conn`: a database connection is leased only after intake completes.
   Before this change it was leased after FastAPI's own parse. Neither version
   holds one while the body arrives.
4. Extraction, deduplication, insert and the KER-107 ledger entry are
   unchanged. The new-record write moved into `_store_new_upload`, with the
   same statements in the same order on the same transaction, so both
   functions meet the 40-line rule. The OpenAPI document gets the multipart
   body shape through `openapi_extra`, since there are no `File()` parameters
   to describe it.

## Limits

| Constant (`config/constants.py`) | Value | Bounds |
|---|---|---|
| `MAX_EVIDENCE_UPLOAD_BYTES` | 10 MiB (unchanged) | the file itself |
| `EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES` | 64 KiB | boundaries, part headers (a long filename included), `record_type` and `title` |
| `EVIDENCE_UPLOAD_MAX_BODY_BYTES` | file limit plus envelope allowance | the whole request, on both paths |
| `EVIDENCE_UPLOAD_MAX_FIELD_BYTES` | 4 KiB | one non-file field; `record_type` is VARCHAR(64) |
| `EVIDENCE_UPLOAD_MAX_FILES` / `_FIELDS` | 1 / 2 | parts per upload |

`frontend/lib/evidence-upload-limits.ts` mirrors the file and body limits, and
`test_the_next_js_proxy_limits_match_the_backend_constants` fails on drift.
`test_a_maximum_size_file_with_maximal_metadata_is_accepted_under_the_real_limits`
sends, under the real limits, a file of exactly 10 MiB with a 255-character
filename, a 64-character `record_type` and a 4 KiB title. It proves the
envelope was not given the file's limit.

## Compatibility changes

- A non-multipart upload is now 415; it was 422. A multipart media type in
  another case is now accepted; Starlette compared it case-sensitively and
  answered 422.
- Missing or misshapen fields are 422 with a **string** `detail`, which the
  dashboard shows as-is. Before, they were FastAPI's list-shaped validation
  detail. Unknown or repeated field names are now refused; before, they were
  ignored, or the last value won.
- A second file, a third field, or a field over 4 KiB is now a 400. Before,
  1,000 files and 1 MiB fields were accepted.
- The proxy makes one extra backend call per upload, `GET /api/v1/auth/me`.
- Refusals sent before the body is read close the connection. A client still
  sending at that moment may see a **connection reset instead of the 413**.
  This was observed with Node on loopback, whether or not the stream is
  cancelled. So `EvidenceUpload` now refuses a file over the limit before
  sending, and recovers instead of staying in "Uploading…" when the
  connection drops.

## Evidence

### Before the fix: the new tests against the code at `94b3a01`

| Run | Result |
|---|---|
| `python -m pytest tests/unit/api/test_evidence_upload_bounds.py`. The router was unchanged; a placeholder for the new module only defined the patched limit names. | exit 1: **36 failed, 5 passed** of 41 |
| `npx jest --runInBand __tests__/evidence-upload-route.test.ts`. The route was unchanged; only the limits module was added. | exit 1: **28 failed, 5 passed** of 33 |
| `npx jest --runInBand __tests__/evidence-upload.test.tsx` against the old form | **2 failed, 1 passed** of 3 |

The backend failures were the reported behaviour:

- **Token cases.** Anonymous, forged and malformed tokens all read the whole
  scripted body before the 401; the auditor role read it before the 403.
- **Unbounded reads.** Every file was read with `read(-1)`, never
  `read(MAX + 1)`.
- **Leases.** A file one byte over the limit leased a database connection
  before its 413.
- **Body limits.** Over-limit bodies, of declared or unknown length, were read
  to the end.
- **Part counts.** A second file was accepted by the parser and reached the
  database.
- **Spooled files.** A file part that never finished, or a malformed tail,
  left its spooled file open. A client that disconnected mid-file had already
  caused a spooled file to be created.

The 5 that passed are contract guards: valid text, PDF and duplicate uploads,
cleanup after a successful upload, and the missing-boundary refusal, which
Starlette already raised before reading. On the frontend:

- Anonymous and forged-session uploads were parsed and forwarded. They got
  201 where 401 or 502 was expected.
- A non-multipart body was parsed or made the route throw, instead of getting
  a 415.
- The route re-serialised every body instead of forwarding the caller's
  bytes.

### After the fix

The backend rows ran on the backend code of `5804458`, which `41ee7a6` does
not change. The frontend rows ran at `41ee7a6`.

| Gate | Command | Exit | Result |
|---|---|---|---|
| Backend, full | `python -m pytest --require-live-database -p no:cacheprovider -q -rfEs` | 0 | 1,577 collected, **1,577 passed**; 0 failed, errored or skipped |
| Backend, new | `test_evidence_upload_bounds.py` and `test_sec_remed_003_evidence_intake.py` | 0 | 44 + 7 passed; the 7 KER-406 integration tests still pass |
| Frontend | `npx jest --runInBand` | 0 | 18 suites, **241 passed** |
| TypeScript | `node node_modules/typescript/bin/tsc --noEmit` | 0 | no errors |
| Lint | `npx eslint` on every changed frontend file | 0 | no findings |
| Build | `npm run build` (Next.js 16.2.10, Turbopack) | 0 | compiled; TypeScript passed |

The backend count is read from pytest's progress output, because the extra
`-q` on top of the repository's own `-q` suppressed the summary line. The
total was confirmed with `--collect-only`. It equals the 1,526 recorded for
SEC-REMED-002 plus the 51 new tests. The live tests ran only against the approved
`kerno_test` database through the TEST-SAFETY-001 guards. Nothing connected to
`kerno_dev`, and no migration was involved.

### Real production server: `scripts/upload-intake-server.mjs`

This is a dedicated test process, not a browser and not a deployment. The
harness drives `next start` over raw loopback sockets with a stub backend and
synthetic tokens. Each run used a clean detached worktree of the commit, which
held no frontend `.env` files, with a `node_modules` junction and a copy of
the production build made from identical source. No frontend file changed
after that build, and the checkout matched the commit. The junction was
removed before the worktree each time.

| Check, at `41ee7a6` | Status | Server closed | Backend calls |
|---|---|---|---|
| Anonymous upload, body left incomplete | 401 in 28 ms | yes | none |
| Forged-session upload, body left incomplete | 401 in 31 ms | yes | session check only |
| Declared over-limit body | 413 in 8 ms | yes | session check only |
| Unknown-length over-limit body | 413; the server closed after the client wrote 11.3 of 14.8 MB | yes | session check only |
| Small text upload | 201, byte-identical by SHA-256 | — | session check, upload |
| Exactly 10 MiB file | 201, byte-identical by SHA-256 | — | session check, upload |

Overall: exit 0, 6 of 6, no cleanup errors. The earlier run at `5804458`
gave the same six outcomes. This confirms what the route tests could not:

- Next.js hands the route an unbuffered stream.
- The server answers before an unauthenticated body completes.
- Node honours `Connection: close`.
- A maximum-size upload passes through the real server intact.

The harness was not run against the unfixed build, so it is evidence for the
fixed SHAs only.

## Remaining limitations

- **PDF parsing is not bounded by this change.** Decoded-output, page and CPU
  limits for pypdf stay a separate control. A 10 MiB PDF can still be costly
  to extract. This fixes transport size and does not establish parser-bomb
  resistance.
- **No reception deadline on either path.** A slow authenticated sender holds
  a coroutine and up to the body limit until the server's own timeouts. Node
  defaults to 300 s per request; uvicorn has its own. The webhook intake has a
  deadline; this route does not.
- **Memory per accepted upload.** The proxy holds up to the body limit, plus
  one copy while concatenating it. The backend holds the body plus a joined
  copy for a moment, the spooled file (1 MiB in memory, the rest on disk) and
  up to `MAX + 1` read bytes. That is roughly three times the limit per
  concurrent authenticated upload. There is no concurrency cap; gateway rate
  limiting (SEC-05) is still deferred.
- **Server-level backend behaviour was not run.** Uvicorn's handling of the
  early `Connection: close` refusals has not been tested; the backend tests
  are in-process ASGI.
- **Refusals outside this change.** The backend's 401 and 403 come from shared
  dependencies and do not ask to close. The Next.js origin 403 comes from the
  SEC-REMED-002 helper and leaves Node to drain the unread body, as on every
  route.
- **Hosting limits** were not measured and are not relied on. Vercel documents
  a 4.5 MB request-body limit for Functions. If the proxy is deployed there,
  uploads above it would be refused by the platform before this code runs, so
  the 10 MiB limit would not be reachable through the proxy. This has not been
  verified here.
- **Private Starlette attribute.** The cleanup reads
  `_files_to_close_on_error`, an internal of Starlette 1.3.1, which is pinned
  in `uv.lock`. The cleanup tests fail if it changes.
- **Lockfile drift.** The ordering was verified on the installed FastAPI
  0.138.1. `uv.lock` pins 0.139.0, a known gap recorded in CLAUDE.md §17.

Kerno is not claimed to be penetration-tested.

## §11 file reviews

Each block follows CLAUDE.md §11. "Tests" names the suites that exercise the
file; every listed test passes.

### ✅ File 1 Review — config/constants.py

**What this file does:** It holds every named number the backend uses,
including the new upload limits.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | unchanged |
| All functions have docstrings | ✅ | no functions added |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | this is where the numbers are named |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** `test_the_envelope_allowance_covers_every_accepted_field_at_its_limit` and `test_the_next_js_proxy_limits_match_the_backend_constants` — ✅ pass.
**Open questions:** None — ready to proceed.
**Proceed to File 2?** Yes — all gates pass, no open questions.

### ✅ File 2 Review — src/api/evidence_upload_intake.py

**What this file does:** It accepts an uploaded document only from an
authorised caller, and only within its size limits, before anything else
happens to it.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | checked with `ast` |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | only `+ 1` (permitted) and HTTP status codes, as in every router |
| No function longer than 40 lines | ✅ | checked with `ast` |
| Tenant isolation rule followed (if DB file) | N/A | no database access |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** `tests/unit/api/test_evidence_upload_bounds.py`, 44 ✅; `tests/integration/test_sec_remed_003_evidence_intake.py`, 7 ✅.
**Open questions:** It relies on Starlette's private `_files_to_close_on_error` (see Remaining limitations); tests pin it.
**Proceed to File 3?** Yes — all gates pass; the open point is recorded and tested.

### ✅ File 3 Review — src/api/routers/evidence.py

**What this file does:** It lets a team upload, list, link and unlink evidence.
Uploads now go through the bounded intake.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | `upload_evidence` was already 58 lines before this change; it was split, and `_store_new_upload` was added |
| Tenant isolation rule followed (if DB file) | ✅ | `set_tenant_context` before every query; the tenant comes from the verified JWT |
| TenantContextMissingError raised on null/empty context | ✅ | via `set_tenant_context`, unchanged |

**Tests:** `test_evidence.py`, 2 ✅; `test_evidence_upload_bounds.py`, 44 ✅; `test_rbac_gates.py` ✅; KER-406 and SEC-REMED-003 integration, 14 ✅ (live DB).
**Open questions:** None — ready to proceed.
**Proceed to File 4?** Yes — all gates pass, no open questions.

### ✅ File 4 Review — frontend/lib/evidence-upload-limits.ts

**What this file does:** It tells the upload form and the proxy how large an
upload may be.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | constants only, each with a doc comment |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | the named definitions |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** the Python parity test ✅; the route and component tests, which mock it with small values ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 5?** Yes — all gates pass, no open questions.

### ✅ File 5 Review — frontend/lib/upload-intake.ts

**What this file does:** It checks that an upload comes from a real session
and stays within size before the proxy reads the file.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | every function has a doc comment (`41ee7a6` added the last two) |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | statuses are named constants |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** `__tests__/evidence-upload-route.test.ts`, 34 ✅; the real-server harness, 6 of 6 ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 6?** Yes — all gates pass, no open questions.

### ✅ File 6 Review — frontend/app/api/evidence/route.ts

**What this file does:** It passes the browser's evidence list and upload
requests to the backend, now refusing unauthenticated or oversized uploads
first.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | the streaming claim was corrected |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** `evidence-upload-route.test.ts`, 34 ✅; `mutation-origins.test.ts`, 70 ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 7?** Yes — all gates pass, no open questions.

### ✅ File 7 Review — frontend/components/EvidenceUpload.tsx

**What this file does:** It is the upload form. It now rejects a file that is
too large without sending it, and recovers from a dropped connection.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | `handleUpload`, `uploadForm` and `reportOutcome` have doc comments; the component itself is documented by the module docstring, as before |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | the size check and the recovery took `handleUpload` from 36 to 47 lines; `41ee7a6` split it into 21, 9 and 23 lines. The component function was already over 40 lines because of its markup, which is unchanged |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** `__tests__/evidence-upload.test.tsx`, 3 ✅.
**Open questions:** None — ready to proceed.
**Proceed to File 8?** Yes — all gates pass, no open questions.

### ✅ File 8 Review — frontend/scripts/upload-intake-server.mjs

**What this file does:** It starts the real built frontend on loopback and
checks that it refuses bad uploads early and passes good ones through
unchanged.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | every function has a doc comment |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | named constants |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** `node --check` ✅; the runs at `5804458` and `41ee7a6` each passed 6 of 6, exit 0.
**Open questions:** None — ready to proceed.
**Proceed to File 9?** Yes — all gates pass, no open questions.

### ✅ Files 9–13 Review — tests

`tests/unit/api/test_evidence_upload_bounds.py`, `tests/integration/test_sec_remed_003_evidence_intake.py`,
`frontend/__tests__/evidence-upload-route.test.ts`, `frontend/__tests__/evidence-upload.test.tsx`,
and `frontend/__tests__/mutation-origins.test.ts`.

**What these files do:** They prove both upload paths refuse early, read only
what the limits allow, clean up after themselves, and still accept valid
uploads.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | non-obvious helpers and test doubles are documented. Test functions and small private helpers have none, as in the repository's other test files and per the standing style rule. This is named here as an explicit exemption, not hidden |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | named test limits |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | the integration test reads under `SET LOCAL app.current_tenant_id` on `kerno_test` |
| TenantContextMissingError raised on null/empty context | N/A | |

**Tests:** all ✅ as listed under Evidence. `mutation-origins.test.ts` changed only to answer the new session check in its backend double; its assertions are otherwise unchanged.
**Open questions:** None — ready to proceed.
**Proceed?** Yes — all gates pass, no open questions.
