/**
 * components/ControlList.tsx — the per-category control table (KER-302 AC-5,
 * Analyse action added in KER-402).
 *
 * What:  one row per control with its system-of-record status badge, source,
 *        confidence, and evidence count — plus, for the roles allowed to
 *        generate, an Analyse button that runs the hybrid engine on that one
 *        control.
 * Why:   the drill-down behind each category card; human-confirmed statuses
 *        are visually distinguishable from machine-only ones. Analyse is
 *        deliberately per-row and nothing else: no analyse-all, no batch —
 *        each call may invoke the LLM and the backend rate-limits at
 *        10/minute, so the unit of intent is one control a human is looking at.
 * How:   rendered by app/dashboard/controls/page.tsx. Tests: npm test.
 */

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import Toast from "@/components/Toast";
import type { CoverageControl } from "@/lib/api";

const STATUS_BADGE_CLASSES: Record<string, string> = {
  met: "bg-green-100 text-green-800",
  partial: "bg-amber-100 text-amber-800",
  gap: "bg-red-100 text-red-800",
};

interface ControlListProps {
  controls: CoverageControl[];
  /**
   * UI gating only (GENERATE_ROLES). Ticket A's 403 on the backend route is
   * the actual guarantee; this just avoids offering a button that would fail.
   */
  canGenerate?: boolean;
}

interface ToastState {
  message: string;
  tone: "success" | "error";
  stamp: number;
}

/** Render a FastAPI error body as a string (FastAPI's own 422 detail is a list). */
function errorDetail(body: unknown): string {
  const detail = (body as { detail?: unknown })?.detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    return detail
      .map((issue) => (issue as { msg?: string })?.msg ?? JSON.stringify(issue))
      .join("; ");
  }
  return "see backend logs";
}

export default function ControlList({ controls, canGenerate = false }: ControlListProps) {
  const router = useRouter();
  const [busyControlId, setBusyControlId] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);

  async function analyse(control: CoverageControl) {
    setBusyControlId(control.control_id);
    try {
      const response = await fetch("/api/recommendations/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ control_id: control.control_id }),
      });
      const body = await response.json().catch(() => ({}));
      if (response.status === 201) {
        // rationale_source is shown on purpose: "template" means the LLM was
        // unavailable and the prose is canned. The score is deterministic
        // either way, but a degraded run must not look like a full one.
        setToast({
          message:
            `${control.control_ref} analysed: ${body.status}, ` +
            `${body.confidence_level} confidence (rationale: ${body.rationale_source}). ` +
            `Review it under Recommendations.`,
          tone: "success",
          stamp: Date.now(),
        });
        router.refresh();
        return;
      }
      setToast({
        message: `Analysis failed (${response.status}): ${errorDetail(body)}`,
        tone: "error",
        stamp: Date.now(),
      });
    } finally {
      setBusyControlId(null);
    }
  }

  if (controls.length === 0) {
    return (
      <p className="rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
        No controls in this category.
      </p>
    );
  }
  return (
    <div>
      <table className="w-full border-collapse overflow-hidden rounded-lg border border-slate-200 bg-white text-sm">
        <thead>
          <tr className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <th className="px-4 py-2">Ref</th>
            <th className="px-4 py-2">Title</th>
            <th className="px-4 py-2">Status</th>
            <th className="px-4 py-2">Source</th>
            <th className="px-4 py-2">Evidence</th>
            {canGenerate && <th className="px-4 py-2" aria-label="Actions" />}
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
              {canGenerate && (
                <td className="px-4 py-2 text-right">
                  <button
                    type="button"
                    onClick={() => analyse(control)}
                    disabled={busyControlId !== null}
                    className="rounded border border-slate-300 px-3 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                  >
                    {busyControlId === control.control_id ? "Analysing…" : "Analyse"}
                  </button>
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {toast && <Toast key={toast.stamp} message={toast.message} tone={toast.tone} />}
    </div>
  );
}
