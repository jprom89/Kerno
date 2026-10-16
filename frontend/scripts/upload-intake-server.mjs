/**
 * What: check the evidence upload proxy's intake bounds on the real production Next.js server
 *       (SEC-REMED-003, PILOT-READY-004). It confirms over raw loopback sockets:
 *       - Anonymous, forged-session and read-only-role uploads are answered while their body is still incomplete.
 *       - Over-limit bodies, declared or of unknown length, get a 413 and a closed connection.
 *       - The stub backend never receives a refused upload.
 *       - A small upload and a maximum-size one arrive at the backend byte-identical.
 *       - A body trickled in small, well-spaced pieces is refused at the absolute reception deadline (408).
 *       - With every upload slot held mid-body, one more upload is refused at once (503, Retry-After), and a slot
 *         freed by a client that leaves is used again.
 *       - The backend's own busy 503, sent with Connection: close before it reads the body, reaches the client as
 *         a controlled refusal, never a 500.
 *       - A backend that never answers is cut off at the forward deadline (504).
 * Why: the route tests drive the handler with in-memory requests. Only the real server shows that
 *      Next.js hands the route an unbuffered stream, that Node honours Connection: close, and that the
 *      deadlines and the upload cap hold on real sockets.
 * How: build first, then run node scripts/upload-intake-server.mjs from a checkout with no frontend
 *      .env files. Loopback only: a stub backend with synthetic tokens, and no database, browser or
 *      credentials. The deadline checks take as long as the real deadlines (about three minutes in all).
 *      The server runs with KERNO_SERVING_PROFILE=full unless KERNO_VALIDATION_SERVING_PROFILE says otherwise.
 *      See docs/sec_remed_003_evidence_intake.md and docs/evidence_upload_limits.md.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { assertRunning, bounded, closeServer, runWithCleanup, spawnOwned, stopOwned } from "./csrf-browser-resources.mjs";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const HTTP_OK = 200;
const CREATED = 201;
const UNAUTHORIZED = 401;
const FORBIDDEN = 403;
const NOT_FOUND = 404;
const REQUEST_TIMEOUT = 408;
const CONTENT_TOO_LARGE = 413;
const BAD_GATEWAY = 502;
const SERVICE_UNAVAILABLE = 503;
const GATEWAY_TIMEOUT = 504;
const READY_TIMEOUT_MS = 30000;
const POLL_INTERVAL_MS = 200;
const READINESS_REQUEST_TIMEOUT_MS = 1000;
const ANSWER_TIMEOUT_MS = 10000;
const CLEANUP_TIMEOUT_MS = 5000;
const WRITE_CHUNK_BYTES = 64 * 1024;
const OVERSHOOT_BYTES = 4 * 1024 * 1024;
const PARTIAL_BODY_BYTES = 64;
const HEADER_END = "\r\n\r\n";
const BOUNDARY = "KernoSecRemed003Harness";
const VALID_TOKEN = "synthetic-valid-session";
const FORGED_TOKEN = "synthetic-forged-session";
const AUDITOR_TOKEN = "synthetic-auditor-session";
// The proxy forwards the caller's Content-Type unchanged, so the boundary tells the stub how to behave
// before it reads any of the body.
const BUSY_BOUNDARY = "KernoHarnessBackendBusy";
const STALL_BOUNDARY = "KernoHarnessBackendStalls";
const BUSY_DETAIL = "evidence uploads are busy; try again shortly";
// The trickle sends one small frame at this interval: far apart, so only an absolute deadline can stop it.
const TRICKLE_INTERVAL_MS = 2000;
// Room above a deadline for the server to answer once it passes.
const DEADLINE_SLACK_MS = 5000;
// A refusal at a full house must come at once, far inside any deadline.
const PROMPT_ANSWER_MS = 2000;
// How long the stub backend stays silent on a stalled upload: past the forward deadline.
const STALL_BEYOND_DEADLINE_MS = 15000;
// How long to let a released slot settle before it is used again.
const SETTLE_MS = 500;
const limits = readLimits();
const servingProfile = process.env.KERNO_VALIDATION_SERVING_PROFILE ?? "full";
const backendCalls = [];
const results = [];

/** Read the deployed limits from lib/evidence-upload-limits.ts, so the harness tests what was built. */
function readLimits() {
  const source = readFileSync(join(frontend, "lib/evidence-upload-limits.ts"), "utf8");
  const product = (name) => {
    const match = source.match(new RegExp(`export const ${name} = ([0-9 *]+);`));
    assert(match, `${name} not found as a plain product`);
    return match[1].split("*").reduce((total, factor) => total * Number(factor), 1);
  };
  const integer = (name) => {
    const match = source.match(new RegExp(`export const ${name} = ([0-9_]+);`));
    assert(match, `${name} not found as a plain integer`);
    return Number(match[1].replaceAll("_", ""));
  };
  const file = product("MAX_EVIDENCE_FILE_BYTES");
  return { file, body: file + product("EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES"),
    maxActive: integer("EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS"), retryAfter: integer("EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS"),
    receptionMs: integer("EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS"), forwardMs: integer("EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS") };
}

