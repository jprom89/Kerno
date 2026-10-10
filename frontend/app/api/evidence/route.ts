/**
 * app/api/evidence/route.ts — browser-safe proxy for the evidence library (KER-407).
 *
 * What:  GET lists evidence; POST forwards one multipart upload to FastAPI.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4) —
 *        the session JWT lives in an httpOnly cookie only the server can read.
 *        POST is bounded before any work on the body (SEC-REMED-003): the
 *        origin is checked, the backend verifies the session, the headers are
 *        judged, and only then is the body received — at most
 *        EVIDENCE_UPLOAD_MAX_BODY_BYTES, counted as the bytes arrive. Those
 *        bytes are buffered, not streamed, and forwarded byte-for-byte with
 *        the caller's own multipart Content-Type: this route never parses or
 *        rebuilds the form. The backend parses it, applies the file, field
 *        and per-file limits, and remains the authority on who may upload.
 * How:   called by EvidenceUpload and the evidence page.
 *        Tests: npm test -- evidence-upload-route mutation-origins.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";
import { rejectUnsafeRequest } from "@/lib/csrf";
import { EVIDENCE_UPLOAD_MAX_BODY_BYTES } from "@/lib/evidence-upload-limits";
import { receiveBoundedBody, rejectUnacceptableUpload, rejectUnverifiedSession } from "@/lib/upload-intake";

const BAD_REQUEST_STATUS = 400;

/** Relay the existing read request with backend authentication unchanged. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const linked = request.nextUrl.searchParams.get("linked");
  const query = linked === null ? "" : `?linked=${linked}`;
  const backendResponse = await apiFetch(`/api/v1/evidence${query}`);
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}

/**
 * Check origin, session and headers before touching the body; then receive it
 * under the limit and forward it unparsed. A client that leaves mid-forward
 * cancels the backend request through request.signal.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const originRejection = rejectUnsafeRequest(request);
  if (originRejection) return originRejection;
  const sessionRejection = await rejectUnverifiedSession(request.signal);
  if (sessionRejection) return sessionRejection;
  const headerRejection = rejectUnacceptableUpload(request, EVIDENCE_UPLOAD_MAX_BODY_BYTES);
  if (headerRejection) return headerRejection;
  const received = await receiveBoundedBody(request, EVIDENCE_UPLOAD_MAX_BODY_BYTES);
  if (!received.complete) return received.response;
  let backendResponse: Response;
  try {
    backendResponse = await apiFetch("/api/v1/evidence", {
      method: "POST",
      body: received.bytes,
      headers: { "Content-Type": request.headers.get("content-type") ?? "" },
      signal: request.signal,
    });
  } catch (error) {
    if (request.signal.aborted) {
      return NextResponse.json({ detail: "upload was cancelled" }, { status: BAD_REQUEST_STATUS });
    }
    throw error;
  }
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}
