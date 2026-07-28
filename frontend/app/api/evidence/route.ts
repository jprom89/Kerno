/**
 * app/api/evidence/route.ts — browser-safe proxy for the evidence library (KER-407).
 *
 * What:  GET lists evidence; POST forwards a multipart upload to FastAPI.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4) —
 *        the session JWT lives in an httpOnly cookie only the server can read.
 *        The upload body is re-sent as FormData rather than parsed, so the
 *        file streams through untouched and no size limit is imposed here
 *        beyond the backend's own.
 * How:   called by EvidenceUpload and the evidence page. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const linked = request.nextUrl.searchParams.get("linked");
  const query = linked === null ? "" : `?linked=${linked}`;
  const backendResponse = await apiFetch(`/api/v1/evidence${query}`);
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  // Forward the multipart body as-is. Content-Type is deliberately NOT set:
  // fetch regenerates it with the correct multipart boundary for this FormData.
  const formData = await request.formData();
  const backendResponse = await apiFetch("/api/v1/evidence", {
    method: "POST",
    body: formData,
  });
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}
