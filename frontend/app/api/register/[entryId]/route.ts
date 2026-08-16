/**
 * app/api/register/[entryId]/route.ts — browser-safe proxy for amending one entry (KER-410).
 *
 * What:  PATCH forwards an amendment to FastAPI and relays the status verbatim.
 * Why:   a 404 from the backend must reach the caller as a 404, not be
 *        flattened into a generic failure — an amendment to a filed regulatory
 *        record that silently does nothing is the worst outcome here. The
 *        backend records the previous values in the KER-107 ledger (KER-409).
 * How:   called by RegisterEntryForm on the detail page. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function PATCH(
  request: NextRequest,
  { params }: { params: Promise<{ entryId: string }> },
): Promise<NextResponse> {
  const { entryId } = await params;
  const backendResponse = await apiFetch(
    `/api/v1/register/entries/${encodeURIComponent(entryId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(await request.json()),
    },
  );
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}
