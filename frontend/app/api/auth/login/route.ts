/**
 * app/api/auth/login/route.ts — the ONLY place the JWT becomes a cookie (KER-301).
 *
 * What:  POST { email, password } → FastAPI /api/v1/auth/login → on success,
 *        set the returned JWT as an httpOnly cookie and return { ok: true }.
 * Why:   FastAPI returns the token in the response body; setting it here as
 *        httpOnly/SameSite means the token never exists in client-readable
 *        storage — no localStorage, no client-side JS access (design
 *        decision 1, recorded in §14).
 * How:   called by the /login page form. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiBaseUrl, SESSION_COOKIE } from "@/lib/api";

// Mirrors the backend's JWT_EXPIRY_SECONDS (config/constants.py, 24 hours) so
// the cookie dies no later than the token inside it.
const SESSION_COOKIE_MAX_AGE_SECONDS = 86400;

export async function POST(request: NextRequest): Promise<NextResponse> {
  const { email, password } = await request.json();
  const backendResponse = await fetch(`${apiBaseUrl()}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
    cache: "no-store",
  });
  if (!backendResponse.ok) {
    // Uniform message regardless of which field was wrong (matches FastAPI).
    return NextResponse.json({ detail: "invalid credentials" }, { status: 401 });
  }
  const { access_token: accessToken } = await backendResponse.json();
  const response = NextResponse.json({ ok: true });
  response.cookies.set(SESSION_COOKIE, accessToken, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_COOKIE_MAX_AGE_SECONDS,
  });
  return response;
}
