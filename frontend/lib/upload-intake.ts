/**
 * lib/upload-intake.ts — verified, admitted, size- and time-bounded receipt of the evidence upload body (SEC-REMED-003, PILOT-READY-004).
 *
 * What:  verifyUploader() has the backend verify the session token and report
 *        its role, within a deadline, before any body byte is read, and refuses
 *        a role that cannot upload; rejectUnacceptableUpload() refuses on
 *        headers alone; admitUpload() takes one of this process's upload slots
 *        or refuses at once; receiveBoundedBody() reads the raw body, counting
 *        the bytes that actually arrive, and stops at the first chunk over the
 *        limit, at an absolute deadline or when the client leaves;
 *        readTextWithin() reads a backend answer under a deadline;
 *        startDeadline() is the one deadline every stage uses. Nothing here
 *        parses multipart; the backend does that.
 * Why:   finding resource-exhaustion.evidence-buffering — the proxy called
 *        request.formData(), materialising an anonymous caller's entire
 *        multipart body in memory before any session check. A cookie is only a
 *        claim, so its presence proves nothing and the role inside its token is
 *        never read here; GET /api/v1/auth/me verifies the token and reports
 *        the role. PILOT-READY-004 (G6) adds the finite lifetime and the cap:
 *        without them a slow trickle, a stalled backend or many uploads at once
 *        could hold this server's memory and connections indefinitely. The cap
 *        counts uploads in this Node.js process only; each server process and
 *        replica keeps its own count. Deadlines are explicit timers cleared when
 *        their stage ends, not AbortSignal.timeout: Node keeps that timer, and
 *        the signal it aborts, pending until it fires (measured on Node 24),
 *        so every upload would leave its session-check and forwarding timers
 *        behind for up to their full length.
 * How:   used by app/api/evidence/route.ts.
 *        Tests: npm test -- evidence-upload-route evidence-upload-admission.
 */

import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { SESSION_COOKIE, apiFetch } from "@/lib/api";
import {
  EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS,
  EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS,
  EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS,
} from "@/lib/evidence-upload-limits";
import { EVIDENCE_UPLOAD_ROLES } from "@/lib/roles";

const BAD_REQUEST_STATUS = 400;
const UNAUTHORIZED_STATUS = 401;
const FORBIDDEN_STATUS = 403;
const REQUEST_TIMEOUT_STATUS = 408;
const CONTENT_TOO_LARGE_STATUS = 413;
const UNSUPPORTED_MEDIA_TYPE_STATUS = 415;
const BAD_GATEWAY_STATUS = 502;
const SERVICE_UNAVAILABLE_STATUS = 503;
const MULTIPART_FORM_DATA = "multipart/form-data";
const BOUNDARY_PARAMETER = "boundary";
const ASCII_DIGITS = /^[0-9]+$/;
const SESSION_PATH = "/api/v1/auth/me";
const AUTHENTICATION_REQUIRED = "authentication required";
const SESSION_NOT_VERIFIED = "the session could not be verified";
const NOT_RECEIVED_COMPLETELY = "upload was not received completely";
// The name AbortSignal.timeout gives its reason, so a deadline reads the same to every caller.
const TIMEOUT_ERROR = "TimeoutError";

// Worded exactly as the backend words the same refusals, so the dashboard
// shows one message whichever side refused.
const ROLE_NOT_PERMITTED = "your role is not permitted to perform this action";
const UPLOADS_BUSY = "evidence uploads are busy; try again shortly";
const NOT_RECEIVED_IN_TIME = "upload was not received within the time limit";

// Uploads this process has admitted and not yet released. Module state is
// per Node.js process, which is exactly the scope the cap claims.
let admittedUploads = 0;

/** The outcome of receiving a body: every byte within the limit, or the refusal to send instead. */
export type BoundedBody =
  | { complete: true; bytes: Uint8Array<ArrayBuffer> }
  | { complete: false; response: NextResponse };

/** What the backend said about the session: the role it verified, or the refusal to send instead. */
type Verification = { role: string } | { refusal: NextResponse };

/** What reading the body produced before it ended: everything, or the news that it went over the limit. */
type CountedBody = { overLimit: true } | { overLimit: false; chunks: Uint8Array[]; length: number };

/** One stage's deadline: the signal to hand to the work, whether the time ran out, and the means to end it. */
export interface Deadline {
  /** Aborts when the parent signal does, with its reason, or when the time runs out, with a TimeoutError. */
  signal: AbortSignal;
  /** Whether it was the time running out, not the parent, that aborted the signal. */
  expired: () => boolean;
  /** Clear the timer and stop following the parent. Every caller runs it in a finally block; calling it again does nothing. */
  dispose: () => void;
}

