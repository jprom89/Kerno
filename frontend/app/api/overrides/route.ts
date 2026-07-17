/**
 * app/api/overrides/route.ts — browser-safe proxy for override capture (KER-303 AC-2).
 *
 * What:  POST { action_type, original_control_id, corrected_control_id?,
 *        justification_text? } → FastAPI /api/v1/overrides with the session
 *        JWT; relays the backend's JSON and status (201/401/403/422).
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4).
 *        The backend enforces the action vocabulary and RBAC for real; the UI
 *        gating is convenience only.
 * How:   called by RecommendationList's action handlers. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function POST(request: NextRequest): Promise<NextResponse> {
  const body = await request.json();
  const backendResponse = await apiFetch("/api/v1/overrides", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const responseBody = await backendResponse.json();
  return NextResponse.json(responseBody, { status: backendResponse.status });
}
