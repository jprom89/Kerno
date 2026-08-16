/**
 * app/api/submissions/windows/route.ts — proxy for the filing window list (KER-411).
 *
 * What:  GET returns the windows open today.
 * Why:   the FastAPI route behind this is deliberately unauthenticated (global
 *        reference data, no tenant_id, no RLS). It is proxied anyway so the
 *        browser keeps a single origin and the page stays behind the dashboard
 *        layout — but nothing here should be read as evidence that the upstream
 *        route checks a session, because it does not.
 * How:   called by the submissions page. Tests: npm test.
 */

import { NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function GET(): Promise<NextResponse> {
  const backendResponse = await apiFetch("/api/v1/submissions/windows");
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}
