/**
 * lib/api.ts — the single typed gateway from the Next.js SERVER to FastAPI.
 *
 * What:  apiFetch() attaches the session JWT (from the httpOnly cookie) as a
 *        Bearer header and calls the FastAPI backend; fetchMe() is the
 *        per-page-load session check (KER-301 AC-4).
 * Why:   one wrapper so every story calls FastAPI the same way — and only
 *        from the server. KERNO_API_URL is a server-side env var, never
 *        NEXT_PUBLIC_*: the browser must never call FastAPI directly; all
 *        calls go through Next.js route handlers and server components,
 *        which hold the httpOnly cookie (decided 15 July 2026).
 * How:   import { apiFetch, fetchMe } from "@/lib/api" in server code only.
 *        Tests: npm test (frontend/__tests__/).
 */

import { cookies } from "next/headers";

/** Name of the httpOnly cookie carrying the FastAPI JWT. */
export const SESSION_COOKIE = "kerno_session";

/** The logged-in identity as returned by GET /api/v1/auth/me — display strings only. */
export interface Me {
  email: string;
  role: string;
}

/** Return the FastAPI base URL from the server-side environment, without a trailing slash. */
export function apiBaseUrl(): string {
  const url = process.env.KERNO_API_URL;
  if (!url) {
    throw new Error("KERNO_API_URL is not set (server-side env var — see .env.example)");
  }
  return url.replace(/\/+$/, "");
}

/**
 * Call a FastAPI path with the session JWT from the httpOnly cookie.
 * Server-only: uses next/headers, which does not exist in the browser bundle.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const cookieStore = await cookies();
  const token = cookieStore.get(SESSION_COOKIE)?.value;
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(`${apiBaseUrl()}${path}`, { ...init, headers, cache: "no-store" });
}

/**
 * Validate the current session against GET /api/v1/auth/me.
 * Returns the identity display strings, or null for any failure — the caller
 * (the dashboard layout) redirects to /login on null.
 */
export async function fetchMe(): Promise<Me | null> {
  try {
    const response = await apiFetch("/api/v1/auth/me");
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as Me;
  } catch {
    return null;
  }
}

