/**
 * components/MeetingAgenda.tsx — the monthly exception-review script.
 *
 * What:  shows counts, then every gap/partial with a plain-English "what
 *        this is" and the exact ask, plus a copy-notes button. Greens sit
 *        in a skip list.
 * Why:   the founder delivering the cycle is not a GRC expert; the pack is
 *        the meeting.
 * How:   npm test — frontend/__tests__/meeting-agenda.test.tsx
 */

"use client";

import { useState } from "react";

import type { MeetingControl, MeetingPack } from "@/lib/api";

function statusClass(status: string): string {
  if (status === "met") {
    return "bg-emerald-100 text-emerald-900";
  }
  if (status === "partial") {
    return "bg-amber-100 text-amber-900";
  }
  return "bg-red-100 text-red-900";
}

function DecisionCard({ item }: { item: MeetingControl }) {
  const evidence =
    item.evidence_titles.length > 0 ? item.evidence_titles.join(", ") : "none linked";
  return (
    <article
      className="rounded-lg border border-slate-200 bg-white p-4"
      data-testid={`decision-${item.control_ref}`}
    >
      <div className="mb-2 flex items-start justify-between gap-3">
        <h3 className="text-base font-semibold text-slate-900">
          {item.control_ref} — {item.title}
        </h3>
        <span className={`rounded-full px-2 py-0.5 text-xs font-medium uppercase ${statusClass(item.status)}`}>
          {item.status}
        </span>
      </div>
      <p className="text-sm text-slate-700">
        <span className="font-medium">What this is: </span>
        {item.what_this_means}
      </p>
      <p className="mt-1 text-sm text-slate-600">Evidence: {evidence}</p>
      <p className="mt-3 text-sm text-slate-900">
        <span className="font-medium">Ask: </span>
        {item.ask_in_the_meeting}
      </p>
      {item.open_recommendation_rationale ? (
        <p className="mt-2 text-sm text-slate-600">Kerno said: {item.open_recommendation_rationale}</p>
      ) : null}
    </article>
  );
}

export default function MeetingAgenda({ pack }: { pack: MeetingPack }) {
  const [copied, setCopied] = useState(false);

  async function copyNotes() {
    await navigator.clipboard.writeText(pack.notes_markdown);
    setCopied(true);
  }

  return (
    <div>
      <p className="rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
        {pack.preamble}
      </p>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-slate-600">
          {pack.met} met / {pack.partial} partial / {pack.gap} gap of {pack.total_controls}{" "}
          NIS2 controls · {pack.review_minutes}-minute review
        </p>
        <button
          type="button"
          onClick={copyNotes}
          className="rounded border border-slate-300 px-3 py-1 text-sm text-slate-700 hover:bg-slate-50"
        >
          {copied ? "Copied" : "Copy notes"}
        </button>
      </div>
      <h2 className="mt-8 text-lg font-semibold text-slate-900">Decisions needed today</h2>
      {pack.decisions_needed.length === 0 ? (
        <p className="mt-2 text-sm text-slate-600">No gaps or partials. Confirm nothing slipped, then stop.</p>
      ) : (
        <div className="mt-3 space-y-3">
          {pack.decisions_needed.map((item) => (
            <DecisionCard key={item.control_id} item={item} />
          ))}
        </div>
      )}
      <details className="mt-8">
        <summary className="cursor-pointer text-sm font-medium text-slate-700">
          Skip unless someone asks ({pack.skip_unless_asked.length} met)
        </summary>
        <ul className="mt-2 list-disc pl-5 text-sm text-slate-600">
          {pack.skip_unless_asked.map((item) => (
            <li key={item.control_id}>
              {item.control_ref} — {item.title} ({item.evidence_count} evidence)
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
