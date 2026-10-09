/**
 * app/api/export/route.ts — browser-safe proxy for the evidence pack download (KER-304).
 *
 * What:  GET ?control_family=… → FastAPI /api/v1/export/evidence-pack with the
 *        session JWT; relays the JSON body AND the Content-Disposition header,
 *        so the browser still receives a named attachment. Only a same-origin
 *        fetch is relayed (SEC-REMED-002 review finding 2.2): the request's
 *        Sec-Fetch-Site must be exactly "same-origin", which is what the
 *        dashboard's ExportButton sends. Everything else — cross-site or
 *        same-site navigation, a typed or bookmarked URL ("none"), a missing
 *        header, an unrecognised value — is a 403 before the session cookie is
 *        read or the backend is called.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4).
 *        Verified: control_family IS the compliance_controls.category value —
 *        build_evidence_pack feeds it straight into the coverage category
 *        filter, so the dashboard passes the category name unchanged. The
 *        backend appends an export_generated ledger entry on every call, and
 *        a cross-site top-level navigation carries the Lax session cookie, so
 *        without this gate any page could add entries to a signed-in user's
 *        append-only ledger. Origin is not sent on GET, hence Fetch Metadata.
 * How:   called by ExportButton. Tests: npm test -- export-route;
 *        browser: node scripts/csrf-browser.mjs (see docs/sec_remed_002_login_csrf.md).
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";
import { rejectUnlessSameOriginFetch, withFetchMetadataCacheHeaders } from "@/lib/csrf";

/** Relay the export for a same-origin fetch only; refuse before any cookie read or backend call otherwise. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const rejection = rejectUnlessSameOriginFetch(request);
  if (rejection) return rejection;
  const controlFamily = request.nextUrl.searchParams.get("control_family") ?? "";
  const backendResponse = await apiFetch(
    `/api/v1/export/evidence-pack?control_family=${encodeURIComponent(controlFamily)}`,
  );
  const body = await backendResponse.arrayBuffer();
  const headers = withFetchMetadataCacheHeaders(new Headers({ "Content-Type": "application/json" }));
  const disposition = backendResponse.headers.get("content-disposition");
  if (disposition) {
    headers.set("Content-Disposition", disposition);
  }
  return new NextResponse(body, { status: backendResponse.status, headers });
}
