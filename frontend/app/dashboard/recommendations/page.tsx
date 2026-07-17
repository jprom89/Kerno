/**
 * app/dashboard/recommendations/page.tsx — the review queue page (KER-303).
 *
 * What:  fetches the open recommendations, the control catalogue (for the
 *        Edit/Reject corrected-control dropdown), and the caller's role, then
 *        renders the interactive list — read-only for auditors.
 * Why:   this is the EU AI Act Article 14 human-oversight surface.
 * How:   server component behind the dashboard auth guard. Tests: npm test.
 */

import RecommendationList from "@/components/RecommendationList";
import { fetchCoverageControls, fetchMe, fetchOpenRecommendations } from "@/lib/api";

const READ_ONLY_ROLES = ["auditor"];

export default async function RecommendationsPage() {
  const [queue, controls, me] = await Promise.all([
    fetchOpenRecommendations(),
    fetchCoverageControls(),
    fetchMe(),
  ]);
  const readOnly = me === null || READ_ONLY_ROLES.includes(me.role);

  return (
    <section>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Recommendations</h1>
        <p className="mt-1 text-sm text-slate-500">
          {queue.total} open recommendation(s) awaiting human review.
        </p>
      </div>
      <RecommendationList
        initialItems={queue.items}
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
