/**
 * app/dashboard/page.tsx — the NIS2 coverage dashboard home (KER-302 AC-1/3/4/6).
 *
 * What:  overall met/partial/gap counts, the per-category card grid, the
 *        last-recalculated timestamp, and (for compliance_lead/vciso) the
 *        manual recalculate button.
 * Why:   the first thing a design partner sees — posture at a glance, sourced
 *        live from the KER-109 system-of-record resolution.
 * How:   server component; data via lib/api.ts. Tests: npm test.
 */

import CoverageGrid from "@/components/CoverageGrid";
import RecalculateButton from "@/components/RecalculateButton";
import { fetchCoverageSummary, fetchMe } from "@/lib/api";

// UI gating only (KER-302 AC-6): the backend enforces auth on the endpoint.
const RECALCULATE_ROLES = ["compliance_lead", "vciso"];

function formatTimestamp(iso: string | null): string {
  if (!iso) {
    return "Never calibrated";
  }
  return new Date(iso).toLocaleString("en-GB", { timeZone: "UTC" }) + " UTC";
}

export default async function DashboardPage() {
  const [summary, me] = await Promise.all([fetchCoverageSummary(), fetchMe()]);
  const canRecalculate = me !== null && RECALCULATE_ROLES.includes(me.role);

  return (
    <section>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">NIS2 coverage</h1>
          <p className="mt-1 text-sm text-slate-500">
            Last recalculated: {formatTimestamp(summary.last_recalculated_at)}
          </p>
        </div>
        {canRecalculate && <RecalculateButton />}
      </div>

      <div className="mb-8 grid grid-cols-3 gap-4">
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <p className="text-3xl font-semibold text-green-800">{summary.met}</p>
          <p className="text-sm text-slate-600">met</p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <p className="text-3xl font-semibold text-amber-800">{summary.partial}</p>
          <p className="text-sm text-slate-600">partial</p>
        </div>
        <div className="rounded-lg border border-slate-200 bg-white p-4">
          <p className="text-3xl font-semibold text-red-800">{summary.gap}</p>
          <p className="text-sm text-slate-600">gap</p>
        </div>
      </div>

      <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500">
        By category
      </h2>
      <CoverageGrid categories={summary.categories} />
    </section>
  );
}
