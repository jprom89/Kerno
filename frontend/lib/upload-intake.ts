/**
 * lib/upload-intake.ts — verified, size-bounded receipt of the evidence upload body (SEC-REMED-003).
 *
 * What:  rejectUnverifiedSession() has the backend verify the session token
 *        before any body byte is read; rejectUnacceptableUpload() refuses on
 *        headers alone; receiveBoundedBody() reads the raw body, counting the
 *        bytes that actually arrive, and stops at the first chunk over the limit.
 *        Nothing here parses multipart; the backend does that.
 * Why:   finding resource-exhaustion.evidence-buffering — the proxy called
 *        request.formData(), materialising an anonymous caller's entire
 *        multipart body in memory before any session check. A cookie is only a
 *        claim, so its presence proves nothing; GET /api/v1/auth/me verifies it.
 * How:   used by app/api/evidence/route.ts. Tests: npm test -- evidence-upload-route.
 */

import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { SESSION_COOKIE, apiFetch } from "@/lib/api";

const BAD_REQUEST_STATUS = 400;
const UNAUTHORIZED_STATUS = 401;
const CONTENT_TOO_LARGE_STATUS = 413;
const UNSUPPORTED_MEDIA_TYPE_STATUS = 415;
const BAD_GATEWAY_STATUS = 502;
const MULTIPART_FORM_DATA = "multipart/form-data";
const BOUNDARY_PARAMETER = "boundary";
const ASCII_DIGITS = /^[0-9]+$/;

/** The outcome of receiving a body: every byte within the limit, or the refusal to send instead. */
export type BoundedBody =
  | { complete: true; bytes: Uint8Array<ArrayBuffer> }
  | { complete: false; response: NextResponse };

/**
 * Every refusal here is sent before the body has been read in full, so each
 * one closes the connection once the response is out. Otherwise Node would keep
 * reading and discarding the rest of the body, as long as the client sends it,
 * to free the connection for reuse.
 */
function refusal(status: number, detail: string): NextResponse {
  return NextResponse.json({ detail }, { status, headers: { Connection: "close" } });
}

/** The 413 for a body over the limit, declared or counted. */
function tooLarge(): NextResponse {
  return refusal(CONTENT_TOO_LARGE_STATUS, "upload exceeds the size limit");
}

/**
 * Return null only when the backend verifies the session cookie's token; otherwise
 * a 401 (no cookie, or a token the backend rejects) or a 502 (verification failed).
 * Runs before the body is touched, and the backend stays the final authority on
 * the upload itself.
 */
export async function rejectUnverifiedSession(signal: AbortSignal): Promise<NextResponse | null> {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return refusal(UNAUTHORIZED_STATUS, "authentication required");
  let verification: Response;
  try {
    verification = await apiFetch("/api/v1/auth/me", { signal });
  } catch {
    return refusal(BAD_GATEWAY_STATUS, "the session could not be verified");
  }
  await verification.body?.cancel().catch(() => undefined);
  if (verification.ok) return null;
  if (verification.status === UNAUTHORIZED_STATUS) return refusal(UNAUTHORIZED_STATUS, "authentication required");
  return refusal(BAD_GATEWAY_STATUS, "the session could not be verified");
}

/** Return whether one Content-Type parameter is a non-empty boundary; names are case-insensitive. */
function isBoundary(parameter: string): boolean {
  const separator = parameter.indexOf("=");
  if (separator < 0) return false;
  const name = parameter.slice(0, separator).trim().toLowerCase();
  const value = parameter.slice(separator + 1).trim().replace(/^"(.*)"$/, "$1");
  return name === BOUNDARY_PARAMETER && value !== "";
}

/**
 * Refuse, before reading the body, what the headers alone rule out: 415 unless
 * multipart/form-data, 400 without a boundary or with a Content-Length that is
 * not plain ASCII digits, 413 when the declared length exceeds maxBodyBytes.
 * The declared length only allows an earlier refusal; the bytes are still counted.
 */
export function rejectUnacceptableUpload(request: Request, maxBodyBytes: number): NextResponse | null {
  const [mediaType, ...parameters] = (request.headers.get("content-type") ?? "").split(";");
  if (mediaType.trim().toLowerCase() !== MULTIPART_FORM_DATA) {
    return refusal(UNSUPPORTED_MEDIA_TYPE_STATUS, "multipart/form-data is required");
  }
  if (!parameters.some(isBoundary)) return refusal(BAD_REQUEST_STATUS, "multipart boundary is missing");
  const declared = request.headers.get("content-length");
  if (declared === null) return null;
  if (!ASCII_DIGITS.test(declared)) return refusal(BAD_REQUEST_STATUS, "invalid Content-Length");
  return Number(declared) > maxBodyBytes ? tooLarge() : null;
}

/** Join the received chunks into one buffer of the length already counted. */
function concatenate(chunks: Uint8Array[], length: number): Uint8Array<ArrayBuffer> {
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

/**
 * Read the raw body, counting the bytes that actually arrive, and stop at the
 * first chunk that takes the total past maxBodyBytes (413, no further read). A
 * body that fails mid-stream, because the client went away, is a 400. The
 * reader is released either way; at most maxBodyBytes are ever retained.
 *
 * The stream is released, not cancelled: the refusal's Connection: close ends
 * the socket once the 413 is written. A client still sending at that moment
 * may see a connection reset instead of the 413 (observed with Node on
 * loopback, cancelled or not), which is why the upload form checks the file
 * size before sending anything.
 */
export async function receiveBoundedBody(request: Request, maxBodyBytes: number): Promise<BoundedBody> {
  if (!request.body) return { complete: true, bytes: new Uint8Array(0) };
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let received = 0;
  try {
    let next = await reader.read();
    while (!next.done) {
      received += next.value.byteLength;
      if (received > maxBodyBytes) return { complete: false, response: tooLarge() };
      chunks.push(next.value);
      next = await reader.read();
    }
  } catch {
    return { complete: false, response: refusal(BAD_REQUEST_STATUS, "upload was not received completely") };
  } finally {
    reader.releaseLock();
  }
  return { complete: true, bytes: concatenate(chunks, received) };
}
