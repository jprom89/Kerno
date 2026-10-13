/**
 * lib/servingProfileStartup.ts — the Node.js-only startup refusal for a misconfigured serving profile (PILOT-READY-003).
 *
 * What:  resolves this server's serving profile once (lib/servingProfile.ts)
 *        and, on a configuration error — unset or blank on a non-development
 *        server, or any value other than exactly "full" or "pilot" — logs the
 *        reason and ends the process with a nonzero exit code.
 * Why:   the API refuses to start on the same misconfiguration
 *        (src/api/serving_profile.py). Throwing from instrumentation is not
 *        enough: Next.js 16.2.10 `next start` only logs "Failed to prepare
 *        server" and keeps the process running (observed while implementing
 *        this), so the server exits explicitly. A separate module because
 *        process.exit is a Node.js API: instrumentation.ts imports it only on
 *        the Node.js runtime, so the Edge bundle never contains it.
 * How:   called by instrumentation.ts register(). Tests: npm test -- serving-profile.
 */

import { serverServingProfile, ServingProfileConfigError } from "./servingProfile";

const STARTUP_REFUSED_EXIT_CODE = 1;

/** Validate the serving profile for this server; on a configuration error, log it and end the process. */
export function refuseToStartOnProfileError(): void {
  try {
    serverServingProfile();
  } catch (error) {
    if (!(error instanceof ServingProfileConfigError)) throw error;
    console.error(`Kerno frontend refused to start: ${error.message}.`);
    process.exit(STARTUP_REFUSED_EXIT_CODE);
  }
}
