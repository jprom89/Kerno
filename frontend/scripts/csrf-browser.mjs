/**
 * What: validate the real production Next.js/browser cookie boundary for SEC-REMED-002.
 * Why: route mocks cannot prove that a cross-site form leaves a browser session intact.
 * How: build first, then node scripts/csrf-browser.mjs. Requires an existing Playwright
 *      module and Chromium browser; see docs/sec_remed_002_login_csrf.md. No installs,
 *      database, personal profile, real credentials or public listeners are used.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.KERNO_PLAYWRIGHT_MODULE || "playwright");
const HTTP_OK = 200;
const FORBIDDEN = 403;
const UNAUTHORIZED = 401;
const UNSUPPORTED_MEDIA = 415;
const NOT_FOUND = 404;
const READY_TIMEOUT_MS = 30000;
const POLL_INTERVAL_MS = 200;
const CHECK_TIMEOUT_MS = 15000;
const SYNTHETIC_PASSWORD = "only-for-this-disposable-test";
const COOKIE = "kerno_session";
const state = { loginCalls: 0, mutationCalls: 0, lastIdentity: null };
const results = [];

/** Launch the installed browser with its normal security defaults, without Playwright flags. */
async function launchBrowser(profile) {
  assert(process.env.KERNO_BROWSER_EXECUTABLE, "Set KERNO_BROWSER_EXECUTABLE to an installed Chromium browser");
  const child = spawn(process.env.KERNO_BROWSER_EXECUTABLE, ["--headless=new",
    "--no-first-run", "--no-default-browser-check", "--remote-debugging-address=127.0.0.1",
    "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });
  child.stderr.on("data", (data) => process.stderr.write(data));
  const deadline = Date.now() + READY_TIMEOUT_MS;
  try {
    while (Date.now() < deadline) {
      if (child.exitCode !== null) throw new Error(`Browser exited ${child.exitCode}`);
      const activePort = join(profile, "DevToolsActivePort");
      if (existsSync(activePort)) {
        const [port] = readFileSync(activePort, "utf8").split(/\r?\n/);
        const browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: READY_TIMEOUT_MS });
        return { browser, child, context: browser.contexts()[0] };
      }
      await new Promise((done) => setTimeout(done, POLL_INTERVAL_MS));
    }
    throw new Error("Installed browser did not expose its local test connection");
  } catch (error) {
    await stopFrontend(child);
    throw error;
  }
}

/** Return one of two deliberately fake accounts; neither can authenticate to Kerno. */
function credentials(identity) {
  return { tenant_slug: identity, email: `${identity}@example.test`, password: SYNTHETIC_PASSWORD };
}

/** Write a small JSON response without logging tokens or credentials. */
function respond(response, status, data) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(data));
}

/** Implement only synthetic authentication and minimal dashboard data, never real auth. */
async function backendRequest(request, response) {
  if (request.url === "/api/v1/auth/login" && request.method === "POST") {
    state.loginCalls += 1;
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    const identity = ["victim", "attacker"].find((name) => {
      const expected = credentials(name);
      return Object.keys(expected).every((key) => body[key] === expected[key]);
    });
    return respond(response, identity ? HTTP_OK : UNAUTHORIZED,
      identity ? { access_token: `synthetic-${identity}-session`, token_type: "bearer" }
        : { detail: "invalid credentials" });
  }
  const token = request.headers.authorization;
  const identity = ["victim", "attacker"].find((name) => token === `Bearer synthetic-${name}-session`);
  if (!identity) return respond(response, UNAUTHORIZED, { detail: "unauthenticated" });
  if (request.url === "/api/v1/auth/me") {
    state.lastIdentity = identity;
    return respond(response, HTTP_OK, { email: `${identity}@example.test`, role: "compliance_lead" });
  }
  if (request.method === "POST") state.mutationCalls += 1;
  if (request.url === "/api/v1/coverage/summary") {
    return respond(response, HTTP_OK, { total_controls: 0, met: 0, partial: 0, gap: 0,
      categories: [], last_recalculated_at: null });
  }
  if (request.url === "/api/v1/coverage/controls") return respond(response, HTTP_OK, []);
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
  await new Promise((done) => server.close(done));
  return port;
}

/** Build a valid JSON text/plain form with its required equals sign inside padding. */
function attackForm(target) {
  const name = JSON.stringify(credentials("attacker")).slice(0, -1) + ',"pad":"';
  return `<form method="POST" enctype="text/plain" action="${target}">
    <input id="payload"><button>Submit attack</button></form><script>
    const field = document.getElementById('payload');
    field.name = ${JSON.stringify(name)}; field.value = '\"}';</script>`;
}

