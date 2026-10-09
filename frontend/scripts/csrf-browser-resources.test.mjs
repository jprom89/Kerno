/**
 * What: exercise CSRF harness cleanup failures with harmless owned-resource doubles.
 * Why: startup errors, hanging exits and cleanup failures must preserve evidence and ownership.
 * How: node --test scripts/csrf-browser-resources.test.mjs; no browser, server or database starts.
 */
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { assertRunning, cleanupOwned, runWithCleanup, spawnOwned, stopOwned } from "./csrf-browser-resources.mjs";

const TEST_TIMEOUT_MS = 30;

/** Create an in-memory child handle that records only its own termination requests. */
function fakeChild(mode = "close") {
  const child = new EventEmitter();
  Object.assign(child, { pid: 1, exitCode: null, signalCode: null, kills: 0 });
  child.kill = () => {
    child.kills += 1;
    if (mode === "throw") throw new Error("kill denied");
    if (mode === "refuse") return false;
    if (mode === "close") queueMicrotask(() => { child.exitCode = 0; child.emit("exit", 0); child.emit("close", 0); });
    // "exit-only": the process is gone but an inherited stdio pipe keeps "close" from firing.
    if (mode === "exit-only") queueMicrotask(() => { child.exitCode = 0; child.emit("exit", 0); });
    return true;
  };
  return child;
}

/** Register a double with the same ownership/error handling as a real launched process. */
function owned(resources, key, mode = "close") {
  const child = fakeChild(mode);
  spawnOwned(resources, key, "unused", [], {}, () => child);
  return child;
}

/** Return an owned listener double whose close attempt is observable. */
function fakeServer(events, name, fail = false) {
  return { listening: true, close(done) { events.push(name); done(fail ? new Error(name) : undefined); },
    closeAllConnections() { events.push(`${name} sockets`); } };
}

test("captures asynchronous launch errors without an unhandled error event", async () => {
  const resources = {};
  const child = fakeChild(); child.pid = undefined;
  const record = spawnOwned(resources, "browserProcess", "missing", [], {}, () => child);
  child.emit("error", new Error("ENOENT"));
  assert.throws(() => assertRunning(record), /ENOENT/);
  await stopOwned(record, TEST_TIMEOUT_MS);
  assert.equal(child.kills, 0);
});

test("captures a synchronous launch failure and cleans the unused profile", async () => {
  const resources = {};
  const record = spawnOwned(resources, "browserProcess", "missing", [], {}, () => { throw new Error("launch denied"); });
  assert.throws(() => assertRunning(record), /launch denied/);
  let removed = false;
  const report = await cleanupOwned(resources, "unused", "unused", { removeProfile() { removed = true; } });
  assert.equal(removed, true); assert.equal(report.profile, "removed");
});

test("stops an owned handle and does not touch an unrelated process double", async () => {
  const resources = {}; const child = owned(resources, "frontendProcess");
  const unrelated = fakeChild();
  await stopOwned(resources.frontendProcess, TEST_TIMEOUT_MS);
  assert.equal(child.kills, 1); assert.equal(unrelated.kills, 0);
});

test("does not kill a child that has already closed", async () => {
  const resources = {}; const child = owned(resources, "frontendProcess");
  child.exitCode = 0; child.emit("close");
  await stopOwned(resources.frontendProcess, TEST_TIMEOUT_MS);
  assert.equal(child.kills, 0);
});

test("treats a child that exited while a descendant holds its pipe as stopped, and unrefs the pipe", async () => {
  const resources = {}; const child = owned(resources, "browserProcess", "exit-only");
  const released = [];
  child.unref = () => released.push("child");
  child.stdio = [null, null, { unref: () => released.push("stderr") }];
  await stopOwned(resources.browserProcess, TEST_TIMEOUT_MS);
  assert.equal(child.kills, 1);
  assert.equal(resources.browserProcess.exited, true);
  assert.equal(resources.browserProcess.closed, false);
  assert.deepEqual(released, ["child", "stderr"]);
});

test("does not kill or wait for a child that already exited without closing, and releases its lingering handles", async () => {
  const resources = {}; const child = owned(resources, "frontendProcess");
  const released = [];
  child.unref = () => released.push("child");
  child.stdio = [null, null, { unref: () => released.push("stderr") }];
  child.exitCode = 0; child.emit("exit", 0);
  const started = Date.now();
  await stopOwned(resources.frontendProcess, TEST_TIMEOUT_MS);
  assert.equal(child.kills, 0);
  assert.deepEqual(released, ["child", "stderr"]);
  assert.ok(Date.now() - started < TEST_TIMEOUT_MS, "must not wait for another exit event");
});

