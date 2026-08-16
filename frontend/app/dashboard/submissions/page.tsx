/**
 * app/dashboard/submissions/page.tsx — filing windows and run history (KER-411).
 *
 * What:  the windows open for filing today, a start-run action, and every run
 *        this tenant has recorded.
 * Why:   completes the pair NOW.md names — a register you maintain (KER-410)
 *        and a filing you can run against it. The legacy static dashboard was
 *        the only surface for this, and Ticket B turned it off outside
 *        development.
 * How:   server component; data via lib/api.ts. Tests: npm test.
 */

import SubmissionsView from "@/components/SubmissionsView";
import { fetchMe, fetchOpenWindows, fetchSubmissionRuns } from "@/lib/api";
// UI gating only. Ticket A already 403s every other role on POST
// /api/v1/submissions/runs — hiding the button is UX, the 403 is the guarantee.
// Auditors still see the windows and the full run history.
import { SUBMISSION_WRITE_ROLES } from "@/lib/roles";

export default async function SubmissionsPage() {
  const [windows, runs, me] = await Promise.all([
    fetchOpenWindows(),
    fetchSubmissionRuns(),
    fetchMe(),
  ]);
  const readOnly = me === null || !SUBMISSION_WRITE_ROLES.includes(me.role);

  return (
    <section>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Submissions</h1>
        <p className="mt-1 text-sm text-slate-500">
          Validate the register against a supervisory filing window and record the
          result. One run is kept per window — running again updates it.
        </p>
      </div>
      <SubmissionsView windows={windows} runs={runs} readOnly={readOnly} />
    </section>
  );
}
