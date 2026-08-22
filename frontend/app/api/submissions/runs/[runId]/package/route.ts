/**
 * app/api/submissions/runs/[runId]/package/route.ts — proxy for the frozen filing.
 *
 * What:  GET → FastAPI /api/v1/submissions/runs/{run_id}/package with the
 *        session JWT; relays the JSON body AND the Content-Disposition header,
 *        so the browser still receives a named attachment.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4).
 *        The bytes are the freeze stored at Start-run, not a live rebuild.
 * How:   called by FilingDownloadButton. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function GET(
  _request: NextRequest,
  { params }: { params: Promise<{ runId: string }> },
): Promise<NextResponse> {
  const { runId } = await params;
  const backendResponse = await apiFetch(
    `/api/v1/submissions/runs/${encodeURIComponent(runId)}/package`,
  );
  const body = await backendResponse.arrayBuffer();
  const headers = new Headers({ "Content-Type": "application/json" });
  const disposition = backendResponse.headers.get("content-disposition");
  if (disposition) {
    headers.set("Content-Disposition", disposition);
  }
  return new NextResponse(body, { status: backendResponse.status, headers });
}
