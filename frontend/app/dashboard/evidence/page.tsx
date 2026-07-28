/**
 * app/dashboard/evidence/page.tsx — the evidence library page (KER-407).
 *
 * What:  lists the tenant's evidence with link status, and (for curating
 *        roles) upload and link actions.
 * Why:   this page is what makes the product usable by a customer at all —
 *        before it, evidence could only arrive by webhook and could never be
 *        linked to a control, so every control scored "gap, no evidence".
 * How:   server component; data via lib/api.ts. Tests: npm test.
 */

import EvidenceList from "@/components/EvidenceList";
import { fetchCoverageControls, fetchEvidence, fetchMe } from "@/lib/api";

// UI gating only — the backend enforces EVIDENCE_CAPABLE_ROLES for real.
// Auditors reach this page read-only: they must be able to see the evidence
// base without being able to change it.
const CURATE_ROLES = ["compliance_lead", "vciso", "security_engineer"];

export default async function EvidencePage() {
  const [library, controls, me] = await Promise.all([
    fetchEvidence(),
    fetchCoverageControls(),
    fetchMe(),
  ]);
  const readOnly = me === null || !CURATE_ROLES.includes(me.role);

  return (
    <section>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Evidence</h1>
        <p className="mt-1 text-sm text-slate-500">
          {library.total} document{library.total === 1 ? "" : "s"} in your evidence
          library. Controls can only be assessed from evidence linked to them.
        </p>
      </div>
      <EvidenceList
        records={library.items}
        controls={controls.map((control) => ({
          control_id: control.control_id,
          control_ref: control.control_ref,
          title: control.title,
        }))}
        readOnly={readOnly}
      />
    </section>
  );
}
