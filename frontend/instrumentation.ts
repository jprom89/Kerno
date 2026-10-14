/**
 * instrumentation.ts — refuse to start this Next.js server when its serving profile is misconfigured (PILOT-READY-003).
 *
 * What:  Next.js calls register() once per server instance, before it handles
 *        requests. On the Node.js runtime it runs the startup check in
 *        lib/servingProfileStartup.ts, which ends the process with a nonzero
 *        exit code on a configuration error: KERNO_SERVING_PROFILE unset or
 *        blank on a non-development server, or any value other than exactly
 *        "full" or "pilot".
 * Why:   this is the frontend's stable server-initialization boundary for the
 *        serving profile, matching the API's refusal to start
 *        (src/api/serving_profile.py). The profile helpers also fail closed on
 *        every use, independently of this hook: the excluded proxy answers 503
 *        without a backend call and the dashboard home refuses to render.
 * How:   automatic for `next dev` and `next start`; `next build` does not run
 *        it, and no page resolves the profile at build time. The check is
 *        imported only on the Node.js runtime (Next.js's documented pattern),
 *        so the Edge bundle — middleware, which does not use the profile —
 *        never contains process.exit. Tests: npm test -- serving-profile.
 */

/** Validate the serving profile once for this server on the Node.js runtime; ends the process on a configuration error. */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") {
    return;
  }
  const { refuseToStartOnProfileError } = await import("./lib/servingProfileStartup");
  refuseToStartOnProfileError();
}
