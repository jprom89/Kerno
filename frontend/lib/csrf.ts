/**
 * lib/csrf.ts — request-origin boundary for browser mutations (SEC-REMED-002).
 *
 * What: rejects untrusted origins before a route reads a body or uses a cookie,
 *       and (for the export GET) rejects anything but a same-origin fetch.
 * Why: login creates a session even without an existing cookie; SameSite and
 *      backend CORS do not prevent an unsolicited session replacement. The
 *      export GET writes a ledger entry, and a cross-site top-level navigation
 *      carries the Lax cookie, so Origin (absent on GET) cannot guard it.
 * How: call rejectUnsafeRequest first in every mutating Next.js API handler and
 *      rejectUnlessSameOriginFetch first in the export GET. Set
 *      KERNO_TRUSTED_ORIGINS on the server; run npm test -- csrf auth-routes export-route.
 */

import { NextResponse } from "next/server";

const FORBIDDEN_STATUS = 403;
const UNSUPPORTED_MEDIA_TYPE_STATUS = 415;
const SERVICE_UNAVAILABLE_STATUS = 503;

// The one Sec-Fetch-Site token the export GET accepts. Browsers send exactly
// this for the dashboard's fetch(); "same-site" (a sibling subdomain), "none"
// (address bar, bookmark, link from outside a browser), "cross-site", an
// absent header (a browser without Fetch Metadata, or a non-browser client)
// and anything unrecognised are all refused. Nothing is inferred from
// Referer, Host or the request URL in their place.
const SAME_ORIGIN_FETCH_SITE = "same-origin";

/** Return whether a value is one canonical HTTP(S) origin, without URL extras. */
function isSerializedOrigin(value: string): boolean {
  try {
    const parsed = new URL(value);
    return !parsed.hostname.includes("*")
      && (parsed.protocol === "https:" || parsed.protocol === "http:")
      && parsed.origin === value;
  } catch {
    return false;
  }
}

/** Read an explicit server allowlist; one missing or invalid entry fails closed. */
function trustedOrigins(): string[] | null {
  const configured = process.env.KERNO_TRUSTED_ORIGINS;
  if (!configured) return null;
  const origins = configured.split(",").map((origin) => origin.trim());
  if (!origins.every(isSerializedOrigin)) return null;
  return origins;
}

/**
 * Return a rejection or null before any side effect; requireJson is for login.
 * Missing/null/malformed Origin is forbidden, including for non-browser callers.
 * Never infer trust from request URLs, Host, Forwarded or X-Forwarded-* headers.
 */
export function rejectUnsafeRequest(request: Request, requireJson = false): NextResponse | null {
  const allowed = trustedOrigins();
  if (!allowed) {
    return NextResponse.json(
      { detail: "browser origin policy is not configured" },
      { status: SERVICE_UNAVAILABLE_STATUS },
    );
  }
  const origin = request.headers.get("origin");
  if (!origin || !isSerializedOrigin(origin) || !allowed.includes(origin)) {
    return NextResponse.json({ detail: "untrusted request origin" }, { status: FORBIDDEN_STATUS });
  }
  const mediaType = request.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
  if (requireJson && mediaType !== "application/json") {
    return NextResponse.json(
      { detail: "application/json is required" },
      { status: UNSUPPORTED_MEDIA_TYPE_STATUS },
    );
  }
  return null;
}

/**
 * Mark a response that depends on Sec-Fetch-Site as uncacheable, keeping the
 * headers it already carries. no-store because the body is per-session data
 * and the verdict is per-request; Vary records the dependency for any cache
 * that ignores no-store.
 */
export function withFetchMetadataCacheHeaders(headers: Headers): Headers {
  headers.set("Cache-Control", "no-store");
  headers.set("Vary", "Sec-Fetch-Site");
  return headers;
}

/**
 * Return a 403 unless the request's Fetch Metadata says exactly same-origin;
 * null otherwise. Runs before any cookie read or backend call. Only the
 * export GET uses it: that route has a backend side effect (an export ledger
 * entry) and is reachable by a cross-site navigation that carries the Lax
 * session cookie. There is no fallback for a missing header.
 */
export function rejectUnlessSameOriginFetch(request: Request): NextResponse | null {
  if (request.headers.get("sec-fetch-site") === SAME_ORIGIN_FETCH_SITE) {
    return null;
  }
  return NextResponse.json(
    { detail: "a same-origin dashboard request is required" },
    { status: FORBIDDEN_STATUS, headers: withFetchMetadataCacheHeaders(new Headers()) },
  );
}
