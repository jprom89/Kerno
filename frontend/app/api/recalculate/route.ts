/**
 * app/api/recalculate/route.ts — browser-safe proxy for the manual recalculation (KER-302 AC-4).
 *
 * What:  POST → FastAPI /api/v1/scheduler/run-recalculation with the session
 *        JWT from the httpOnly cookie; relays the JSON result. Full profile
 *        only: under the pilot profile it answers 404 like the absent API route.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4) —
 *        client-initiated actions go through route handlers like this one.
 *        The backend enforces auth; the UI additionally hides the button from
 *        roles that should not see it. The pilot API does not register this
 *        endpoint (PILOT-READY-003), and this proxy must not stand in for it.
 * How:   called by the RecalculateButton. Tests: npm test -- mutation-origins serving-profile.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";
import { rejectUnsafeRequest } from "@/lib/csrf";
import { fullProfileRefusal } from "@/lib/servingProfile";

/** Refuse outside the full profile, then enforce the browser origin policy, before any cookie or backend use. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const refusal = fullProfileRefusal();
  if (refusal) return NextResponse.json({ detail: refusal.detail }, { status: refusal.status });
  const rejection = rejectUnsafeRequest(request);
  if (rejection) return rejection;
  const backendResponse = await apiFetch("/api/v1/scheduler/run-recalculation", {
    method: "POST",
  });
  const body = await backendResponse.json();
  return NextResponse.json(body, { status: backendResponse.status });
}