/** Run action once signal aborts, at once if it already has. Returns the function that detaches it. */
function onAbort(signal: AbortSignal, action: () => void): () => void {
  if (signal.aborted) {
    action();
    return () => undefined;
  }
  signal.addEventListener("abort", action, { once: true });
  return () => signal.removeEventListener("abort", action);
}

/**
 * Start a deadline of ms milliseconds that also follows parent (the client's
 * request signal), so the work it bounds ends when either comes first. Unlike
 * AbortSignal.timeout, the timer and the listener on parent both end at
 * dispose(), so neither outlives the stage, however early the stage finishes.
 */
export function startDeadline(parent: AbortSignal, ms: number): Deadline {
  const controller = new AbortController();
  let timedOut = false;
  function expire(): void {
    if (controller.signal.aborted) return;
    timedOut = true;
    controller.abort(new DOMException("the deadline passed", TIMEOUT_ERROR));
  }
  const timer = setTimeout(expire, ms);
  const stopFollowing = onAbort(parent, () => controller.abort(parent.reason));
  function dispose(): void {
    clearTimeout(timer);
    stopFollowing();
  }
  return { signal: controller.signal, expired: () => timedOut, dispose };
}

/**
 * Every refusal here is sent before the body has been read in full, so each
 * one closes the connection once the response is out. Otherwise Node would keep
 * reading and discarding the rest of the body, as long as the client sends it,
 * to free the connection for reuse.
 */
function refusal(status: number, detail: string, headers: Record<string, string> = {}): NextResponse {
  return NextResponse.json({ detail }, { status, headers: { ...headers, Connection: "close" } });
}

/** The 413 for a body over the limit, declared or counted. */
function tooLarge(): NextResponse {
  return refusal(CONTENT_TOO_LARGE_STATUS, "upload exceeds the size limit");
}

/**
 * The refusal for a body that did not arrive in full: a 408 when the reception
 * deadline passed, otherwise a 400 (the body failed mid-stream, or the client
 * left; in a real disconnect both happen, in either order, so they share one answer).
 */
function notReceived(deadline: Deadline): NextResponse {
  if (deadline.expired()) return refusal(REQUEST_TIMEOUT_STATUS, NOT_RECEIVED_IN_TIME);
  return refusal(BAD_REQUEST_STATUS, NOT_RECEIVED_COMPLETELY);
}

/** The 502 for a session the backend could not be asked about, or answered about unusably. */
function sessionNotVerified(): Verification {
  return { refusal: refusal(BAD_GATEWAY_STATUS, SESSION_NOT_VERIFIED) };
}

/**
 * Return null only when the backend verifies the session cookie's token and
 * reports a role in EVIDENCE_UPLOAD_ROLES; otherwise a 401 (no cookie, or a
 * token the backend rejects), a 403 (a verified role that may not upload) or a
 * 502 (no usable answer within EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS, or
 * the caller left). The role comes only from the backend's answer, never from
 * the cookie or the request. Runs before the body is touched; the backend
 * stays the final authority on the upload itself.
 */
export async function verifyUploader(signal: AbortSignal): Promise<NextResponse | null> {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return refusal(UNAUTHORIZED_STATUS, AUTHENTICATION_REQUIRED);
  const deadline = startDeadline(signal, EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS);
  let verification: Verification;
  try {
    verification = await verifiedRole(deadline.signal);
  } finally {
    deadline.dispose();
  }
  if ("refusal" in verification) return verification.refusal;
  if (EVIDENCE_UPLOAD_ROLES.includes(verification.role)) return null;
  return refusal(FORBIDDEN_STATUS, ROLE_NOT_PERMITTED);
}

/** Ask the backend whose session this is, all within signal's lifetime; a 401 stays a 401, anything else unusable is a 502. */
async function verifiedRole(signal: AbortSignal): Promise<Verification> {
  let verification: Response;
  try {
    verification = await apiFetch(SESSION_PATH, { signal });
  } catch {
    return sessionNotVerified();
  }
  if (!verification.ok) {
    void verification.body?.cancel().catch(() => undefined);
    if (verification.status !== UNAUTHORIZED_STATUS) return sessionNotVerified();
    return { refusal: refusal(UNAUTHORIZED_STATUS, AUTHENTICATION_REQUIRED) };
  }
  try {
    const role = reportedRole(await readJsonWithin(verification, signal));
    return role === null ? sessionNotVerified() : { role };
  } catch {
    return sessionNotVerified();
  }
}

/** The role string in a GET /api/v1/auth/me answer, or null when the answer has none. */
function reportedRole(identity: unknown): string | null {
  if (typeof identity !== "object" || identity === null) return null;
  const role = (identity as { role?: unknown }).role;
  return typeof role === "string" ? role : null;
}

