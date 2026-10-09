/**
 * app/api/recommendations/generate/route.ts — proxy for on-demand analysis (KER-402).
 *
 * What:  POST { control_id } → FastAPI /api/v1/recommendations/generate with
 *        the session JWT; relays the backend's JSON and status verbatim.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4).
 *        Every backend answer passes through unchanged — 201, 403, 404, 422,
 *        429 (the route is rate-limited at 10/minute), 500 — because each one
 *        means something different to the person who clicked, and mapping any
 *        of them to success or not-found here would lie to them.
 * How:   called by ControlList's Analyse button. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";
import { rejectUnsafeRequest } from "@/lib/csrf";

/** Enforce the browser origin policy before parsing input or changing state. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const rejection = rejectUnsafeRequest(request);
  if (rejection) return rejection;
  const backendResponse = await apiFetch("/api/v1/recommendations/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(await request.json()),
  });
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}
