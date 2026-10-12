/**
 * lib/servingProfile.ts — which dashboard surfaces this Next.js server offers (PILOT-READY-003, G10).
 *
 * What:  resolves KERNO_SERVING_PROFILE from this server's own environment to
 *        "full" or "pilot", and gives the proxy routes of pilot-excluded
 *        backend surfaces the refusal to answer with before they do anything.
 * Why:   the pilot API does not register the kickoff §4.3 exclusions (manual
 *        recalculation among them), so a button for one would be a dead
 *        control and a working proxy an alternate route around that decision.
 *        The backend stays authoritative; this keeps the UI consistent with it.
 *        Server-side only: never NEXT_PUBLIC_*, never read from a request,
 *        header, cookie, query or browser storage (in a client bundle it would
 *        always read as unset). Unset or blank means "full" so local
 *        `npm run dev` keeps working; the API instead requires an explicit
 *        value outside development. The frontend and the API are separately
 *        configured processes and must be deployed with the same value — the
 *        shared variable name is not evidence that the two values match.
 * How:   server components call isFullProfile(); route handlers for excluded
 *        surfaces call fullProfileRefusal() first. Tests: npm test -- serving-profile.
 */

export const SERVING_PROFILE_VARIABLE = "KERNO_SERVING_PROFILE";
export const FULL_PROFILE = "full";
export const PILOT_PROFILE = "pilot";

export type ServingProfile = typeof FULL_PROFILE | typeof PILOT_PROFILE;

const NOT_FOUND_STATUS = 404;
const SERVICE_UNAVAILABLE_STATUS = 503;

/** The configured value is neither blank nor exactly "full" or "pilot". */
export class ServingProfileConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ServingProfileConfigError";
  }
}

/**
 * Return the serving profile for a configured value. Surrounding whitespace is
 * ignored and unset or blank means "full"; values are case-sensitive, and
 * anything unrecognised throws rather than being widened to "full".
 */
export function resolveServingProfile(value: string | undefined): ServingProfile {
  const stated = (value ?? "").trim();
  if (stated === "") {
    return FULL_PROFILE;
  }
  if (stated === FULL_PROFILE || stated === PILOT_PROFILE) {
    return stated;
  }
  throw new ServingProfileConfigError(
    `${SERVING_PROFILE_VARIABLE} must be "full" or "pilot"; the configured value is not recognised`,
  );
}

/** Return this server's profile from its own environment; throws on an unrecognised value. */
export function configuredServingProfile(): ServingProfile {
  return resolveServingProfile(process.env.KERNO_SERVING_PROFILE);
}

/** Return whether this server serves the full product; throws on an unrecognised value. */
export function isFullProfile(): boolean {
  return configuredServingProfile() === FULL_PROFILE;
}

/** The status and detail a full-profile-only route answers with instead of running. */
export interface ProfileRefusal {
  status: number;
  detail: string;
}

/**
 * Return the refusal for a route that exists only in the full profile, or null
 * in full. Pilot gets the API's own 404 shape, as if the route were absent; an
 * unrecognised value gets a generic 503. Route handlers call it before the
 * origin check, any cookie read, body parse or backend call, so neither answer
 * has side effects. Kept free of next/server so server components (and their
 * jsdom tests) can import this module too.
 */
export function fullProfileRefusal(): ProfileRefusal | null {
  let profile: ServingProfile;
  try {
    profile = configuredServingProfile();
  } catch (error) {
    if (!(error instanceof ServingProfileConfigError)) throw error;
    return { status: SERVICE_UNAVAILABLE_STATUS, detail: "serving profile is not configured" };
  }
  if (profile === PILOT_PROFILE) {
    return { status: NOT_FOUND_STATUS, detail: "Not Found" };
  }
  return null;
}
