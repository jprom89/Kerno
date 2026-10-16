# Evidence upload limits — gate G6 (PILOT-READY-004)

**Status (16 October 2026): Checkpoint A — implemented and tested offline on the
branch `pilot/pilot-ready-004-upload-limits` at `8b34a39` (draft PR, pending
independent review).** No database contact, grant switch or deployment was part of it.
Checkpoint B, the synthetic live run on `kerno_test`, has not run; it needs a
separate approval of the sequence in §9. Nothing here is validated on a pilot
host: §8 lists what must still be measured there.

Issue: [jprom89/Kerno#22](https://github.com/jprom89/Kerno/issues/22). Gate:
G6 in `docs/pilot_readiness_kickoff.md` §4.2. It builds on SEC-REMED-003
(`docs/sec_remed_003_evidence_intake.md`), which already authenticates before
the body, counts the bytes that arrive and limits the multipart shape; all of
that is unchanged.

## 1. What G6 required, and what is now in the code

| Requirement | Now |
|---|---|
| Absolute reception deadline on both upload paths | API 30 s, proxy 60 s, from the first body read, never reset by a chunk → 408 |
| Bounded admission covering buffering and processing, no waiting queue | 2 active uploads per API process and 2 per Next.js process; a full house is a 503 with `Retry-After` at once |
| Permission checks before the body | Unchanged on the API; the proxy now also refuses a role that may not upload, from the backend-verified identity, before any body byte |
| PDF page, decoded-output and time limits, enforced while processing | 500 pages before any page is read; 16 MiB per stream during or right after its decoding; 64 MiB decoded per document; 10,485,760 characters of text as fragments arrive; 30 s wall clock, after which the worker is killed and reaped |
| A deadline that really stops the work | PDF text is read in a separate worker process, which is killed, waited for and cleaned up before the request goes on — never an abandoned thread |
| Extraction before any database lease | The upload is admitted, received and extracted in a dependency declared before `get_conn` |
| Safe, stable errors | Fixed string details; no pypdf exception text, document content or server path |
| Rejected uploads leave nothing | No `context_records` row, no ledger entry, no lease |
| Successful uploads unchanged | Same text normalisation, `content_hash`, deduplication, verified actor and same-transaction ledger entry |

## 2. Order of operations

**API, `POST /api/v1/evidence`** (`src/api/routers/evidence.py`,
`src/api/evidence_upload_processing.py`, `src/api/evidence_upload_intake.py`):

1. Token (401) and role (403), from the verified JWT, before anything else.
2. Refusals the headers alone decide: 415 not multipart, 400 no boundary or a
   malformed `Content-Length`, 413 a declared length over the body limit.
   They take no upload slot, so they keep their own status at a full house.
3. Admission: one of `EVIDENCE_UPLOAD_MAX_ACTIVE` slots for this process, or
   503 with `Retry-After: 5` and `Connection: close` at once, before any body
   byte is read.
4. Bounded reception (SEC-REMED-003 counting and multipart limits) within
   `EVIDENCE_UPLOAD_RECEPTION_DEADLINE_SECONDS` → 408; a client that leaves → 400.
5. Text extraction in a worker thread. Plain text (`.txt`, `.md`, `.csv`) is
   decoded in-process as before. A PDF is read by a separate, killable worker
   process (§5).
6. Only then does `get_conn` lease a connection. Fingerprint, deduplication,
   the insert and its ledger entry are unchanged.
7. The slot is released after the endpoint's connection has been returned,
   on every outcome. A request cancelled while its PDF worker runs waits until
   that worker has been reaped before its slot is released.

**Proxy, `POST /api/evidence`** (`frontend/app/api/evidence/route.ts`,
`frontend/lib/upload-intake.ts`):

1. Browser origin (SEC-REMED-002).
2. Session and role: `GET /api/v1/auth/me` verifies the cookie's token, and
   the role it reports must be in `EVIDENCE_UPLOAD_ROLES`. The request and its
   answer body run under `EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS`. No
   cookie or a rejected token → 401; another role → 403; failure or timeout → 502.
3. Header refusals, as on the API.
4. Admission: one of `EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS` slots, or 503
   with `Retry-After` at once.
5. Body reception: counted (413), within `EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS`
   (408), and stopped at once if the client leaves (400).
6. Forward: the received bytes and the caller's own `Content-Type`, unchanged
   and unparsed, under `EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS` for the request and
   its answer body (504); a client that leaves aborts the backend request (400).
   A broken connection to the API is a controlled refusal: the API's busy 503
   with its `Retry-After` when its status and headers got through, otherwise
   502. A whole answer that is not JSON is also a 502. Nothing is retried.
7. The backend's status, JSON body and a delay-seconds `Retry-After` are
   relayed. The slot is released on every outcome, and no timer or abort
   listener outlives the request.

## 3. Limits

Backend values are in `config/constants.py`, frontend values in
`frontend/lib/evidence-upload-limits.ts`. `tests/unit/api/test_evidence_upload_policy_consistency.py`
fails if the shared values drift: the cap, `Retry-After`, the upload roles,
and the forward deadline exceeding API reception plus PDF extraction.

| Constant | Value | What it counts, and when it is enforced | Scope |
|---|---|---|---|
| `EVIDENCE_UPLOAD_MAX_ACTIVE` | 2 | Uploads holding a slot, from admission (after token, role and header checks) to the end of the request | per API process |
| `EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS` | 2 | The same, in the proxy, from admission until the API's answer is read | per Next.js server process |
| `EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS` | 5 | `Retry-After` on a full house | — |
| `EVIDENCE_UPLOAD_RECEPTION_DEADLINE_SECONDS` | 30.0 | Wall time from the first body read to the last byte, not reset per chunk | per request |
| `EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS` | 60,000 | The same, browser → proxy; a full 10.06 MiB body needs about 176 KB/s | per request |
| `EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS` | 10,000 | The proxy's session check, request and answer body | per request |
| `EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS` | 75,000 | Proxy → API request and answer body; exceeds API reception (30 s) plus PDF extraction (30 s) | per request |
| `PDF_EXTRACTION_DEADLINE_SECONDS` | 30.0 | Wall time from launching the worker to its result, interpreter start and pypdf import included; then kill and reap | per PDF |
| `PDF_MAX_PAGES` | 500 | Pages pypdf finds in the page tree, checked before any page's text is extracted | per PDF |
| `PDF_MAX_STREAM_OUTPUT_BYTES` | 16 MiB | Decoded output of one stream. FlateDecode, LZWDecode and RunLengthDecode stop as their output grows past it; every stream's output is also checked right after it decodes, which holds ASCII85Decode (up to four bytes per input byte) to the limit whenever its decoding fits under the memory ceiling. For ordinary ASCII85 data CPython's decoder needs about 38 bytes of memory per output byte, so the 512 MiB ceiling refuses such a stream first, from about 12.5 MiB of output. JBIG2Decode is disabled | per stream |
| `PDF_MAX_DECODED_BYTES` | 64 MiB | Decoded bytes of every stream of the document, charged as each stream finishes decoding; decoded data is cached, so it accumulates | per PDF |
| `PDF_MAX_EXTRACTED_TEXT_CHARACTERS` | 10,485,760 | Characters of extracted text across pages: charged as pypdf reports each fragment (extraction stops mid-page) and again on each page's returned text; a Form XObject's text once at any nesting depth. Parity with the largest plain-text upload | per PDF |
| `PDF_WORKER_MEMORY_LIMIT_BYTES` | 512 MiB | The worker's OS ceiling: committed memory (Windows Job Object) or address space (`RLIMIT_AS`, POSIX), installed and read back before pypdf is imported or the upload read | per worker process |
| `PDF_WORKER_CPU_LIMIT_SECONDS` | 31 | The worker's CPU-time ceiling (Job Object user time, `RLIMIT_CPU`); a backstop for a worker the API could not kill | per worker process |
| `PDF_PARSER_MINIMUM_VERSION` | `6.14.2` | The oldest pypdf the worker parses with: the exact version tested here | — |
| `PDF_WORKER_MAX_OUTPUT_BYTES` | 41,943,552 | Worker stdout the API will read: two header lines plus the largest text at four bytes a character; past it the worker is killed → 503 | per worker process |

The SEC-REMED-003 limits are unchanged: a 10 MiB file, a 64 KiB multipart
envelope, one file and two fields of at most 4 KiB.

The memory bound for PDF work in one API process is therefore
`EVIDENCE_UPLOAD_MAX_ACTIVE × PDF_WORKER_MEMORY_LIMIT_BYTES` = 1 GiB of worker
memory, plus the API's own buffers for at most two uploads.

## 4. Responses

Every refusal is JSON with a string `detail`; the dashboard shows it as
"Upload failed: …" (and every 413 as "That file is too large.").

| Status | `detail` | When | Path |
|---|---|---|---|
| 503, `Retry-After: 5`, `Connection: close` | `evidence uploads are busy; try again shortly` | full house | API and proxy, before any body byte |
| 408, `Connection: close` | `upload was not received within the time limit` | reception deadline | API and proxy |
| 403 | `your role is not permitted to perform this action` | the token's role may not upload | API (unchanged, from `require_role`), before any body byte |
| 403, `Connection: close` | `your role is not permitted to perform this action` | the role the backend verified may not upload | proxy, before any body byte |
| 504 | `the evidence service did not respond in time` | forward deadline | proxy |
| 502 | `the evidence service could not be reached` | connection to the API broke | proxy |
| 502 | `the evidence service's answer could not be read` | the API's whole answer is not JSON | proxy |
| 422 | `the PDF could not be read` | malformed or unreadable, including an AES-encrypted PDF with an empty user password when the optional `cryptography` package is absent | API |
| 422 | `encrypted PDFs are not supported` | a user password pypdf cannot open, AES included; an empty-password PDF is read as before | API |
| 422 | `the PDF has more than 500 pages` | page cap | API |
| 422 | `the PDF exceeds the text-extraction limits` | stream, decoded or text budget; any `MemoryError` or pypdf `LimitReachedError` while parsing, even one pypdf catches; a worker that dies after its ready line without a complete result line | API |
| 422 | `the PDF could not be processed within the time limit` | extraction deadline | API |
| 503 | `evidence processing is unavailable; try again later` | a server-side fault, never the document's: the worker could not start or get its private directory; it could not install or verify its ceiling or parser protections, or gave no trusted ready line; or, after a trusted ready line, its result was itself `unavailable`, named another ceiling or version, was malformed or partial with exit 0, was followed by a nonzero exit, carried bytes after a refusal or text that does not decode, or overflowed the output cap | API |

Everything else keeps its status and detail: 415, 400 and 413 from intake,
the form-shape 422s, the unsupported-type 422, the empty-text 422, and the
proxy's 401, 502 and 400. PDF refusals are 422 rather than 413 on purpose:
413 stays the byte-size refusal, and the dashboard replaces every 413 detail
with its own size message.

## 5. The PDF worker

`src/services/pdf_text_worker.py` runs as a child process;
`src/services/pdf_text_extraction.py` starts and supervises it.

**Start.** `python -P <worker file> <limits JSON>` with the same interpreter as
the API, a private working directory made for this worker alone
(`tempfile.mkdtemp`, owner-only), stdin the PDF, stdout the result channel and
stderr discarded. On POSIX it leads a new session, so its process group can be
killed. The environment is an allowlist, never a copy: the interpreter's own
variables (`PATH`, `SYSTEMROOT`, `APPDATA`, `HOME`, `TEMP` and the like) and
only absolute entries of `PYTHONPATH`, `PYTHONHOME` and `PYTHONUSERBASE`. No
database URL, JWT secret, API key, token or password can pass. The worker
imports only the standard library and pypdf: no `config`, `src` or `dotenv`.

**Before it reads the upload, in this order.** Any failure answers
`unavailable` (503) and nothing is parsed.

1. Install and read back the OS ceiling. On Windows the worker assigns itself
   to a new Job Object, which nests under any job it is already in, with a
   committed-memory limit, a user-time limit, at most one active process (so it
   can start no other program) and kill-on-close. All four flags and both
   values are read back with `QueryInformationJobObject`. On POSIX it sets
   `RLIMIT_AS` and `RLIMIT_CPU`, never raising an existing lower limit, and
   reads them back. Any other platform answers `unavailable`.
2. Import pypdf, which must be at least `6.14.2`.
3. Require every mechanism it relies on: the seven per-stream limit globals in
   `pypdf.filters`, `decode_stream_data`, `JBIG2DEC_BINARY` (then set to `None`,
   so no external program is ever started), `PageObject.extract_xform_text`,
   and the pypdf error classes. Python must provide `sys.monitoring`, which
   needs 3.12 or later, and a free tool id.
4. Set the limits, wrap the stream decoder in the decoded-bytes budget, wrap
   form extraction so a form's text is charged once, and install the observer:
   a `sys.monitoring` `RAISE` callback that marks the document over the limits
   for any `MemoryError` or `LimitReachedError` raised while parsing, caught
   or not.
5. Self-test: with a tiny limit, an oversized Flate stream must raise
   `LimitReachedError` during `get_data()` and a small one must pass through
   the budget wrapper. The observer must have seen the first, and must also
   see a deliberately raised and swallowed `MemoryError`. Then the real limits
   are set and the marks cleared.
6. Write `ready <ceiling> <pypdf version>`, then read the upload.

**Result.** `<outcome> <ceiling> <pypdf version>`, then the text for `text`.
The API trusts a document verdict only after a ready line naming a recognised
ceiling and a version at or above the minimum, with a matching result line.
A worker that dies after its ready line without writing a complete result
line was stopped while parsing under its ceiling, so its document is refused
with a 422. On Windows, CPython 3.14.3 can die with `0xC0000005` when an
allocation fails at the ceiling. A worker that dies before its ready line is a server fault (503), as is any other untrusted
or malformed answer (§4).

**Lifecycle.** A writer thread feeds stdin. A reader thread reads stdout in
64 KiB chunks up to `PDF_WORKER_MAX_OUTPUT_BYTES`; past that the worker is
killed. The calling thread waits at most `PDF_EXTRACTION_DEADLINE_SECONDS`.
On every outcome, in a `finally`:

1. kill the worker if it is still running (and its process group on POSIX);
2. wait for it;
3. join both threads;
4. close the pipes;
5. remove the private directory.

Only after that does `extract_pdf_text` return or raise, and only then is the
upload's slot released.

## 6. Capacity scope

- **API.** The cap is a counter in each API process. One uvicorn process admits
  2 uploads. `--workers N` admits 2N per host, and R replicas admit 2NR. Each
  admitted PDF upload runs at most one worker process of at most 512 MiB, so
  worker memory per host is at most 2N × 512 MiB.
- **Threads.** Extraction waits in anyio's default thread pool (40 tokens per
  event loop, shared with every sync endpoint), using at most 2 per process.
