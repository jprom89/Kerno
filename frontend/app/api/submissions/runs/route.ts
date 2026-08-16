/**
 * app/api/submissions/runs/route.ts — proxy for submission runs (KER-411).
 *
 * What:  GET returns this tenant's run history; POST starts a run for a window.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4).
 *        The body carries only submission_window_id — tenant and actor come
 *        from the JWT the server attaches, and the backend appends the KER-107
 *        ledger entry (KER-409). Status is relayed verbatim so an unknown
 *        window stays a 404 rather than becoming a generic failure.
 * How:   called by SubmissionsView. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function GET(): Promise<NextResponse> {
  const backendResponse = await apiFetch("/api/v1/submissions/runs");
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  const backendResponse = await apiFetch("/api/v1/submissions/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(await request.json()),
  });
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}