/** Send a JSON response from the stub backend. */
function respond(response, status, data) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(data));
}

/** The multipart boundary of a forwarded request, from its Content-Type alone. */
function boundaryOf(request) {
  return (request.headers["content-type"] ?? "").split("boundary=")[1] ?? "";
}

/**
 * Stub FastAPI: verifies only the synthetic tokens, records what each forwarded upload contained, and for the
 * two special boundaries answers busy before reading the body, or never answers.
 */
async function backendRequest(request, response) {
  const authorization = request.headers.authorization ?? "";
  backendCalls.push(`${request.method} ${request.url}`);
  if (request.method === "GET" && request.url === "/api/v1/auth/me") {
    if (authorization === `Bearer ${VALID_TOKEN}`) return respond(response, HTTP_OK, { email: "u@example.test", role: "compliance_lead" });
    if (authorization === `Bearer ${AUDITOR_TOKEN}`) return respond(response, HTTP_OK, { email: "a@example.test", role: "auditor" });
    return respond(response, UNAUTHORIZED, { detail: "invalid token" });
  }
  if (request.method === "POST" && request.url === "/api/v1/evidence" && boundaryOf(request) === BUSY_BOUNDARY) {
    // The API's own busy refusal: answered before any body byte is read, then the connection is closed.
    backendCalls.push("busy 503 before reading the body");
    response.writeHead(SERVICE_UNAVAILABLE, { "content-type": "application/json", "retry-after": String(limits.retryAfter),
      connection: "close" });
    return response.end(JSON.stringify({ detail: BUSY_DETAIL }));
  }
  if (request.method === "POST" && request.url === "/api/v1/evidence" && boundaryOf(request) === STALL_BOUNDARY) {
    backendCalls.push("stalled upload");
    request.resume();
    await new Promise((done) => {
      const timer = setTimeout(done, limits.forwardMs + STALL_BEYOND_DEADLINE_MS);
      request.socket.once("close", () => { backendCalls.push("stalled upload abandoned by the proxy"); clearTimeout(timer); done(); });
    });
    return undefined;
  }
  if (request.method === "POST" && request.url === "/api/v1/evidence") {
    const hash = createHash("sha256");
    let length = 0;
    for await (const chunk of request) {
      length += chunk.length;
      hash.update(chunk);
    }
    const upload = { sha256: hash.digest("hex"), length, contentType: request.headers["content-type"] };
    backendCalls.push(upload);
    return respond(response, CREATED, { record_id: "e0000000-0000-4000-e000-000000000031", deduplicated: false });
  }
  return respond(response, NOT_FOUND, { detail: "stub route not implemented" });
}

