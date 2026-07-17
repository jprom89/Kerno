/**
 * app/api/export/route.ts — browser-safe proxy for the evidence pack download (KER-304).
 *
 * What:  GET ?control_family=… → FastAPI /api/v1/export/evidence-pack with the
 *        session JWT; relays the JSON body AND the Content-Disposition header,
 *        so the browser still receives a named attachment.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4).
 *        Verified: control_family IS the compliance_controls.category value —
 *        build_evidence_pack feeds it straight into the coverage category
 *        filter, so the dashboard passes the category name unchanged.
 * How:   called by ExportButton. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const controlFamily = request.nextUrl.searchParams.get("control_family") ?? "";
  const backendResponse = await apiFetch(
    `/api/v1/export/evidence-pack?control_family=${encodeURIComponent(controlFamily)}`,
  );
  const body = await backendResponse.arrayBuffer();
  const headers = new Headers({ "Content-Type": "application/json" });
  const disposition = backendResponse.headers.get("content-disposition");
  if (disposition) {
    headers.set("Content-Disposition", disposition);
  }
  return new NextResponse(body, { status: backendResponse.status, headers });
}
