/**
 * lib/servingProfile.ts — which dashboard surfaces this Next.js server offers (PILOT-READY-003, G10).
 *
 * What:  resolves KERNO_SERVING_PROFILE from this server's own environment to
 *        "full" or "pilot", once per server process, and gives the proxy routes
 *        of pilot-excluded backend surfaces the refusal to answer with before
 *        they do anything. Same contract as the API (src/api/serving_profile.py):
 *        unset or blank means "full" only on a development server (`next dev`);
 *        on any other server it is a configuration error, never widened to
 *        "full"; any other value is a configuration error everywhere.
 * Why:   the pilot API does not register the kickoff §4.3 exclusions (manual
 *        recalculation among them), so a button for one would be a dead
 *        control and a working proxy an alternate route around that decision.
 *        The backend stays authoritative; this keeps the UI consistent with it
 *        and fails closed when the profile was not stated for a deployment.
 *        Server-side only: never NEXT_PUBLIC_*, never read from a request,
 *        header, cookie, query or browser storage (in a client bundle it would
 *        always read as unset). The frontend and the API are separately
 *        configured processes and must be deployed with the same value — the
 *        shared variable name is not evidence that the two values match.
 * How:   instrumentation.ts validates the profile once when the server starts.
 *        Server components call isFullProfile(); route handlers for excluded
 *        surfaces call fullProfileRefusal() first. Both read the one cached
 *        answer from serverServingProfile(). Tests: npm test -- serving-profile.
 *
 * Stability: Next.js bundles instrumentation, route handlers and server
 * components into separate module graphs, so there is no single module
 * instance to hold the answer for all of them. Each instance resolves once, on
 * its first call, from the same process environment the server started with,
 * and then keeps that answer — or the same error — for the life of the
 * process; nothing in this application writes process.env. That is equivalent
 * to resolving once at the initialization boundary: every caller sees the
 * value instrumentation.ts validated at startup, and no request can make a
 * caller re-read it. Development versus production comes from NODE_ENV, which
 * the server command fixes (`next dev` is development; `next build` and
 * `next start` are production) and which Next.js inlines into production
 * server bundles.
 */

export const SERVING_PROFILE_VARIABLE = "KERNO_SERVING_PROFILE";
export const FULL_PROFILE = "full";
export const PILOT_PROFILE = "pilot";

export type ServingProfile = typeof FULL_PROFILE | typeof PILOT_PROFILE;

const NOT_FOUND_STATUS = 404;
const SERVICE_UNAVAILABLE_STATUS = 503;

/** The profile is missing on a non-development server, or is not exactly "full" or "pilot". */
export class ServingProfileConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ServingProfileConfigError";
  }
}

/** Return whether this Next.js server runs as a development server (`next dev`). */
export function isDevelopmentServer(): boolean {
  return process.env.NODE_ENV === "development";
}

/**
 * Return the serving profile for a configured value. Surrounding whitespace is
 * ignored. Unset or blank means "full" on a development server and is a
 * configuration error anywhere else; values are case-sensitive, and anything
 * unrecognised throws rather than being widened to "full". Messages never echo
 * the configured value.
 */
export function resolveServingProfile(value: string | undefined, development: boolean): ServingProfile {
  const stated = (value ?? "").trim();
  if (stated === "") {
    if (development) {
      return FULL_PROFILE;
    }
    throw new ServingProfileConfigError(
      `${SERVING_PROFILE_VARIABLE} must be set to "full" or "pilot" outside development`,
    );
  }
  if (stated === FULL_PROFILE || stated === PILOT_PROFILE) {
    return stated;
  }
  throw new ServingProfileConfigError(
    `${SERVING_PROFILE_VARIABLE} must be "full" or "pilot"; the configured value is not recognised`,
  );
}

type Resolution = { profile: ServingProfile } | { error: ServingProfileConfigError };

let resolution: Resolution | undefined;

/**
 * Return this server's profile, resolved on the first call from its own
 * environment and cached for the life of the process; later calls never
 * re-read the environment. A configuration error is cached too and rethrown
 * on every call, so a misconfigured server fails the same way every time.
 */
export function serverServingProfile(): ServingProfile {
  if (resolution === undefined) {
    resolution = resolveOnce();
  }
  if ("error" in resolution) {
    throw resolution.error;
  }
  return resolution.profile;
}

/** Resolve from the environment, capturing a configuration error instead of throwing it. */
function resolveOnce(): Resolution {
  try {
    return { profile: resolveServingProfile(process.env.KERNO_SERVING_PROFILE, isDevelopmentServer()) };
  } catch (error) {
    if (!(error instanceof ServingProfileConfigError)) throw error;
    return { error };
  }
}

/** Return whether this server serves the full product; throws on a configuration error. */
export function isFullProfile(): boolean {
  return serverServingProfile() === FULL_PROFILE;
}

/** The status and detail a full-profile-only route answers with instead of running. */
export interface ProfileRefusal {
  status: number;
  detail: string;
}

/**
 * Return the refusal for a route that exists only in the full profile, or null
 * in full. Pilot gets the API's own 404 shape, as if the route were absent; a
 * configuration error (missing outside development, or unrecognised) gets a
 * generic 503. Route handlers call it before the origin check, any cookie
 * read, body parse or backend call, so neither answer has side effects. Kept
 * free of next/server so server components (and their jsdom tests) can import
 * this module too.
 */
export function fullProfileRefusal(): ProfileRefusal | null {
  let profile: ServingProfile;
  try {
    profile = serverServingProfile();
  } catch (error) {
    if (!(error instanceof ServingProfileConfigError)) throw error;
    return { status: SERVICE_UNAVAILABLE_STATUS, detail: "serving profile is not configured" };
  }
  if (profile === PILOT_PROFILE) {
    return { status: NOT_FOUND_STATUS, detail: "Not Found" };
  }
  return null;
}
