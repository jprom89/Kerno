/**
 * lib/roles.ts — which roles the UI offers write actions to, and which may upload evidence.
 *
 * What:  the role allow-lists the register screens use to decide whether to
 *        render create and edit controls, and the list the evidence upload
 *        proxy checks before it reads an upload body.
 * Why:   shared between the register list and detail pages, so one edit changes
 *        both. The display lists are UI gating ONLY — Ticket A gates the routes
 *        themselves, and a 403 is what actually stops an auditor writing. Hiding
 *        a button is a courtesy to the user, not a security control. If these
 *        lists ever disagree with the server's, the server wins and the UI is
 *        the bug. EVIDENCE_UPLOAD_ROLES is the one early refusal (see its note).
 * How:   import into server components that already know the caller's role,
 *        and into lib/upload-intake.ts for the upload proxy.
 */

/** Roles permitted to add or amend a DORA register entry (mirrors REGISTER_CAPABLE_ROLES). */
export const REGISTER_WRITE_ROLES = ["compliance_lead", "vciso"];

/**
 * Roles permitted to start a submission run (mirrors SUBMISSION_CAPABLE_ROLES).
 *
 * Deliberately a separate constant from REGISTER_WRITE_ROLES even though the
 * two lists are identical today — the server keeps them separate for the same
 * reason: editing the register and filing it are different authorities and may
 * not stay the same. Reusing one for both would make a future role change to
 * one silently move the other.
 */
export const SUBMISSION_WRITE_ROLES = ["compliance_lead", "vciso"];

/**
 * Roles permitted to trigger an on-demand analysis of a control (mirrors
 * GENERATE_CAPABLE_ROLES).
 *
 * Not REGISTER_WRITE_ROLES reused: generate includes security_engineer, which
 * register writes do not — the lists differ TODAY, not just potentially.
 */
export const GENERATE_ROLES = ["compliance_lead", "vciso", "security_engineer"];

/**
 * Roles permitted to upload evidence (mirrors EVIDENCE_CAPABLE_ROLES in
 * src/services/evidence_intake.py; tests/unit/api/test_evidence_upload_policy_consistency.py
 * fails if the two drift).
 *
 * Unlike the lists above, this one is enforced server-side as well: the upload
 * proxy refuses every other role before reading a byte of the body. It judges
 * only the role the backend reports for the verified session, never a claim in
 * the cookie or the request, and the backend's own gate still decides.
 */
export const EVIDENCE_UPLOAD_ROLES = ["compliance_lead", "vciso", "security_engineer"];
