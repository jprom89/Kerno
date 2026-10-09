/**
 * lib/evidence-upload-limits.ts — evidence upload size limits shared by the upload form and the proxy (SEC-REMED-003).
 *
 * What:  the per-file limit and the total request-body limit for one upload.
 * Why:   they mirror MAX_EVIDENCE_UPLOAD_BYTES and EVIDENCE_UPLOAD_MAX_BODY_BYTES
 *        in config/constants.py, so the proxy never refuses an upload the backend
 *        would accept or receives more than the backend would. A backend test,
 *        tests/unit/api/test_evidence_upload_bounds.py, fails if the two drift.
 * How:   import from client or server code; this module imports nothing.
 */

/** Largest document accepted, in bytes (10 MiB, the backend's file limit). */
export const MAX_EVIDENCE_FILE_BYTES = 10 * 1024 * 1024;

/** Room for the multipart boundaries, part headers and the record_type and title values. */
export const EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES = 64 * 1024;

/** Largest request body the proxy will receive for one upload, counted as the bytes arrive. */
export const EVIDENCE_UPLOAD_MAX_BODY_BYTES = MAX_EVIDENCE_FILE_BYTES + EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES;
