/**
 * Browser-safe proxy for review decisions (KER-303), since the browser never calls FastAPI directly: after the origin
 * check (403, or 503 when unconfigured) it forwards the JSON body { action_type, original_control_id,
 * recommendation_id, corrected_control_id?, justification_text? } unchanged to /api/v1/overrides with the session JWT.
 * It relays the backend's JSON and status (201/401/403/404/409/422) and never substitutes a recommendation:
 * recommendation_id is the one the reviewer saw (SEC-REMED-005), so a 409 for a replaced one reaches the UI unretried.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";
import { rejectUnsafeRequest } from "@/lib/csrf";

/** Enforce the browser origin policy before parsing input or changing state. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const rejection = rejectUnsafeRequest(request);
  if (rejection) return rejection;
  const body = await request.json();
  const backendResponse = await apiFetch("/api/v1/overrides", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const responseBody = await backendResponse.json();
  return NextResponse.json(responseBody, { status: backendResponse.status });
}
