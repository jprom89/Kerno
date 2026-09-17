/**
 * middleware.ts — the outer gate on every dashboard route (KER-301 AC-3).
 *
 * What:  requests to /dashboard/* without a session cookie are redirected to
 *        /login before any page code runs.
 * Why:   fail closed at the edge. This is a presence check only — the cookie's
 *        JWT is cryptographically validated on every dashboard page load by
 *        the layout's GET /api/v1/auth/me call (AC-4), so a forged cookie gets
 *        past this gate but dies at the layout. Two layers, one source of truth.
 * How:   Next.js runs this automatically for the matcher paths. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

// Inlined rather than imported from lib/api.ts: middleware runs in the edge
// runtime, where next/headers (pulled in by that module) is unavailable.
const SESSION_COOKIE = "kerno_session";

export function middleware(request: NextRequest): NextResponse {
  if (!request.cookies.get(SESSION_COOKIE)?.value) {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*"],
};