/**
 * Read a response body as text, giving up as soon as signal aborts: the
 * body's reader is cancelled, so a backend that sends its headers and then
 * stalls cannot hold the caller. Throws signal.reason on abort, and the
 * stream's own error when the body fails part-way (a connection that drops
 * after the headers: Node's fetch fails it with TypeError "terminated"). A
 * response without a body reads as the empty string.
 */
export async function readTextWithin(response: Response, signal: AbortSignal): Promise<string> {
  if (signal.aborted) {
    void response.body?.cancel().catch(() => undefined);
    throw signal.reason;
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  function stopReading(): void {
    reader.cancel(signal.reason).catch(() => undefined);
  }
  signal.addEventListener("abort", stopReading, { once: true });
  const decoder = new TextDecoder();
  let text = "";
  try {
    let next = await reader.read();
    while (!next.done) {
      text += decoder.decode(next.value, { stream: true });
      next = await reader.read();
    }
  } finally {
    signal.removeEventListener("abort", stopReading);
    reader.releaseLock();
  }
  if (signal.aborted) throw signal.reason;
  return text + decoder.decode();
}

/** Read a response body as JSON within signal's lifetime, as readTextWithin reads it; a body that is not JSON, or none, is a SyntaxError. */
async function readJsonWithin(response: Response, signal: AbortSignal): Promise<unknown> {
  return JSON.parse(await readTextWithin(response, signal));
}

/**
 * Take one of this process's upload slots, or return null when all
 * EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS are taken; it never waits. The
 * returned function gives the slot back, and calling it again does nothing,
 * so no outcome can return a slot twice and lift the cap. The count is per
 * Node.js process: a deployment admits this cap times its server processes.
 */
export function admitUpload(): (() => void) | null {
  if (admittedUploads >= EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS) return null;
  admittedUploads += 1;
  let released = false;
  function release(): void {
    if (released) return;
    released = true;
    admittedUploads -= 1;
  }
  return release;
}

/** How many uploads this process has admitted and not yet released. */
export function activeUploads(): number {
  return admittedUploads;
}

/** The 503 for an upload that found every slot taken, with the Retry-After the backend also sends. */
export function uploadsBusy(): NextResponse {
  const retryAfter = String(EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS);
  return refusal(SERVICE_UNAVAILABLE_STATUS, UPLOADS_BUSY, { "Retry-After": retryAfter });
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

/** Read until the body ends, counting bytes, and stop at the first chunk that takes the total past maxBodyBytes. */
async function readCounted(reader: ReadableStreamDefaultReader<Uint8Array>, maxBodyBytes: number): Promise<CountedBody> {
  const chunks: Uint8Array[] = [];
  let received = 0;
  let next = await reader.read();
  while (!next.done) {
    received += next.value.byteLength;
    if (received > maxBodyBytes) return { overLimit: true };
    chunks.push(next.value);
    next = await reader.read();
  }
  return { overLimit: false, chunks, length: received };
}

/**
 * Read the raw body, counting the bytes that actually arrive, and stop at the
 * first chunk that takes the total past maxBodyBytes (413, no further read). A
 * body that fails mid-stream, or whose client leaves (request.signal aborts)
 * while it is still arriving, is a 400. A body still incomplete deadlineMs
 * after the first read is a 408: the deadline is absolute, started once and
 * never reset by arriving bytes, so a trickle cannot outlast it. The reader is
 * released and the deadline disposed whatever happens, and at most
 * maxBodyBytes are ever retained.
 *
 * On the deadline or the client's departure the stream is cancelled, so
 * nothing more is pulled from the client; a cancelled read ends like a
 * finished body, which is why the signal is checked before the bytes are
 * trusted. On a 413 it is only released: the refusal's Connection: close ends
 * the socket once the 413 is written. Either way a client still sending at
 * that moment may see a connection reset instead of the refusal (observed
 * with Node on loopback for the 413), which is why the upload form checks the
 * file size before sending anything.
 */
export async function receiveBoundedBody(
  request: Request,
  maxBodyBytes: number,
  deadlineMs: number,
): Promise<BoundedBody> {
  if (!request.body) return { complete: true, bytes: new Uint8Array(0) };
  const reader = request.body.getReader();
  const deadline = startDeadline(request.signal, deadlineMs);
  const stopWatching = onAbort(deadline.signal, () => {
    reader.cancel(deadline.signal.reason).catch(() => undefined);
  });
  try {
    const counted = await readCounted(reader, maxBodyBytes);
    if (deadline.signal.aborted) return { complete: false, response: notReceived(deadline) };
    if (counted.overLimit) return { complete: false, response: tooLarge() };
    return { complete: true, bytes: concatenate(counted.chunks, counted.length) };
  } catch {
    return { complete: false, response: notReceived(deadline) };
  } finally {
    stopWatching();
    deadline.dispose();
    reader.releaseLock();
  }
}
