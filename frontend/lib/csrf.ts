/**
 * lib/csrf.ts — request-origin boundary for browser mutations (SEC-REMED-002).
 *
 * What: rejects untrusted origins before a route reads a body or uses a cookie.
 * Why: login creates a session even without an existing cookie; SameSite and
 *      backend CORS do not prevent an unsolicited session replacement.
 * How: call rejectUnsafeRequest first in every mutating Next.js API handler.
 *      Set KERNO_TRUSTED_ORIGINS on the server; run npm test -- csrf auth-routes.
 */

import { NextResponse } from "next/server";

const FORBIDDEN_STATUS = 403;
const UNSUPPORTED_MEDIA_TYPE_STATUS = 415;
const SERVICE_UNAVAILABLE_STATUS = 503;

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
