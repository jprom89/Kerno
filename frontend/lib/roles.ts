/**
 * lib/roles.ts — which roles the UI offers write actions to.
 *
 * What:  the role allow-lists the register screens use to decide whether to
 *        render create and edit controls.
 * Why:   shared between the register list and detail pages, so one edit changes
 *        both. This is UI gating ONLY — Ticket A gates the routes themselves,
 *        and a 403 is what actually stops an auditor writing. Hiding a button
 *        is a courtesy to the user, not a security control. If these lists ever
 *        disagree with the server's, the server wins and the UI is the bug.
 * How:   import into server components that already know the caller's role.
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
