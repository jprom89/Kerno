/**
 * components/ControlList.tsx — the per-category control table (KER-302 AC-5).
 *
 * What:  one row per control with its system-of-record status badge, source,
 *        confidence, and evidence count.
 * Why:   the drill-down behind each category card; human-confirmed statuses
 *        are visually distinguishable from machine-only ones.
 * How:   rendered by app/dashboard/controls/page.tsx. Tests: npm test.
 */

import type { CoverageControl } from "@/lib/api";

const STATUS_BADGE_CLASSES: Record<string, string> = {
  met: "bg-green-100 text-green-800",
  partial: "bg-amber-100 text-amber-800",
  gap: "bg-red-100 text-red-800",
};

export default function ControlList({ controls }: { controls: CoverageControl[] }) {
  if (controls.length === 0) {
    return (
      <p className="rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
        No controls in this category.
      </p>
    );
  }
  return (
    <table className="w-full border-collapse overflow-hidden rounded-lg border border-slate-200 bg-white text-sm">
      <thead>
        <tr className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
          <th className="px-4 py-2">Ref</th>
          <th className="px-4 py-2">Title</th>
          <th className="px-4 py-2">Status</th>
          <th className="px-4 py-2">Source</th>
          <th className="px-4 py-2">Evidence</th>
        </tr>
      </thead>
      <tbody>
        {controls.map((control) => (
          <tr key={control.control_id} className="border-b border-slate-100">
            <td className="px-4 py-2 font-mono text-xs text-slate-700">
              {control.control_ref}
            </td>
            <td className="px-4 py-2 text-slate-900">{control.title}</td>
            <td className="px-4 py-2">
              <span
                className={`rounded px-2 py-0.5 text-xs font-medium ${
                  STATUS_BADGE_CLASSES[control.status] ?? "bg-slate-100 text-slate-700"
                }`}
              >
                {control.status}
              </span>
            </td>
            <td className="px-4 py-2 text-xs text-slate-600">
              {control.human_confirmed ? "human-confirmed" : control.status_source}
            </td>
            <td className="px-4 py-2 text-xs text-slate-600">{control.evidence_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
