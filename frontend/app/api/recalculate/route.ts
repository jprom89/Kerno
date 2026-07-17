/**
 * app/api/recalculate/route.ts — browser-safe proxy for the manual recalculation (KER-302 AC-4).
 *
 * What:  POST → FastAPI /api/v1/scheduler/run-recalculation with the session
 *        JWT from the httpOnly cookie; relays the JSON result.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4) —
 *        client-initiated actions go through route handlers like this one.
 *        The backend enforces auth; the UI additionally hides the button from
 *        roles that should not see it.
 * How:   called by the RecalculateButton. Tests: npm test.
 */

import { NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function POST(): Promise<NextResponse> {
  const backendResponse = await apiFetch("/api/v1/scheduler/run-recalculation", {
    method: "POST",
  });
  const body = await backendResponse.json();
  return NextResponse.json(body, { status: backendResponse.status });
}