test("releases the browser child's handles when browser.close() made it exit before stopOwned ran", async () => {
  const resources = {}; const child = owned(resources, "browserProcess", "hang");
  const released = [];
  child.unref = () => released.push("child");
  child.stdio = [null, null, { unref: () => released.push("stderr") }];
  resources.session = { browser: { close() { child.exitCode = 0; child.emit("exit", 0); } } };
  const report = await cleanupOwned(resources, "profile", "root", { timeoutMs: TEST_TIMEOUT_MS,
    removeProfile() { assert.fail("A browser-used profile must not be deleted"); } });
  assert.equal(child.kills, 0);
  assert.deepEqual(released, ["child", "stderr"]);
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.steps.map((step) => `${step.resource}:${step.status}`),
    ["browser connection:completed", "browser process:completed", "frontend process:completed",
      "attacker listener:completed", "stub backend listener:completed"]);
  assert.equal(report.profile, "retained");
});

test("bounds an exit wait even if the child never closes", async () => {
  const resources = {}; const child = owned(resources, "browserProcess", "hang");
  const released = [];
  child.unref = () => released.push("child");
  child.stdio = [null, { unref: () => released.push("stdout") }, { unref: () => released.push("stderr") }];
  await assert.rejects(stopOwned(resources.browserProcess, TEST_TIMEOUT_MS), /timed out/);
  assert.deepEqual(released, ["child", "stdout", "stderr"]);
});

test("attempts every owned cleanup after close, kill and listener failures", async () => {
  const events = []; const resources = {};
  owned(resources, "browserProcess", "throw");
  const frontend = owned(resources, "frontendProcess");
  resources.session = { browser: { close() { events.push("browser"); throw new Error("CDP close failed"); } } };
  resources.attackerServer = fakeServer(events, "attacker", true);
  resources.backend = fakeServer(events, "backend");
  const report = await cleanupOwned(resources, "profile", "root", { timeoutMs: TEST_TIMEOUT_MS,
    removeProfile() { assert.fail("A browser-used profile must not be deleted"); } });
  assert.equal(frontend.kills, 1);
  assert.deepEqual(events, ["browser", "attacker", "attacker sockets", "backend", "backend sockets"]);
  assert.deepEqual(report.errors.map((e) => e.resource), ["browser connection", "browser process", "attacker listener"]);
  assert.equal(report.profile, "retained");
});

test("bounds hanging browser and server close operations and continues", async () => {
  const events = [];
  const resources = { session: { browser: { close: () => new Promise(() => {}) } },
    attackerServer: { listening: true, close() {}, closeAllConnections() {} },
    backend: fakeServer(events, "backend") };
  const report = await cleanupOwned(resources, "unused", "unused", {
    timeoutMs: TEST_TIMEOUT_MS, removeProfile() { events.push("profile"); } });
  assert.deepEqual(events, ["backend", "backend sockets", "profile"]);
  assert.equal(report.errors.length, 2);
});

test("retains a launched browser profile even after apparently successful parent exit", async () => {
  const resources = {}; owned(resources, "browserProcess");
  const report = await cleanupOwned(resources, "profile", "root", {
    removeProfile() { assert.fail("Descendant profile release is not proven"); } });
  assert.equal(report.profile, "retained"); assert.equal(report.errors.length, 0);
});

test("keeps the original test failure and separately reports cleanup failures", async () => {
  const original = new Error("original assertion failed"); let evidence;
  await assert.rejects(runWithCleanup(async () => { throw original; },
    async () => ({ errors: [{ resource: "browser", message: "close failed" }] }),
    (report) => { evidence = report; }), (error) => error === original);
  assert.equal(evidence.originalFailure, original.message);
  assert.equal(evidence.cleanup.errors[0].message, "close failed");
});

test("keeps the original failure even when cleanup unexpectedly throws", async () => {
  const original = new Error("original assertion failed"); let evidence;
  await assert.rejects(runWithCleanup(async () => { throw original; },
    async () => { throw new Error("unexpected cleanup failure"); }, (report) => { evidence = report; }),
  (error) => error === original);
  assert.equal(evidence.cleanup.errors[0].message, "unexpected cleanup failure");
});

test("a cleanup-only failure makes an otherwise successful run fail", async () => {
  await assert.rejects(runWithCleanup(async () => {}, async () => ({ errors: [{ message: "close failed" }] }),
    () => {}), /cleanup incomplete/);
});