/** Serve only local synthetic attack pages, including an opaque-origin sandbox form. */
function attackPage(request, response, target) {
  const form = attackForm(target);
  const escaped = form.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");
  const html = request.url === "/opaque"
    ? `<iframe sandbox="allow-forms allow-scripts" srcdoc="${escaped}"></iframe>` : form;
  response.writeHead(HTTP_OK, { "content-type": "text/html; charset=utf-8" });
  response.end(html);
}

/** Start the production frontend with an allowlist of OS variables and synthetic config. */
function startFrontend(port, backendPort, tempRoot) {
  for (const name of [".env", ".env.local", ".env.production", ".env.production.local"]) {
    assert(!existsSync(join(frontend, name)), `Refusing a frontend containing ${name}`);
  }
  const env = {};
  for (const key of ["PATH", "Path", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"]) {
    if (process.env[key]) env[key] = process.env[key];
  }
  Object.assign(env, { NODE_ENV: "production", NEXT_TELEMETRY_DISABLED: "1",
    KERNO_API_URL: `http://127.0.0.1:${backendPort}`, KERNO_TRUSTED_ORIGINS: `http://localhost:${port}`,
    TEMP: tempRoot, TMP: tempRoot });
  return spawn(process.execPath, [join(frontend, "node_modules/next/dist/bin/next"),
    "start", "--hostname", "127.0.0.1", "--port", String(port)],
  { cwd: frontend, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
}

/** Wait for the local production login page without ever contacting a real backend. */
async function waitReady(port, child) {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`Next.js exited ${child.exitCode}`);
    try {
      const response = await fetch(`http://127.0.0.1:${port}/login`);
      if (response.ok) return;
    } catch { /* Startup may not have bound the listener yet. */ }
    await new Promise((done) => setTimeout(done, POLL_INTERVAL_MS));
  }
  throw new Error("Isolated Next.js startup timed out");
}

/** Assert cookie contents only in memory; returned reports never include token values. */
async function victimCookie(context, origin) {
  const cookie = (await context.cookies(origin)).find((entry) => entry.name === COOKIE);
  assert(cookie, "Expected the synthetic victim session cookie");
  assert.equal(cookie.value, "synthetic-victim-session");
  assert.equal(cookie.httpOnly, true);
  assert.equal(cookie.secure, true);
  assert.equal(cookie.sameSite, "Lax");
  assert.equal(cookie.path, "/");
  return cookie;
}

/** Log in through the real UI and check the server-side identity and cookie boundary. */
async function legitimateLogin(page, context, origin) {
  await page.goto(`${origin}/login`);
  await page.getByLabel("Organisation", { exact: true }).fill("victim");
  await page.getByLabel("Email", { exact: true }).fill(credentials("victim").email);
  await page.getByLabel("Password", { exact: true }).fill(SYNTHETIC_PASSWORD);
  await Promise.all([page.waitForURL(`${origin}/dashboard`), page.getByRole("button", { name: "Sign in", exact: true }).click()]);
  await page.getByText("victim@example.test", { exact: true }).waitFor();
  assert.equal(state.lastIdentity, "victim");
  await victimCookie(context, origin);
  assert(!(await page.evaluate(() => document.cookie)).includes(COOKIE));
  results.push({ check: "trusted-origin JSON login and production cookie flags", passed: true });
}

/** Submit a real cross-site form and prove that it cannot authenticate or replace a cookie. */
async function rejectedForm(page, context, origin, attacker, opaque = false, signedIn = true) {
  const before = state.loginCalls;
  await page.goto(attacker + (opaque ? "/opaque" : "/"));
  const pending = page.waitForResponse((response) => response.url() === `${origin}/api/auth/login`);
  const button = opaque ? page.frameLocator("iframe").getByRole("button") : page.getByRole("button");
  await button.click();
  const response = await pending;
  assert.equal(response.status(), FORBIDDEN);
  const headers = await response.request().allHeaders();
  assert.equal(headers.origin, opaque ? "null" : attacker);
  assert.equal(headers["sec-fetch-site"], "cross-site");
  assert.equal(JSON.parse(response.request().postData()).tenant_slug, "attacker");
  assert.equal((await response.allHeaders())["set-cookie"], undefined);
  assert.equal(state.loginCalls, before);
  if (signedIn) await victimCookie(context, origin);
  else assert(!(await context.cookies(origin)).some((entry) => entry.name === COOKIE));
  results.push({ check: opaque ? "opaque null-origin form" : "cross-site text/plain login form",
    signedIn, origin: headers.origin, fetchSite: headers["sec-fetch-site"], passed: true });
}

/** Check login's independent JSON gate and both rejection and success of logout. */
async function otherBoundaries(page, context, origin, attacker) {
  await page.goto(`${origin}/login`);
  const before = state.loginCalls;
  const status = await page.evaluate(async (body) => {
    const response = await fetch("/api/auth/login", { method: "POST",
      headers: { "Content-Type": "text/plain" }, body: JSON.stringify(body) });
    return response.status;
  }, credentials("attacker"));
  assert.equal(status, UNSUPPORTED_MEDIA);
  assert.equal(state.loginCalls, before);
  await victimCookie(context, origin);
  await page.goto(attacker);
  const responsePromise = page.waitForResponse(`${origin}/api/auth/logout`);
  await page.locator("form").evaluate((form, action) => { form.action = action; }, `${origin}/api/auth/logout`);
  await page.getByRole("button").click();
  assert.equal((await responsePromise).status(), FORBIDDEN);
  await victimCookie(context, origin);
  await page.goto(`${origin}/dashboard`);
  await page.getByText("victim@example.test", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Log out", exact: true }).click();
  await page.waitForURL(`${origin}/login`);
  assert(!(await context.cookies(origin)).some((entry) => entry.name === COOKIE));
  results.push({ check: "trusted text/plain refused; cross-site logout refused; legitimate logout succeeds", passed: true });
}

/** Stop only the test process this script created, then await its exit. */
async function stopFrontend(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise((done) => child.once("exit", done));
  child.kill();
  await exited;
}

/** Close only resources made by this run and remove its checked disposable profile. */
async function cleanup(resources, profile, tempRoot) {
  if (resources.session) {
    await resources.session.browser.close();
    await stopFrontend(resources.session.child);
  }
  await stopFrontend(resources.frontendProcess);
  if (resources.attackerServer) await new Promise((done) => resources.attackerServer.close(done));
  await new Promise((done) => resources.backend.close(done));
  assert.equal(dirname(realpathSync(profile)), realpathSync(tempRoot));
  rmSync(profile, { recursive: true });
}

/** Execute the browser acceptance sequence and return its non-secret evidence record. */
async function runBrowserChecks(context, origin, attacker, version) {
  context.setDefaultTimeout(CHECK_TIMEOUT_MS);
  const page = await context.newPage();
  await rejectedForm(page, context, origin, attacker, false, false);
  await legitimateLogin(page, context, origin);
  await rejectedForm(page, context, origin, attacker);
  await rejectedForm(page, context, origin, attacker, true);
  await otherBoundaries(page, context, origin, attacker);
  console.log(JSON.stringify({ evidence: "browser/frontend validation with a stubbed backend; not full-stack authentication validation",
    browser: version, listeners: "127.0.0.1 only", origin, attacker, results }, null, 2));
}

/** Run a fresh isolated profile against local synthetic servers and always clean them up. */
async function main() {
  assert(process.env.KERNO_BROWSER_WORK_DIR, "Set KERNO_BROWSER_WORK_DIR to a disposable work directory");
  const tempRoot = resolve(process.env.KERNO_BROWSER_WORK_DIR);
  mkdirSync(tempRoot, { recursive: true });
  const profile = mkdtempSync(join(tempRoot, "profile-"));
  const backend = createServer((request, response) => {
    backendRequest(request, response).catch(() => respond(response, UNAUTHORIZED, { detail: "stub failure" }));
  });
  const resources = { backend };
  try {
    const backendPort = await listen(backend);
    const port = await unusedPort();
    const origin = `http://localhost:${port}`;
    resources.attackerServer = createServer((req, res) => attackPage(req, res, `${origin}/api/auth/login`));
    const attacker = `http://127.0.0.1:${await listen(resources.attackerServer)}`;
    resources.frontendProcess = startFrontend(port, backendPort, tempRoot);
    resources.frontendProcess.stdout.on("data", () => {});
    resources.frontendProcess.stderr.on("data", (data) => process.stderr.write(data));
    await waitReady(port, resources.frontendProcess);
    resources.session = await launchBrowser(profile);
    await runBrowserChecks(resources.session.context, origin, attacker, resources.session.browser.version());
  } finally {
    await cleanup(resources, profile, tempRoot);
  }
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