- **Proxy.** The cap is per Next.js server process. On a serverless platform
  each instance has its own count, so the cap bounds an instance, not the
  deployment. A deployment-wide bound must come from the platform's own
  concurrency settings.
- **Not a rate limit.** Repeated uploads within the cap are not throttled;
  SEC-05 rate limiting stays deferred.

## 7. Evidence (Checkpoint A, offline)

**Environment.** Windows 11 Home 10.0.26200, CPython 3.14.3 (the repository's
interpreter), pypdf 6.14.2, Node 24.14.1, Next.js 16.2.10, Jest 30. All
backend runs used the offline wrapper, which points `KERNO_TEST_ENV_FILE` and
`KERNO_TEST_RUNTIME_ENV_FILE` at absent files and unsets every database
variable, so every live test skips with its reason and nothing connects.

**Base and suites.** Base: main `14c8f9a59f2d9d174e3f829dbd1392d0b2fb4bf2`
(PR #21's merge). Tested code: `8b34a399d2ae46db08bbed9b907ff6f5c9258d5f`. The
documentation commit that follows it changes no executable file.

| Check | Base `14c8f9a` | Tested `8b34a39` |
|---|---|---|
| Backend, whole suite, offline (`offline_pytest.sh -W ignore::DeprecationWarning -rs`) | exit 0; 2342 passed, 476 skipped, 57 deselected | exit 0; **2733 passed, 478 skipped, 58 deselected**, 0 failed or errored. Every skip is "no approved test database is configured": the two new live full-profile G6 tests account for +2, and the new `pilot_profile` test for +1 deselected |
| Backend unit tests alone (`tests/unit -rs`) | — | 2718 passed, **0 skipped** (no platform skip remains, as a zero-skip Checkpoint B run requires) |
| Jest (`npx jest`) | 347 passed, 21 suites | **415 passed**, 23 suites |
| `npx tsc --noEmit` | exit 0 | exit 0 |
| ESLint on the changed frontend files | — | exit 0 |
| ESLint on the whole frontend | 6 errors in 4 untouched files | the same 6 errors in the same 4 files (`app/login/page.tsx`, `components/EvidenceList.tsx`, `components/RegisterEntryDetail.tsx`, `components/SubmissionRunView.tsx`) |
| `next build` in a clean detached worktree of `0556df4` (no frontend `.env` files, `KERNO_*` unset; its frontend source differs from `8b34a39` only in the harness script) | exit 0 | exit 0; the only warning is the pre-existing middleware-to-proxy deprecation |

**Real production server** (`frontend/scripts/upload-intake-server.mjs` at
`8b34a39` on that build, `next start` over loopback with a stub backend and
synthetic tokens; no browser, database or deployment). Run once under each
serving profile, 13 of 13 checks passed each time, exit 0, no cleanup errors:

| Check | `full` | `pilot` |
|---|---|---|
| Anonymous, forged-session and read-only-role uploads answered while their body is incomplete | 401 / 401 / 403, `Connection: close` | 401 / 401 / 403 |
| Declared and unknown-length over-limit bodies | 413 / 413 (closed after 11.3 MB written) | 413 / 413 |
| Small and exactly-10 MiB uploads forwarded byte-identical (SHA-256) | 201 / 201 | 201 / 201 |
| Both slots held mid-body, one more upload | 503 at 17 ms, `Retry-After: 5`, nothing forwarded | 503 at 11 ms |
| A slot freed by a client that left, used again | 201 | 201 |
| The API's busy 503 sent with `Connection: close` before it reads the body (64-byte and 4 MiB file) | 503 / 503 | 503 / 503 |
| Body trickled one byte every 2 s | 408 at 60,011 ms, 29 bytes read, nothing forwarded | 408 at 60,014 ms |
| Backend that never answers | 504 at 75,012 ms; the backend saw the proxy abandon its request | 504 at 75,022 ms |

**Mutation checks.** Every check used a script that kept one uniquely named
backup, restored the original bytes in `try/finally`, asserted that the
mutation site matched exactly once, verified byte-for-byte restoration, and
reran the unmutated tests.

- **Header order:** moving the header refusals back after admission failed
  all 4 header-order tests with `assert 503 == <status>`; restored, they pass.
- **Worker:** 12 mutants killed, among them observer ignored, observer blind
  to `LimitReachedError`, form re-report charged, no after-decode check,
  jbig2dec left on, `ActiveProcessLimit` not requested, and no
  end-of-document check.
- **Parent:** 10 mutants killed, among them unfiltered `PYTHONPATH`, cwd back
  in the shared temp root, directory never removed, no new session, no group
  kill, whole environment passed, and a harness that keeps the ceiling.
- **API and proxy:** 5 mutants killed, among them `asyncio.shield` restored,
  transport failures rethrown, and the busy relay removed.
- **Late proxy change:** restoring the throw for a non-JSON answer fails its
  test.

**The memory ceiling itself** (the real worker's production
`install_ceiling(worker_limits())`, Windows Job Object, 512 MiB = 536,870,912 bytes):

| Probe | Result |
|---|---|
| Allocate 1 MiB at a time until refused | `MemoryError` after 493 steps; peak commit 511.11 MiB, within one step of the ceiling |
| Steps of 64 KiB, 4 MiB, 16 MiB | peaks 511.99, 508.70, 496.16 MiB; `MemoryError` each time |
| A document that grows memory gradually (ToUnicode map 4,000,000 × 400), real worker through `extract_pdf_text`, 9 runs | 422 `the PDF exceeds the text-extraction limits`; peaks 505.86–507.70 MiB, never above 512 MiB; worker reaped, pipes closed, threads joined, private directory removed |
| The same worker on the same document with only its ceiling installation replaced | grew past 640 MiB and was killed by the watcher (640.97 and 641.25 MiB), proving the ceiling is what stopped it |
| The same through the real app and a fake connection | 422 with no lease, statement, insert or ledger call; slot released; then a normal PDF in the same app stored (201) |

**Early rejection, separately.** Shapes that ask for one huge allocation are
refused long before the ceiling. The `/Differences` glyph name 4000 × 400,000
and the same inside a Form XObject are refused in about 0.4 s at about 35 MiB,
because the single allocation they request is denied. Without any ceiling the
same `/Differences` PDF reached 1,572.5 MiB. A legitimate 500-page document of
9,680,999 characters reads in 7.5–10.6 s at about 61 MiB.

**Swallowed failures.** pypdf 6.14.2 catches exceptions at these real sites,
and the worker now refuses the document at each of them:

- `crlf_space_check`;
- the `Q` and `cm` handlers;
- the font size;
- three glyph-map lookups;
- the charmap decode fallback;
- the Form XObject handler;
- a dictionary value read;
- `LimitReachedError` re-raised as `PdfReadError`.

Each site was tested three ways. pypdf alone swallows the planted failure and
returns text as if it had succeeded: complete text at 8 sites, and visibly
less text at the line-break check, the string decoding and the Form XObject.
The worker with its observer switched off returns that same text. The real
worker with its observer refuses the document with `over-limits` at every
site. A real worker process with one
planted failure is refused with 422 through `extract_pdf_text`.

**Isolation, read from outside the running worker.** The worker's environment
block, current directory and command line were read through its PEB after it
wrote its ready line, while it was still parsing a 100-page document. The
parent held 12 credential sentinels (among them `DATABASE_URL`,
`KERNO_MIGRATION_DATABASE_URL`, `KERNO_JWT_SECRET`, `PGPASSWORD`, `PGPASSFILE`
and `MISTRAL_API_KEY`) and every `.env.example` name. The block held exactly
12 allowlisted names and none of those names or values. The worker's directory
was its private directory, removed afterwards. `.env` files with sentinel
values placed beside, above and in the worker's directories never reached its
block. A control child that does load them shows every value, so the method
detects a loader.

**Linux, supplementary only.** Run in WSL2 (Linux 6.18) on Python 3.10.6,
which is below the supported floor, against the real worker before the
observer was added: every run printed `ready posix-rlimit 6.14.2`.

| Document | Outcome |
|---|---|
| ordinary text | `text` |
| `/Differences` amplifier | `over-limits` |
| Form XObject amplifier | `over-limits` |
| ToUnicode 4M × 400 | `over-limits` at a 502 MiB peak RSS, a clean `MemoryError` |
| 480-page document under a 1 s CPU ceiling | killed by `RLIMIT_CPU` |

With the observer in place, the same interpreter answers `unavailable` with
no ready line, the intended fail-closed outcome. Linux on a supported
interpreter (3.12 or later) is still outstanding (§8).

## 8. Not verified here, and remaining limitations

**Deployment validation (G6 "Validate", outstanding):**

- **Host request-body limits.** Vercel documents 4.5 MB for Functions, which
  would make a 10 MiB upload unreachable through the proxy.
- **Function and proxy timeouts.** They must allow the 60 s reception and 75 s
  forward deadlines.
- **Worker and replica counts.** These multiply both caps (§6).
- **Memory headroom.** The host must hold 2 × 512 MiB of PDF workers per API
  process.
- **Linux on a supported interpreter.** The `RLIMIT_AS`/`RLIMIT_CPU` ceilings,
  the process-group kill and the observer, run on CPython 3.12 or later. Here
  they ran only on Python 3.10 under WSL, before the observer existed, and the
  POSIX group kill only with stand-in processes.

**Compatibility:**

- **Python 3.12 or later is needed for PDF processing.** `pyproject.toml` still
  allows 3.11, where every PDF upload is refused 503 (fail closed). Pinning
  the interpreter belongs to G3.
- **pypdf 6.14.2 or later.** `pyproject.toml` now declares the floor; `uv.lock`
  still has no pypdf entry (G3).

**Known residuals:**

- An exception raised and caught entirely inside C code never reaches the
  observer; pypdf's handlers are Python, so every known site is covered.
- If memory is so exhausted that the observer's callback cannot run, the mark
  can be missed. In practice that path kills the worker, which is a 422.
- ASCII85 decoding is memory-hungry in CPython. A stream of 16 MiB of zeros,
  which ASCII85 writes compactly, is accepted at about 407 MiB of commit. An
  ordinary 12.0 MiB stream was accepted at about 493 MiB, and a 12.7 MiB one
  was refused at the ceiling (422), although both are under the 16 MiB
  per-stream limit. For ordinary ASCII85 data, the ceiling is the binding
  limit from about 12.5 MiB of output. The refusal is still safe; legitimate
  ASCII85 content streams are far smaller.
- An API answer of 201 whose body is cut off reaches the browser as 502,
  although the record may be stored. Retrying is safe: uploads deduplicate on
  `content_hash`.
- Text containing a lone surrogate still fails with a 500 at fingerprinting.
  This is pre-existing and unchanged.
- A native cancellation during the endpoint's database work abandons the sync
  endpoint's thread. This is pre-existing for every sync endpoint.

## 9. Proposed Checkpoint B (needs its own approval)

Only the existing owner `kerno_test` and runtime `kerno_test_app` at
`127.0.0.1:5432/kerno_test`, through the same exclusive guard, at the reviewed
SHA. No migration is needed (head stays `e6f7g8h9`), and executable files stay
frozen throughout. Stop at the first failure and report the profile and head
then in force, with no automatic cleanup, switch-back, rerun or recovery.

| Step | Command | Profile | Expect |
|---|---|---|---|
| 0 | prechecks: HEAD = approved SHA, clean tracked tree, origin matches, credential files present (not read), no stray database variables | — | — |
| 1 | `python scripts/migrate_test_database.py current` | full | `e6f7g8h9` |
| 2 | `python -m pytest --require-live-database --require-runtime-identity -rs` | **full** | every collected test passes, 0 failed / errors / skips, pilot-only tests deselected; owner, lock, runtime identity and `full` grant profile verified |
| 3 | `python scripts/migrate_test_database.py grant-runtime --profile pilot` | → **pilot** | `(profile pilot)` |
| 4 | `python -m pytest --require-live-database --require-runtime-identity --runtime-profile pilot -rs` | **pilot** | every `pilot_profile` test passes, the new pilot upload test included; 0 failed / errors / skips; `pilot` verified |
| 5 | `python scripts/migrate_test_database.py grant-runtime` | → **full** | `(profile full)` |
| 6 | `python -m pytest tests/security/test_runtime_identity_boundary.py tests/integration/test_runtime_serving_profile_startup.py tests/integration/test_pilot_ready_004_upload_limits.py --require-live-database --require-runtime-identity -rs` | **full** | restoration proof plus the G6 live tests; 0 failed / errors / skips; `full` verified |
| 7 | `python scripts/migrate_test_database.py current` | full | `e6f7g8h9` |

The live G6 tests are in `tests/integration/test_pilot_ready_004_upload_limits.py`:

- In the full app, a real PDF is stored once on the runtime login with its
  text, hash, actor and one ledger entry, and its re-upload is deduplicated.
- An over-limit and a malformed PDF each leave no row, no ledger entry and no
  runtime lease.
- In the pilot app, the same stored-once upload and one refusal leave no trace.

## 10. §11 file reviews

The code is reviewed per file below; tests are grouped. Gate facts were
checked with an AST scan, LF line endings with a byte count, and every
listed test passes offline.

### ✅ File 1 Review — config/constants.py

**What this file does (one sentence a non-engineer can read)**
It holds the named numbers for how many uploads may run at once, how long each
stage may take, and how much work reading a PDF may do.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | unchanged |
| All functions have docstrings | ✅ | no functions |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | the file is where they are named; each new value has its rationale |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- tests/unit/api/test_evidence_upload_policy_consistency.py — ✅ passes (frontend and backend values agree)
- every test using the limits — ✅ passes

**Open questions before next file**
The values are implementation defaults chosen for the pilot. They are to be
measured against representative documents on the pilot host (G18).

**Proceed to File 2?**
Yes — all gates pass, no open questions.

### ✅ File 2 Review — src/services/pdf_text_worker.py (new)

**What this file does (one sentence a non-engineer can read)**
It is the separate, disposable program that reads a PDF's text inside hard
memory, time and work limits, and reports only a short verdict and the text.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | What/Why/How |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | tunable limits arrive from config/constants.py through the parent; Windows API values (9, 0x2, 0x8, 0x100, 0x2000, 1, 10,000,000) and the six `sys.monitoring` tool ids are named module-private constants with their source, like `_EXTERNAL_ID_MAX_LENGTH` in evidence_intake.py |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | no database access; the worker receives no credentials |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- tests/unit/services/test_pdf_text_worker.py — ✅ passes (in-process logic, real worker processes, swallow sites, ceilings, JBIG2, ASCII85, form charging)

**Open questions before next file**
Its ceiling, observer and process-group behaviour on Linux with a supported
interpreter is not yet run (§8).

**Proceed to File 3?**
Yes — all gates pass; the open item is deployment validation, not a code gap.

### ✅ File 3 Review — src/services/pdf_text_extraction.py (new)

**What this file does (one sentence a non-engineer can read)**
It starts the PDF worker with a clean environment in its own directory, waits
for it no longer than the deadline, and always kills, waits for and cleans up
after it before it answers.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | What/Why/How |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | all from config/constants.py |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | no database access |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- tests/unit/services/test_pdf_text_extraction.py — ✅ passes (real worker refusals, deadlines, kill and reap, output cap, environment and isolation read from outside, ceiling proof)

**Open questions before next file**
None — ready to proceed.

**Proceed to File 4?**
Yes — all gates pass, no open questions.

### ✅ File 4 Review — src/services/evidence_intake.py

**What this file does (one sentence a non-engineer can read)**
It turns an uploaded file into text, now sending PDFs to the bounded worker
instead of reading them inside the API.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | updated |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | `_EXTERNAL_ID_MAX_LENGTH = 255` is pre-existing |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- tests/unit/services/test_evidence_intake.py — ✅ passes

**Open questions before next file**
None — ready to proceed.

**Proceed to File 5?**
Yes — all gates pass, no open questions.

### ✅ File 5 Review — src/exceptions.py

**What this file does (one sentence a non-engineer can read)**
It adds the error that means "the PDF reader could not run", which the upload
path turns into a 503.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | unchanged |
| All functions have docstrings | ✅ | the new class has one |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- covered by the extraction, intake and admission tests — ✅ passes

**Open questions before next file**
None — ready to proceed.

**Proceed to File 6?**
Yes — all gates pass, no open questions.

### ✅ File 6 Review — src/api/evidence_upload_processing.py (new)

**What this file does (one sentence a non-engineer can read)**
It lets at most two uploads per API process in at once, receives and reads
each before any database connection is taken, and frees the place on every
outcome.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | What/Why/How |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | HTTP status codes only, as everywhere in src/api |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | it opens no connection; the endpoint does, under tenant context |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- tests/unit/api/test_evidence_upload_admission.py — ✅ passes (overlap, full house, reuse after every outcome, ordering, cancellation, ceiling and slot reuse)

**Open questions before next file**
None — ready to proceed.

**Proceed to File 7?**
Yes — all gates pass, no open questions.

### ✅ File 7 Review — src/api/evidence_upload_intake.py

**What this file does (one sentence a non-engineer can read)**
It receives the upload body within its size limits and now also within a
fixed time limit.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | updated |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | HTTP status codes only |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- tests/unit/api/test_evidence_upload_bounds.py — ✅ passes (every SEC-REMED-003 case, unchanged)
- tests/unit/api/test_evidence_upload_admission.py — ✅ passes (deadline cases)

**Open questions before next file**
None — ready to proceed.

**Proceed to File 8?**
Yes — all gates pass, no open questions.

### ✅ File 8 Review — src/api/routers/evidence.py

**What this file does (one sentence a non-engineer can read)**
It stores an uploaded document's text, now receiving it already read, so no
database connection is held while a PDF is parsed.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | updated |
| All functions have docstrings | ✅ | |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | the row indexes and status codes are pre-existing |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | `set_tenant_context` before every query, unchanged |
| TenantContextMissingError raised on null/empty context | ✅ | through `set_tenant_context`, unchanged |

**Test coverage summary**
- tests/unit/api/test_evidence.py, test_evidence_upload_bounds.py, test_evidence_upload_admission.py — ✅ passes
- tests/integration/test_pilot_ready_004_upload_limits.py — 🔶 integration (needs live DB; Checkpoint B)

**Open questions before next file**
None — ready to proceed.

**Proceed to File 9?**
Yes — all gates pass, no open questions.

### ✅ File 9 Review — pyproject.toml

**What this file does (one sentence a non-engineer can read)**
It now asks for the pypdf version whose limit hooks the worker was built and
tested against.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | N/A | not Python |
| All functions have docstrings | N/A | |
| No spec notation in variable names | N/A | |
| No magic numbers | N/A | |
| No function longer than 40 lines | N/A | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- the worker verifies the version at run time — ✅ passes

**Open questions before next file**
`requires-python` still says 3.11, although PDF processing needs 3.12 or
later. That is left for G3 and the owner, and recorded in §8.

**Proceed to File 10?**
Yes — the open item is an owner decision, recorded.

### ✅ File 10 Review — frontend/lib/upload-intake.ts, frontend/app/api/evidence/route.ts

**What this file does (one sentence a non-engineer can read)**
The proxy checks who is uploading and their role before reading any of the
upload, lets at most two uploads in at once, gives every stage a time limit,
and passes the upload on unchanged.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | file header comments updated |
| All functions have docstrings | ✅ | JSDoc on every function |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | named constants |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- frontend/__tests__/evidence-upload-route.test.ts, evidence-upload-admission.test.ts, evidence-upload-backend-reset.test.ts, mutation-origins.test.ts — ✅ passes
- frontend/scripts/upload-intake-server.mjs on the real production server — ✅ 13 of 13 under each profile (§7)

**Open questions before next file**
On a serverless host the cap bounds an instance, not the deployment (§6).

**Proceed to File 11?**
Yes — the open item is deployment validation.

### ✅ File 11 Review — frontend/lib/evidence-upload-limits.ts, frontend/lib/roles.ts

**What this file does (one sentence a non-engineer can read)**
They hold the proxy's cap, time limits and the roles that may upload, which
mirror the API's.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | no functions |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | the files that name them |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | N/A | |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- tests/unit/api/test_evidence_upload_policy_consistency.py — ✅ passes

**Open questions before next file**
None — ready to proceed.

**Proceed to File 12?**
Yes — all gates pass, no open questions.

### ✅ Files 12–24 Review — tests and test tools

New: `tests/unit/services/test_pdf_text_worker.py`,
`tests/unit/services/test_pdf_text_extraction.py`,
`tests/unit/services/_pdf_documents.py`,
`tests/unit/services/pdf_worker_probes/` (`probe_worker.py`,
`probe_real_worker.py`, `probe_uncapped_worker.py`, `swallow_sites.py`,
`outside_view.py`), `tests/unit/api/test_evidence_upload_admission.py`,
`tests/unit/api/test_evidence_upload_policy_consistency.py`,
`tests/integration/test_pilot_ready_004_upload_limits.py`,
`frontend/__tests__/evidence-upload-admission.test.ts` and
`frontend/__tests__/evidence-upload-backend-reset.test.ts`.

Changed: `tests/unit/services/test_evidence_intake.py`,
`frontend/__tests__/evidence-upload-route.test.ts` and
`frontend/scripts/upload-intake-server.mjs`.

**What these files do (one sentence a non-engineer can read)**
They prove the limits hold by really overlapping uploads, really trickling
bodies, really killing workers and reading them from outside.

| Check | Result | Notes |
|---|---|---|
| Module docstring present | ✅ | |
| All functions have docstrings | ✅ | helpers do; test functions have none |
| No spec notation in variable names | ✅ | |
| No magic numbers | ✅ | named constants |
| No function longer than 40 lines | ✅ | |
| Tenant isolation rule followed (if DB file) | ✅ | the live file uses the runtime pool and the existing fixtures |
| TenantContextMissingError raised on null/empty context | N/A | |

**Test coverage summary**
- every offline test listed — ✅ passes; no test skips on Windows outside the live-database tests
- tests/integration/test_pilot_ready_004_upload_limits.py — 🔶 integration (needs live DB; Checkpoint B)

**Open questions before next file**
The Windows-only outside-view and ceiling tests have no POSIX equivalent
defined, and POSIX-only tests are not collected on Windows.

**Proceed?**
Yes — Checkpoint A is complete pending independent review; Checkpoint B
needs separate approval.
