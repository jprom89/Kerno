/**
 * app/api/evidence/route.ts — browser-safe proxy for the evidence library (KER-407).
 *
 * What:  GET lists evidence; POST forwards one multipart upload to FastAPI.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4) —
 *        the session JWT lives in an httpOnly cookie only the server can read.
 *        POST is bounded before any work on the body (SEC-REMED-003): the
 *        origin is checked, the backend verifies the session and reports a
 *        role that may upload, the headers are judged, one of this process's
 *        upload slots is taken (PILOT-READY-004; a full house is a 503 at once),
 *        and only then is the body received — at most
 *        EVIDENCE_UPLOAD_MAX_BODY_BYTES, counted as the bytes arrive, within
 *        an absolute reception deadline (408), and no longer once the client
 *        leaves (400). Those bytes are buffered, not streamed, and forwarded
 *        byte-for-byte with the caller's own multipart Content-Type: this route
 *        never parses or rebuilds the form. The forward has its own deadline
 *        (504) and ends early if the client leaves (400). A connection to the
 *        backend that fails is a 502 with a string detail, not a thrown 500,
 *        unless the status and headers that got through were the API's busy
 *        503, which is then relayed with its Retry-After: the API sends that
 *        503 before reading the body and closes the connection, which can
 *        reset the socket while the upload is still being written. Nothing is
 *        retried. The slot is released on every outcome, and no stage's timer
 *        or abort listener outlives the request. The backend parses the form,
 *        applies the file, field and per-file limits, and remains the
 *        authority on who may upload.
 * How:   called by EvidenceUpload and the evidence page.
 *        Tests: npm test -- evidence-upload-route evidence-upload-admission
 *        evidence-upload-backend-reset mutation-origins.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";
import { rejectUnsafeRequest } from "@/lib/csrf";
import {
  EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS,
  EVIDENCE_UPLOAD_MAX_BODY_BYTES,
  EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS,
} from "@/lib/evidence-upload-limits";
import {
  type Deadline,
  admitUpload,
  readTextWithin,
  receiveBoundedBody,
  rejectUnacceptableUpload,
  startDeadline,
  uploadsBusy,
  verifyUploader,
} from "@/lib/upload-intake";

const BAD_REQUEST_STATUS = 400;
const BAD_GATEWAY_STATUS = 502;
const SERVICE_UNAVAILABLE_STATUS = 503;
const GATEWAY_TIMEOUT_STATUS = 504;
// A delay-seconds Retry-After (RFC 9110 §10.2.3) is relayed; anything else is dropped.
const RETRY_AFTER_SECONDS = /^[0-9]+$/;
const UPLOAD_CANCELLED = "upload was cancelled";
const BACKEND_TOO_SLOW = "the evidence service did not respond in time";
const BACKEND_UNREACHABLE = "the evidence service could not be reached";
const BACKEND_UNREADABLE = "the evidence service's answer could not be read";
// Worded exactly as the API words its busy refusal, for when only that
// refusal's status and headers survived the connection.
const UPLOADS_BUSY = "evidence uploads are busy; try again shortly";

/** What one forward produced: the backend's whole answer, or the refusal to send because the exchange broke off. */
type Exchange = { answer: Response; text: string } | { refusal: NextResponse };

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
 * Check origin, session and role, and headers before touching the body; take
 * an upload slot; then receive the body under its size and time limits and
 * forward it unparsed. The slot is held until the backend's answer has been
 * read, and given back however the upload ends.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const originRejection = rejectUnsafeRequest(request);
  if (originRejection) return originRejection;
  const uploaderRejection = await verifyUploader(request.signal);
  if (uploaderRejection) return uploaderRejection;
  const headerRejection = rejectUnacceptableUpload(request, EVIDENCE_UPLOAD_MAX_BODY_BYTES);
  if (headerRejection) return headerRejection;
  const release = admitUpload();
  if (!release) return uploadsBusy();
  try {
    const received = await receiveBoundedBody(
      request, EVIDENCE_UPLOAD_MAX_BODY_BYTES, EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS,
    );
    if (!received.complete) return received.response;
    return await forward(request, received.bytes);
  } finally {
    release();
  }
}

