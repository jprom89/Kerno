/**
 * app/api/evidence/[recordId]/links/route.ts — proxy for linking evidence (KER-407).
 *
 * What:  POST { control_id, relevance_score, note } -> FastAPI link endpoint.
 * Why:   browser-safe path for a write that needs the httpOnly session cookie.
 * How:   called by LinkEvidenceForm. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ recordId: string }> },
): Promise<NextResponse> {
  const { recordId } = await params;
  const body = await request.json();
  const backendResponse = await apiFetch(`/api/v1/evidence/${recordId}/links`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}
