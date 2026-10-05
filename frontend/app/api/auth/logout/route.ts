/**
 * app/api/auth/logout/route.ts — end the session by destroying the cookie (KER-301).
 *
 * What:  POST → clear the httpOnly session cookie, return { ok: true }.
 * Why:   the browser cannot delete an httpOnly cookie itself; only this
 *        server route can. The caller (NavHeader's logout button) redirects
 *        to /login after the cookie is gone.
 * How:   called by the logout button. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { SESSION_COOKIE } from "@/lib/api";
import { rejectUnsafeRequest } from "@/lib/csrf";

/** Enforce the browser origin policy before parsing input or changing state. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const rejection = rejectUnsafeRequest(request);
  if (rejection) return rejection;
  const response = NextResponse.json({ ok: true });
  // Max-Age 0 tells the browser to drop the cookie immediately.
  response.cookies.set(SESSION_COOKIE, "", {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
  return response;
}
