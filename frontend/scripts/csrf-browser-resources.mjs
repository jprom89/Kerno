/**
 * What: track and clean up only resources created by the CSRF browser harness.
 * Why: launch failures and cleanup timeouts must not hide the original test failure.
 * How: imported by csrf-browser.mjs; test with node --test scripts/csrf-browser-resources.test.mjs.
 */
import { spawn } from "node:child_process";
import { realpathSync, rmSync } from "node:fs";
import { dirname } from "node:path";

export const CLEANUP_TIMEOUT_MS = 5000;

/** Await one operation with a deadline, retaining a rejection handler after a timeout. */
export async function bounded(operation, timeoutMs, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${timeoutMs} ms`)), timeoutMs);
  });
  try { return await Promise.race([operation, timeout]); }
  finally { clearTimeout(timer); }
}

/** Register ownership before launch; retain error events even before callers begin waiting. */
export function spawnOwned(resources, key, command, args, options, launch = spawn) {
  const record = { label: key, child: null, error: null, spawned: false, exited: false, closed: false };
  resources[key] = record;
  try {
    record.child = launch(command, args, options);
    record.spawned = Number.isInteger(record.child.pid);
    record.child.on("spawn", () => { record.spawned = true; });
    record.child.on("error", (error) => { record.error = error; });
    record.child.on("exit", () => { record.exited = true; });
    record.child.on("close", () => { record.closed = true; });
  } catch (error) { record.error = error; }
  return record;
}

/** Fail with the captured launch error or premature exit, without killing any other process. */
export function assertRunning(record) {
  if (record.error) throw new Error(`${record.label} launch/process error: ${record.error.message}`, { cause: record.error });
  if (record.closed || record.child.exitCode !== null || record.child.signalCode !== null) {
    throw new Error(`${record.label} exited before readiness`);
  }
}

/**
 * Stop one owned child handle and bound its exit wait; never enumerate or kill by name.
 * The wait ends at "exit" (the process is gone), not only at "close": a browser's
 * helper processes inherit its stderr pipe and can keep "close" from firing for
 * longer than the bound after the browser itself has exited, which earlier
 * reported a false cleanup failure.
 */
export async function stopOwned(record, timeoutMs = CLEANUP_TIMEOUT_MS) {
  if (!record?.child || record.closed || record.exited || (!record.spawned && record.error)) return;
  const child = record.child;
  let onEnded;
  const ended = new Promise((done, reject) => {
    onEnded = done;
    child.once("exit", onEnded);
    child.once("close", onEnded);
    try {
      if (child.exitCode === null && child.signalCode === null && !child.kill()) {
        if (record.closed || record.exited) done();
        else reject(new Error(`${record.label} refused termination`));
      }
    } catch (error) { reject(error); }
  });
  try { await bounded(ended, timeoutMs, `${record.label} exit`); }
  finally {
    child.removeListener("exit", onEnded);
    child.removeListener("close", onEnded);
    // Neither a failed termination nor a lingering inherited pipe may keep the harness open.
    if (!record.closed) {
      child.unref?.();
      for (const stream of child.stdio ?? []) stream?.unref?.();
    }
  }
}

/** Close a harness-owned listener and its own keepalive sockets, including partial startup. */
export async function closeServer(server, timeoutMs = CLEANUP_TIMEOUT_MS) {
  if (!server?.listening) return;
  const closed = new Promise((done, reject) => {
    server.close((error) => error ? reject(error) : done());
    server.closeAllConnections?.();
  });
  try { await bounded(closed, timeoutMs, "local server close"); }
  finally { server.unref?.(); }
}

/** Remove only a never-used profile inside its declared temporary parent. */
function removeUnusedProfile(profile, tempRoot) {
  if (dirname(realpathSync(profile)) !== realpathSync(tempRoot)) {
    throw new Error("Refusing profile removal outside the declared temporary directory");
  }
  rmSync(profile, { recursive: true });
}

/** Attempt every cleanup independently; a browser-used profile is conservatively retained. */
export async function cleanupOwned(resources, profile, tempRoot, options = {}) {
  const timeoutMs = options.timeoutMs ?? CLEANUP_TIMEOUT_MS;
  const report = { steps: [], errors: [], profile: "retained" };
  const attempt = async (label, action) => {
    try {
      await bounded(Promise.resolve().then(action), timeoutMs, label);
      report.steps.push({ resource: label, status: "completed" });
    } catch (error) {
      report.errors.push({ resource: label, message: error.message });
      report.steps.push({ resource: label, status: "failed" });
    }
  };
  if (resources.session) await attempt("browser connection", () => resources.session.browser.close());
  await attempt("browser process", () => stopOwned(resources.browserProcess, timeoutMs));
  await attempt("frontend process", () => stopOwned(resources.frontendProcess, timeoutMs));
  await attempt("attacker listener", () => closeServer(resources.attackerServer, timeoutMs));
  await attempt("stub backend listener", () => closeServer(resources.backend, timeoutMs));
  if (resources.browserProcess?.spawned) {
    report.profileReason = "Browser launched: parent exit cannot prove every descendant released the profile. No deletion attempted.";
  } else {
    await attempt("unused profile", () => {
      (options.removeProfile ?? removeUnusedProfile)(profile, tempRoot);
      report.profile = "removed";
    });
  }
  return report;
}

/** Preserve the original failure, report cleanup separately, and fail a cleanup-only failure. */
export async function runWithCleanup(run, cleanup, report) {
  let originalFailure;
  try { await run(); } catch (error) { originalFailure = error; }
  let cleanupResult;
  try { cleanupResult = await cleanup(); }
  catch (error) { cleanupResult = { errors: [{ resource: "cleanup", message: error.message }] }; }
  report({ originalFailure: originalFailure?.message ?? null, cleanup: cleanupResult });
  if (originalFailure) throw originalFailure;
  if (cleanupResult.errors.length) throw new Error("Owned-resource cleanup incomplete; see cleanup evidence");
}