/**
 * Send the received bytes with the caller's own Content-Type and relay the
 * backend's status and JSON body unchanged, with its Retry-After when it sends
 * a delay in seconds (the API's busy 503 does). The backend request and the read
 * of its answer are aborted together when the client leaves or when
 * EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS passes, so neither keeps running; that,
 * and a connection to the backend that fails, is answered by brokenOff(). The
 * deadline's timer and its listener on the client's signal end with this
 * function, however it ends. An answer that arrives whole but is not JSON (a
 * gateway's HTML error page, say) is a 502 with a string detail too, so no
 * outcome of the forward is thrown for Next.js to answer.
 */
async function forward(request: NextRequest, bytes: Uint8Array<ArrayBuffer>): Promise<NextResponse> {
  const deadline = startDeadline(request.signal, EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS);
  try {
    const exchanged = await exchange(request, bytes, deadline);
    if ("refusal" in exchanged) return exchanged.refusal;
    const parsed = parsedAnswer(exchanged.text);
    if (!parsed) return NextResponse.json({ detail: BACKEND_UNREADABLE }, { status: BAD_GATEWAY_STATUS });
    return NextResponse.json(parsed.json, {
      status: exchanged.answer.status,
      headers: relayedRetryAfter(exchanged.answer),
    });
  } finally {
    deadline.dispose();
  }
}

/**
 * Make the one backend request for this upload and read its whole answer, both
 * under the deadline's signal. When either breaks off — fetch rejects, or the
 * answer's body fails after its status and headers arrived — return the
 * refusal brokenOff() gives instead of throwing. Nothing is ever retried.
 */
async function exchange(request: NextRequest, bytes: Uint8Array<ArrayBuffer>, deadline: Deadline): Promise<Exchange> {
  let answer: Response | null = null;
  try {
    answer = await apiFetch("/api/v1/evidence", {
      method: "POST",
      body: bytes,
      headers: { "Content-Type": request.headers.get("content-type") ?? "" },
      signal: deadline.signal,
    });
    return { answer, text: await readTextWithin(answer, deadline.signal) };
  } catch {
    return { refusal: brokenOff(request, deadline, answer) };
  }
}

/**
 * The refusal, always with a string detail, for a forward that broke off: a
 * 400 when the client left, a 504 when the forward deadline passed, and
 * otherwise a failed connection to the backend. The API sends its busy 503
 * with Connection: close before reading any of the body, and closing a socket
 * with the body unread resets it, often while this proxy is still writing the
 * upload: fetch then rejects, or the answer's body fails after its headers.
 * So when the status and headers that did arrive were a 503 with a
 * delay-seconds Retry-After, that busy refusal is relayed with its
 * Retry-After; anything else is a 502.
 */
function brokenOff(request: NextRequest, deadline: Deadline, answer: Response | null): NextResponse {
  if (request.signal.aborted) {
    return NextResponse.json({ detail: UPLOAD_CANCELLED }, { status: BAD_REQUEST_STATUS });
  }
  if (deadline.expired()) {
    return NextResponse.json({ detail: BACKEND_TOO_SLOW }, { status: GATEWAY_TIMEOUT_STATUS });
  }
  const retryAfter = answer?.status === SERVICE_UNAVAILABLE_STATUS ? relayedRetryAfter(answer) : undefined;
  if (retryAfter) {
    return NextResponse.json({ detail: UPLOADS_BUSY }, { status: SERVICE_UNAVAILABLE_STATUS, headers: retryAfter });
  }
  return NextResponse.json({ detail: BACKEND_UNREACHABLE }, { status: BAD_GATEWAY_STATUS });
}

/** The backend's answer parsed as JSON, or undefined when it is not JSON. */
function parsedAnswer(text: string): { json: unknown } | undefined {
  try {
    return { json: JSON.parse(text) };
  } catch {
    return undefined;
  }
}

/** The backend's Retry-After as a header to relay, when it is a plain delay in seconds; otherwise none. */
function relayedRetryAfter(backendResponse: Response): Record<string, string> | undefined {
  const retryAfter = backendResponse.headers.get("retry-after");
  return retryAfter !== null && RETRY_AFTER_SECONDS.test(retryAfter) ? { "Retry-After": retryAfter } : undefined;
}