/** Bind an HTTP server to loopback only, returning the assigned ephemeral port. */
async function listen(server) {
  await new Promise((done, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", done); });
  return server.address().port;
}

/** Reserve an available loopback port long enough to select the Next.js listener. */
async function unusedPort() {
  const server = createServer();
  const port = await listen(server);
  await closeServer(server);
  return port;
}

/** Start the production frontend with an allowlist of OS variables and synthetic config. */
function startFrontend(port, backendPort, tempRoot, resources) {
  for (const name of [".env", ".env.local", ".env.production", ".env.production.local"]) {
    assert(!existsSync(join(frontend, name)), `Refusing a frontend containing ${name}`);
  }
  const env = {};
  for (const key of ["PATH", "Path", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"]) {
    if (process.env[key]) env[key] = process.env[key];
  }
  // next start refuses to run without a serving profile (PILOT-READY-003). The upload proxy is served in both;
  // KERNO_VALIDATION_SERVING_PROFILE picks which one the harness runs.
  Object.assign(env, { NODE_ENV: "production", NEXT_TELEMETRY_DISABLED: "1",
    KERNO_API_URL: `http://127.0.0.1:${backendPort}`, KERNO_TRUSTED_ORIGINS: `http://127.0.0.1:${port}`,
    KERNO_SERVING_PROFILE: servingProfile, TEMP: tempRoot, TMP: tempRoot });
  return spawnOwned(resources, "frontendProcess", process.execPath, [join(frontend, "node_modules/next/dist/bin/next"),
    "start", "--hostname", "127.0.0.1", "--port", String(port)],
  { cwd: frontend, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
}

/** Wait for the production server's login page, failing if the process exits first. */
async function waitReady(port, record) {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    assertRunning(record);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/login`, { signal: AbortSignal.timeout(READINESS_REQUEST_TIMEOUT_MS) });
      if (response.ok) return;
    } catch { /* Startup may not have bound the listener yet. */ }
    await new Promise((done) => setTimeout(done, POLL_INTERVAL_MS));
  }
  throw new Error("Isolated Next.js startup timed out");
}

/** A multipart body: one text file and a record_type field, with the given boundary. */
function multipart(content, filename = "policy.txt", boundary = BOUNDARY) {
  return Buffer.concat([
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\n`
      + "Content-Type: text/plain\r\n\r\n"),
    content,
    Buffer.from(`\r\n--${boundary}\r\nContent-Disposition: form-data; name="record_type"\r\n\r\npolicy\r\n--${boundary}--\r\n`),
  ]);
}

/** The request line and headers of an upload: chunked unless a length is given, with the session cookie if any. */
function requestHead(port, { token, length, boundary = BOUNDARY }) {
  const lines = [`POST /api/evidence HTTP/1.1`, `Host: 127.0.0.1:${port}`, `Origin: http://127.0.0.1:${port}`,
    `Content-Type: multipart/form-data; boundary=${boundary}`];
  if (token) lines.push(`Cookie: kerno_session=${token}`);
  lines.push(length === undefined ? "Transfer-Encoding: chunked" : `Content-Length: ${length}`);
  return lines.join("\r\n") + HEADER_END;
}

/** Frame one piece of body for Transfer-Encoding: chunked. */
function chunkFrame(data) {
  return Buffer.concat([Buffer.from(`${data.length.toString(16)}\r\n`), data, Buffer.from("\r\n")]);
}

/**
 * Send a request over a raw socket, writing body frames only while the server has neither answered nor
 * closed, and report what came back. `finish` sends the terminating frames; leaving it false keeps the
 * body incomplete, which is how a check proves the server answered before reading it.
 */
function exchange(port, head, frames, finish, timeoutMs = ANSWER_TIMEOUT_MS) {
  return new Promise((done) => {
    const socket = connect(port, "127.0.0.1");
    const outcome = { status: null, headers: {}, written: 0, serverClosed: false, answeredAt: null };
    let received = Buffer.alloc(0);
    const started = Date.now();
    const timer = setTimeout(() => socket.destroy(), timeoutMs);
    const answered = () => outcome.status !== null || outcome.serverClosed;
    socket.on("data", (data) => {
      received = Buffer.concat([received, data]);
      const end = received.indexOf(HEADER_END);
      if (outcome.status === null && end >= 0) {
        const [statusLine, ...lines] = received.subarray(0, end).toString("latin1").split("\r\n");
        outcome.status = Number(statusLine.split(" ")[1]);
        outcome.answeredAt = Date.now() - started;
        for (const line of lines) outcome.headers[line.slice(0, line.indexOf(":")).toLowerCase()] = line.slice(line.indexOf(":") + 1).trim();
      }
    });
    socket.on("end", () => { outcome.serverClosed = true; });
    socket.on("error", () => { outcome.serverClosed = true; });
    socket.on("close", () => { clearTimeout(timer); done({ ...outcome, elapsedMs: Date.now() - started }); });
    socket.on("connect", async () => {
      socket.write(head);
      for (const frame of frames) {
        if (answered() || socket.destroyed) break;
        outcome.written += frame.length;
        if (!socket.write(frame)) await drained(socket);
      }
      if (finish && !answered() && !socket.destroyed) socket.write(finish);
    });
  });
}

/** Resolve when the socket can take more data, or has closed. */
function drained(socket) {
  return new Promise((done) => {
    const resume = () => { socket.off("drain", resume); socket.off("close", resume); done(); };
    socket.on("drain", resume);
    socket.on("close", resume);
  });
}

/** Split a buffer into write-sized pieces without copying it. */
function slices(buffer) {
  const list = [];
  for (let start = 0; start < buffer.length; start += WRITE_CHUNK_BYTES) list.push(buffer.subarray(start, start + WRITE_CHUNK_BYTES));
  return list;
}

/** Record a check, failing on the first unmet expectation, with the evidence that decided it. */
async function check(name, run) {
  const callsBefore = backendCalls.length;
  const outcome = await run();
  const calls = backendCalls.slice(callsBefore);
  results.push({ name, status: outcome.status, connection: outcome.headers?.connection ?? null,
    serverClosed: outcome.serverClosed, answeredAtMs: outcome.answeredAt, bodyBytesWritten: outcome.written,
    backendCalls: calls.map((call) => (typeof call === "string" ? call : `upload ${call.length} bytes`)) });
  return { outcome, calls };
}

/** Anonymous and forged-session uploads must be answered with 401 while their body is still incomplete. */
async function unverifiedCallers(port) {
  for (const [name, token] of [["anonymous upload answered before its body completes", null],
    ["forged-session upload answered before its body completes", FORGED_TOKEN]]) {
    const { outcome, calls } = await check(name, () => exchange(port, requestHead(port, { token }),
      [chunkFrame(multipart(Buffer.from("partial evidence")).subarray(0, PARTIAL_BODY_BYTES))], null));
    assert.equal(outcome.status, UNAUTHORIZED, `${name}: expected 401 while the body was incomplete`);
    assert.equal(outcome.headers.connection, "close");
    assert.ok(outcome.serverClosed, `${name}: the server must close rather than drain the unread body`);
    assert.deepEqual(calls, token ? ["GET /api/v1/auth/me"] : [], `${name}: unexpected backend calls`);
  }
}

/** Declared and unknown-length over-limit bodies must be refused and the connection closed, with nothing forwarded. */
async function overLimitBodies(port) {
  const declared = await check("declared over-limit body refused before reading it", () => exchange(port,
    requestHead(port, { token: VALID_TOKEN, length: limits.body + 1 }), slices(Buffer.alloc(WRITE_CHUNK_BYTES, "x")), null));
  assert.equal(declared.outcome.status, CONTENT_TOO_LARGE);
  assert.equal(declared.outcome.headers.connection, "close");
  assert.ok(declared.outcome.serverClosed, "the server must close the connection after the 413");
  assert.deepEqual(declared.calls, ["GET /api/v1/auth/me"]);
  const sent = slices(Buffer.alloc(limits.body + OVERSHOOT_BYTES, "x")).map(chunkFrame);
  const streamed = await check("unknown-length over-limit body cut off and the connection closed", () => exchange(port,
    requestHead(port, { token: VALID_TOKEN }), sent, Buffer.from("0\r\n\r\n")));
  // A client still sending when the server closes may get a reset instead of the 413 (see receiveBoundedBody),
  // so what is asserted is the server's side: it closed early, and nothing was forwarded.
  assert.ok(streamed.outcome.status === CONTENT_TOO_LARGE || streamed.outcome.status === null,
    `unexpected status ${streamed.outcome.status}`);
  assert.ok(streamed.outcome.serverClosed, "the server must close the connection");
  assert.ok(streamed.outcome.written < sent.reduce((total, frame) => total + frame.length, 0),
    "the server must close before the client could send the whole over-limit body");
  assert.deepEqual(streamed.calls, ["GET /api/v1/auth/me"]);
}

/** A small upload and a maximum-size one must reach the backend byte-identical, with the stub's 201 relayed. */
async function validUploads(port) {
  for (const [name, content] of [["small text upload forwarded byte-identical", Buffer.from("Access review policy v2.")],
    ["maximum-size file forwarded byte-identical", Buffer.alloc(limits.file, "m")]]) {
    const body = multipart(content);
    const { outcome, calls } = await check(name, () => exchange(port,
      requestHead(port, { token: VALID_TOKEN, length: body.length }), slices(body), null));
    assert.equal(outcome.status, CREATED, `${name}: expected the stub's 201 to be relayed`);
    const forwarded = calls.find((call) => typeof call === "object");
    assert.ok(forwarded, `${name}: the upload never reached the backend`);
    assert.equal(forwarded.sha256, createHash("sha256").update(body).digest("hex"), `${name}: bytes changed in transit`);
    assert.equal(forwarded.contentType, `multipart/form-data; boundary=${BOUNDARY}`);
  }
}

/** A verified session whose role may not upload must be answered with 403 while its body is still incomplete. */
async function readOnlyRole(port) {
  const name = "read-only role answered before its body completes";
  const { outcome, calls } = await check(name, () => exchange(port, requestHead(port, { token: AUDITOR_TOKEN }),
    [chunkFrame(multipart(Buffer.from("partial evidence")).subarray(0, PARTIAL_BODY_BYTES))], null));
  assert.equal(outcome.status, FORBIDDEN, `${name}: expected 403 while the body was incomplete`);
  assert.equal(outcome.headers.connection, "close");
  assert.ok(outcome.serverClosed, `${name}: the server must close rather than drain the unread body`);
  assert.deepEqual(calls, ["GET /api/v1/auth/me"], `${name}: unexpected backend calls`);
}

/**
 * Open an upload that sends its first few bytes and then holds, so it keeps its upload slot until the
 * socket is destroyed. Resolves once the server has had time to admit it.
 */
async function heldUpload(port) {
  const socket = connect(port, "127.0.0.1");
  await new Promise((done) => socket.once("connect", done));
  socket.on("error", () => {});
  socket.write(requestHead(port, { token: VALID_TOKEN }));
  socket.write(chunkFrame(multipart(Buffer.from("held evidence")).subarray(0, PARTIAL_BODY_BYTES)));
  await new Promise((done) => setTimeout(done, SETTLE_MS));
  return socket;
}

/**
 * With every upload slot held mid-body, one more upload must be refused at once (503 with Retry-After and
 * Connection: close, nothing forwarded); once the holding clients leave, a slot must be usable again.
 */
async function fullHouse(port) {
  const held = [];
  for (let index = 0; index < limits.maxActive; index += 1) held.push(await heldUpload(port));
  const name = "one upload more than the cap refused at once while the others are held mid-body";
  const { outcome, calls } = await check(name, () => exchange(port, requestHead(port, { token: VALID_TOKEN }),
    [chunkFrame(multipart(Buffer.from("one too many")).subarray(0, PARTIAL_BODY_BYTES))], null));
  for (const socket of held) socket.destroy();
  assert.equal(outcome.status, SERVICE_UNAVAILABLE, `${name}: expected 503`);
  assert.equal(outcome.headers["retry-after"], String(limits.retryAfter));
  assert.equal(outcome.headers.connection, "close");
  assert.ok(outcome.answeredAt < PROMPT_ANSWER_MS, `${name}: answered after ${outcome.answeredAt} ms, not at once`);
  assert.deepEqual(calls, ["GET /api/v1/auth/me"], `${name}: a refused upload must not be forwarded`);
  await new Promise((done) => setTimeout(done, SETTLE_MS));
  const body = multipart(Buffer.from("after the holders left"));
  const reuse = await check("a slot freed by a client that left is used again", () => exchange(port,
    requestHead(port, { token: VALID_TOKEN, length: body.length }), slices(body), null));
  assert.equal(reuse.outcome.status, CREATED, "the freed slot was not usable again");
}

/**
 * The backend's busy 503 is sent with Connection: close before it reads the body. The client must get a
 * controlled refusal with a string detail (the 503 with its Retry-After, or a 502), never a 500.
 */
async function backendBusy(port) {
  for (const size of [PARTIAL_BODY_BYTES, WRITE_CHUNK_BYTES * PARTIAL_BODY_BYTES]) {
    const name = `backend's early busy 503 reaches the client as a controlled refusal (${size}-byte file)`;
    const body = multipart(Buffer.alloc(size, "b"), "policy.txt", BUSY_BOUNDARY);
    const { outcome, calls } = await check(name, () => exchange(port,
      requestHead(port, { token: VALID_TOKEN, length: body.length, boundary: BUSY_BOUNDARY }), slices(body), null));
    assert.ok([SERVICE_UNAVAILABLE, BAD_GATEWAY].includes(outcome.status), `${name}: got ${outcome.status}`);
    if (outcome.status === SERVICE_UNAVAILABLE) assert.equal(outcome.headers["retry-after"], String(limits.retryAfter));
    assert.ok(calls.includes("busy 503 before reading the body"), `${name}: the backend was not reached`);
  }
}

/**
 * Send a verified upload's body as one tiny chunked frame per interval, each far inside any idle timeout,
 * until the server answers or closes (or timeoutMs passes), and report what came back.
 */
function trickle(port, frame, intervalMs, timeoutMs) {
  return new Promise((done) => {
    const socket = connect(port, "127.0.0.1");
    const outcome = { status: null, headers: {}, written: 0, serverClosed: false, answeredAt: null };
    const started = Date.now();
    let received = Buffer.alloc(0);
    let ticker = null;
    const timer = setTimeout(() => socket.destroy(), timeoutMs);
    socket.on("data", (data) => {
      received = Buffer.concat([received, data]);
      const end = received.indexOf(HEADER_END);
      if (outcome.status === null && end >= 0) {
        const [statusLine, ...lines] = received.subarray(0, end).toString("latin1").split("\r\n");
        outcome.status = Number(statusLine.split(" ")[1]);
        outcome.answeredAt = Date.now() - started;
        for (const line of lines) outcome.headers[line.slice(0, line.indexOf(":")).toLowerCase()] = line.slice(line.indexOf(":") + 1).trim();
      }
    });
    const stop = () => { outcome.serverClosed = true; clearInterval(ticker); };
    socket.on("end", stop);
    socket.on("error", stop);
    socket.on("close", () => { clearTimeout(timer); clearInterval(ticker); done({ ...outcome, elapsedMs: Date.now() - started }); });
    socket.on("connect", () => {
      socket.write(requestHead(port, { token: VALID_TOKEN }));
      ticker = setInterval(() => {
        if (outcome.status !== null || socket.destroyed) return;
        socket.write(chunkFrame(frame));
        outcome.written += frame.length;
      }, intervalMs);
    });
  });
}

/** A trickled body is refused at the reception deadline, not kept alive by its steady little frames. */
async function trickledBody(port) {
  const name = "trickled body refused at the absolute reception deadline";
  const { outcome, calls } = await check(name, () => trickle(port, Buffer.from("t"), TRICKLE_INTERVAL_MS,
    limits.receptionMs + DEADLINE_SLACK_MS * 2));
  assert.equal(outcome.status, REQUEST_TIMEOUT, `${name}: expected 408`);
  assert.equal(outcome.headers.connection, "close");
  assert.ok(outcome.answeredAt >= limits.receptionMs, `${name}: answered after ${outcome.answeredAt} ms, before the deadline`);
  assert.ok(outcome.answeredAt <= limits.receptionMs + DEADLINE_SLACK_MS, `${name}: answered after ${outcome.answeredAt} ms`);
  assert.deepEqual(calls, ["GET /api/v1/auth/me"], `${name}: a refused upload must not be forwarded`);
}

/** A backend that never answers is cut off at the forward deadline (504), and the proxy abandons its request. */
async function backendStalls(port) {
  const name = "silent backend cut off at the forward deadline";
  const body = multipart(Buffer.from("stalled evidence"), "policy.txt", STALL_BOUNDARY);
  const { outcome } = await check(name, () => exchange(port,
    requestHead(port, { token: VALID_TOKEN, length: body.length, boundary: STALL_BOUNDARY }), slices(body), null,
    limits.forwardMs + DEADLINE_SLACK_MS * 2));
  assert.equal(outcome.status, GATEWAY_TIMEOUT, `${name}: expected 504`);
  assert.ok(outcome.answeredAt >= limits.forwardMs, `${name}: answered after ${outcome.answeredAt} ms, before the deadline`);
  assert.ok(outcome.answeredAt <= limits.forwardMs + DEADLINE_SLACK_MS, `${name}: answered after ${outcome.answeredAt} ms`);
  await new Promise((done) => setTimeout(done, SETTLE_MS));
  assert.ok(backendCalls.includes("stalled upload abandoned by the proxy"), `${name}: the backend request was not abandoned`);
}

/** Stop the owned server process and listener and remove the owned temporary directory, each independently. */
async function cleanup(resources, tempRoot) {
  const errors = [];
  for (const [label, action] of [["frontend process", () => stopOwned(resources.frontendProcess, CLEANUP_TIMEOUT_MS)],
    ["stub backend listener", () => closeServer(resources.backend, CLEANUP_TIMEOUT_MS)],
    ["temporary directory", () => rmSync(tempRoot, { recursive: true, force: true })]]) {
    try { await bounded(Promise.resolve().then(action), CLEANUP_TIMEOUT_MS, label); }
    catch (error) { errors.push({ resource: label, message: error.message }); }
  }
  return { errors };
}

/** Run every check against a fresh production server and always clean up, reporting evidence as JSON. */
async function main() {
  const tempRoot = mkdtempSync(join(tmpdir(), "kerno-upload-intake-"));
  const resources = { backend: createServer((request, response) => {
    backendRequest(request, response).catch(() => respond(response, UNAUTHORIZED, { detail: "stub failure" }));
  }) };
  const evidence = { evidence: "real production Next.js server with a stubbed backend over loopback; not a browser or deployment test",
    sourceSha: process.env.KERNO_VALIDATION_SOURCE_SHA ?? "unrecorded", servingProfile, limits, results };
  await runWithCleanup(async () => {
    const backendPort = await listen(resources.backend);
    const port = await unusedPort();
    resources.frontendProcess = startFrontend(port, backendPort, tempRoot, resources);
    assertRunning(resources.frontendProcess);
    resources.frontendProcess.child.stdout.on("data", () => {});
    resources.frontendProcess.child.stderr.on("data", (data) => process.stderr.write(data));
    await waitReady(port, resources.frontendProcess);
    await unverifiedCallers(port);
    await readOnlyRole(port);
    await overLimitBodies(port);
    await validUploads(port);
    await fullHouse(port);
    await backendBusy(port);
    await trickledBody(port);
    await backendStalls(port);
  }, () => cleanup(resources, tempRoot), (outcome) => {
    console.log(JSON.stringify({ ...evidence, ...outcome,
      exitCode: outcome.originalFailure || outcome.cleanup.errors.length ? 1 : 0 }, null, 2));
  });
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
