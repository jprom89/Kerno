/**
 * app/api/evidence/[recordId]/links/[controlId]/route.ts — proxy for unlinking (KER-407).
 *
 * What:  DELETE -> FastAPI soft-delete (sets removed_at; history is preserved).
 * Why:   browser-safe path for a write that needs the httpOnly session cookie.
 * How:   called by EvidenceList's unlink action. Tests: npm test.
 */

import { NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function DELETE(
  _request: Request,
  { params }: { params: Promise<{ recordId: string; controlId: string }> },
): Promise<NextResponse> {
  const { recordId, controlId } = await params;
  const backendResponse = await apiFetch(
    `/api/v1/evidence/${recordId}/links/${controlId}`,
    { method: "DELETE" },
  );
  // The backend returns 204 with no body; mirror that rather than inventing one.
  return new NextResponse(null, { status: backendResponse.status });
}
