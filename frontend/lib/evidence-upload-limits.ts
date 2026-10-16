/**
 * lib/evidence-upload-limits.ts — evidence upload limits shared by the upload form and the proxy (SEC-REMED-003, PILOT-READY-004).
 *
 * What:  the per-file limit and the total request-body limit for one upload;
 *        how many uploads one proxy process handles at once; and how long the
 *        proxy waits for the session check, for the body and for the backend.
 * Why:   the size limits mirror MAX_EVIDENCE_UPLOAD_BYTES and
 *        EVIDENCE_UPLOAD_MAX_BODY_BYTES in config/constants.py, so the proxy
 *        never refuses an upload the backend would accept or receives more than
 *        the backend would; the admission cap and Retry-After mirror
 *        EVIDENCE_UPLOAD_MAX_ACTIVE and EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS.
 *        Backend tests fail if they drift: tests/unit/api/test_evidence_upload_bounds.py
 *        (sizes) and tests/unit/api/test_evidence_upload_policy_consistency.py
 *        (admission, Retry-After and the forwarding deadline).
 * How:   import from client or server code; this module imports nothing.
 */

/** Largest document accepted, in bytes (10 MiB, the backend's file limit). */
export const MAX_EVIDENCE_FILE_BYTES = 10 * 1024 * 1024;

/** Room for the multipart boundaries, part headers and the record_type and title values. */
export const EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES = 64 * 1024;

/** Largest request body the proxy will receive for one upload, counted as the bytes arrive. */
export const EVIDENCE_UPLOAD_MAX_BODY_BYTES = MAX_EVIDENCE_FILE_BYTES + EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES;

/**
 * Uploads one Next.js server process receives and forwards at once. The count
 * is per process, not per deployment: every server process and every replica
 * keeps its own, so a deployment admits this many times its process count.
 * One more is refused with a 503 at once; nothing waits in a queue.
 */
export const EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS = 2;

/** Seconds the 503 for a full house tells the caller to wait (its Retry-After). */
export const EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS = 5;

/** Longest the proxy waits for the backend to verify the session and report its role, answer body included. */
export const EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS = 10_000;

/**
 * Longest the proxy spends receiving one upload body from the browser, counted
 * from the first read and never reset by arriving bytes, so a slow trickle
 * cannot keep an upload alive. The largest permitted body (about 10.06 MiB)
 * fits within it at about 176 KB/s, roughly 1.4 Mbit/s.
 */
export const EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS = 60_000;

/**
 * Longest the proxy waits on the backend for one forwarded upload, its
 * response body included. Longer than the backend's own reception deadline
 * plus its PDF extraction deadline, so the proxy never abandons an upload the
 * backend can still complete.
 */
export const EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS = 75_000;
