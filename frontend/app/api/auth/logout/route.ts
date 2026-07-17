/**
 * app/api/auth/logout/route.ts — end the session by destroying the cookie (KER-301).
 *
 * What:  POST → clear the httpOnly session cookie, return { ok: true }.
 * Why:   the browser cannot delete an httpOnly cookie itself; only this
 *        server route can. The caller (NavHeader's logout button) redirects
 *        to /login after the cookie is gone.
 * How:   called by the logout button. Tests: npm test.
 */

import { NextResponse } from "next/server";

import { SESSION_COOKIE } from "@/lib/api";

export async function POST(): Promise<NextResponse> {
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
