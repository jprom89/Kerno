/**
 * What: check the evidence upload proxy's intake bounds on the real production Next.js server
 *       (SEC-REMED-003). It confirms four things over raw loopback sockets:
 *       - Anonymous and forged-session uploads are answered while their body is still incomplete.
 *       - Over-limit bodies, declared or of unknown length, get a 413 and a closed connection.
 *       - The stub backend never receives a refused upload.
 *       - A small upload and a maximum-size one arrive at the backend byte-identical.
 * Why: the route tests drive the handler with in-memory requests. Only the real server shows that
 *      Next.js hands the route an unbuffered stream and that Node honours Connection: close.
 * How: build first, then run node scripts/upload-intake-server.mjs from a checkout with no frontend
 *      .env files. Loopback only: a stub backend with synthetic tokens, and no database, browser or
 *      credentials. See docs/sec_remed_003_evidence_intake.md.
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
const NOT_FOUND = 404;
const CONTENT_TOO_LARGE = 413;
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
const limits = readLimits();
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
  const file = product("MAX_EVIDENCE_FILE_BYTES");
  return { file, body: file + product("EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES") };
}

function respond(response, status, data) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(data));
}

/** Stub FastAPI: verifies only the synthetic token, and records what each forwarded upload contained. */
async function backendRequest(request, response) {
  const authorization = request.headers.authorization ?? "";
  backendCalls.push(`${request.method} ${request.url}`);
  if (request.method === "GET" && request.url === "/api/v1/auth/me") {
    if (authorization === `Bearer ${VALID_TOKEN}`) return respond(response, HTTP_OK, { email: "u@example.test", role: "compliance_lead" });
    return respond(response, UNAUTHORIZED, { detail: "invalid token" });
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
  Object.assign(env, { NODE_ENV: "production", NEXT_TELEMETRY_DISABLED: "1",
    KERNO_API_URL: `http://127.0.0.1:${backendPort}`, KERNO_TRUSTED_ORIGINS: `http://127.0.0.1:${port}`,
    TEMP: tempRoot, TMP: tempRoot });
  return spawnOwned(resources, "frontendProcess", process.execPath, [join(frontend, "node_modules/next/dist/bin/next"),
    "start", "--hostname", "127.0.0.1", "--port", String(port)],
  { cwd: frontend, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
}

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

function multipart(content, filename = "policy.txt") {
  return Buffer.concat([
    Buffer.from(`--${BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\n`
      + "Content-Type: text/plain\r\n\r\n"),
    content,
    Buffer.from(`\r\n--${BOUNDARY}\r\nContent-Disposition: form-data; name="record_type"\r\n\r\npolicy\r\n--${BOUNDARY}--\r\n`),
  ]);
}

function requestHead(port, { token, length }) {
  const lines = [`POST /api/evidence HTTP/1.1`, `Host: 127.0.0.1:${port}`, `Origin: http://127.0.0.1:${port}`,
    `Content-Type: multipart/form-data; boundary=${BOUNDARY}`];
  if (token) lines.push(`Cookie: kerno_session=${token}`);
  lines.push(length === undefined ? "Transfer-Encoding: chunked" : `Content-Length: ${length}`);
  return lines.join("\r\n") + HEADER_END;
}

function chunkFrame(data) {
  return Buffer.concat([Buffer.from(`${data.length.toString(16)}\r\n`), data, Buffer.from("\r\n")]);
}

/**
 * Send a request over a raw socket, writing body frames only while the server has neither answered nor
 * closed, and report what came back. `finish` sends the terminating frames; leaving it false keeps the
 * body incomplete, which is how a check proves the server answered before reading it.
 */
function exchange(port, head, frames, finish) {
  return new Promise((done) => {
    const socket = connect(port, "127.0.0.1");
    const outcome = { status: null, headers: {}, written: 0, serverClosed: false, answeredAt: null };
    let received = Buffer.alloc(0);
    const started = Date.now();
    const timer = setTimeout(() => socket.destroy(), ANSWER_TIMEOUT_MS);
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

function drained(socket) {
  return new Promise((done) => {
    const resume = () => { socket.off("drain", resume); socket.off("close", resume); done(); };
    socket.on("drain", resume);
    socket.on("close", resume);
  });
}

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

async function main() {
  const tempRoot = mkdtempSync(join(tmpdir(), "kerno-upload-intake-"));
  const resources = { backend: createServer((request, response) => {
    backendRequest(request, response).catch(() => respond(response, UNAUTHORIZED, { detail: "stub failure" }));
  }) };
  const evidence = { evidence: "real production Next.js server with a stubbed backend over loopback; not a browser or deployment test",
    sourceSha: process.env.KERNO_VALIDATION_SOURCE_SHA ?? "unrecorded", limits, results };
  await runWithCleanup(async () => {
    const backendPort = await listen(resources.backend);
    const port = await unusedPort();
    resources.frontendProcess = startFrontend(port, backendPort, tempRoot, resources);
    assertRunning(resources.frontendProcess);
    resources.frontendProcess.child.stdout.on("data", () => {});
    resources.frontendProcess.child.stderr.on("data", (data) => process.stderr.write(data));
    await waitReady(port, resources.frontendProcess);
    await unverifiedCallers(port);
    await overLimitBodies(port);
    await validUploads(port);
  }, () => cleanup(resources, tempRoot), (outcome) => {
    console.log(JSON.stringify({ ...evidence, ...outcome,
      exitCode: outcome.originalFailure || outcome.cleanup.errors.length ? 1 : 0 }, null, 2));
  });
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
