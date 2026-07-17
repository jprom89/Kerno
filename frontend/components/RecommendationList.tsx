/**
 * components/RecommendationList.tsx — the human-in-the-loop review queue (KER-303).
 *
 * What:  each open recommendation with confidence badge, evidence count, and
 *        the three actions mapped EXACTLY to the KER-106 backend vocabulary
 *        (decided 15 July 2026): Approve → "approve" (immediate), Edit →
 *        "edit" and Reject → "reject" (both open the shared OverrideForm,
 *        which requires justification AND a corrected control). Client-side
 *        filters by confidence band and category; auditors get a read-only
 *        view (the backend enforces their 403 for real).
 * Why:   EU AI Act Article 14 — this is where humans oversee the machine.
 * How:   rendered by app/dashboard/recommendations/page.tsx. Tests: npm test.
 */

"use client";

import { useMemo, useState } from "react";

import OverrideForm, { type ControlOption } from "@/components/OverrideForm";
import Toast from "@/components/Toast";
import type { OpenRecommendation } from "@/lib/api";

// Badge colour keys off the SERVER's confidence_level (one source of truth —
// §14 KER-303 design decision 1), never off frontend-derived cutoffs.
const CONFIDENCE_BADGE_CLASSES: Record<string, string> = {
  high: "bg-green-100 text-green-800",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-red-100 text-red-800",
};

const FULL_PERCENT = 100;

interface RecommendationListProps {
  initialItems: OpenRecommendation[];
  controls: ControlOption[];
  readOnly: boolean;
}

interface ToastState {
  message: string;
  tone: "success" | "error";
  stamp: number;
}

export default function RecommendationList({
  initialItems,
  controls,
  readOnly,
}: RecommendationListProps) {
  const [items, setItems] = useState(initialItems);
  const [openFormFor, setOpenFormFor] = useState<{ id: string; action: "edit" | "reject" } | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);
  const [confidenceFilter, setConfidenceFilter] = useState("all");
  const [categoryFilter, setCategoryFilter] = useState("all");

  const categories = useMemo(
    () => Array.from(new Set(items.map((item) => item.category).filter(Boolean))) as string[],
    [items],
  );

  const visible = items.filter(
    (item) =>
      (confidenceFilter === "all" || item.confidence_level === confidenceFilter) &&
      (categoryFilter === "all" || item.category === categoryFilter),
  );

  async function submitAction(
    item: OpenRecommendation,
    action: "approve" | "edit" | "reject",
    correctedControlId?: string,
    justification?: string,
  ) {
    setSubmitting(true);
    const response = await fetch("/api/overrides", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action_type: action,
        original_control_id: item.control_id,
        corrected_control_id: correctedControlId ?? null,
        justification_text: justification ?? null,
      }),
    });
    if (response.status === 201) {
      setItems((current) => current.filter((row) => row.recommendation_id !== item.recommendation_id));
      setToast({ message: `Recorded ${action} for ${item.control_ref ?? item.control_id}.`, tone: "success", stamp: Date.now() });
      setOpenFormFor(null);
    } else {
      const body = await response.json().catch(() => ({}));
      setToast({
        message: `Action failed (${response.status}): ${body.detail ?? "see backend logs"}`,
        tone: "error",
        stamp: Date.now(),
      });
    }
    setSubmitting(false);
  }

  if (items.length === 0) {
    return (
      <p className="rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
        No open recommendations — everything the AI has proposed has been reviewed.
      </p>
    );
  }

  return (
    <div>
      <div className="mb-4 flex gap-3 text-sm">
        <label className="flex items-center gap-2">
          <span className="text-slate-600">Confidence</span>
          <select
            value={confidenceFilter}
            onChange={(event) => setConfidenceFilter(event.target.value)}
            className="rounded border border-slate-300 px-2 py-1"
            aria-label="Filter by confidence"
          >
            <option value="all">all</option>
            <option value="high">high</option>
            <option value="medium">medium</option>
            <option value="low">low</option>
          </select>
        </label>
        <label className="flex items-center gap-2">
          <span className="text-slate-600">Category</span>
          <select
            value={categoryFilter}
            onChange={(event) => setCategoryFilter(event.target.value)}
            className="rounded border border-slate-300 px-2 py-1"
            aria-label="Filter by category"
          >
            <option value="all">all</option>
            {categories.map((category) => (
              <option key={category} value={category}>
                {category.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
      </div>

      <ul className="space-y-3">
        {visible.map((item) => (
          <li
            key={item.recommendation_id}
            className="rounded-lg border border-slate-200 bg-white p-4"
            data-testid={`recommendation-${item.recommendation_id}`}
          >
            <div className="flex items-start justify-between gap-4">
              <div>
                <p className="font-mono text-xs text-slate-500">
                  {item.control_ref ?? item.control_id}
                  {item.category && (
                    <span className="ml-2 capitalize">· {item.category.replace(/_/g, " ")}</span>
                  )}
                </p>
                <p className="mt-1 text-sm font-medium text-slate-900">
                  {item.control_title ?? "(control not in catalogue)"} — proposed status:{" "}
                  <span className="font-semibold">{item.status}</span>
                </p>
                <p className="mt-1 text-xs text-slate-600">{item.rationale}</p>
                <p className="mt-1 text-xs text-slate-500">
                  {item.evidence_count} evidence record(s) ·{" "}
                  {new Date(item.generated_at).toLocaleDateString("en-GB")}
                </p>
              </div>
              <div className="flex flex-col items-end gap-2">
                <span
                  className={`rounded px-2 py-0.5 text-xs font-medium ${
                    CONFIDENCE_BADGE_CLASSES[item.confidence_level] ?? "bg-slate-100 text-slate-700"
                  }`}
                >
                  {Math.round(item.confidence_score * FULL_PERCENT)}% · {item.confidence_level}
                </span>
                {!readOnly && (
                  <div className="flex gap-2">
                    <button
                      type="button"
                      disabled={submitting}
                      onClick={() => submitAction(item, "approve")}
                      className="rounded bg-green-700 px-3 py-1 text-xs font-medium text-white disabled:opacity-40"
                    >
                      Approve
                    </button>
                    <button
                      type="button"
                      disabled={submitting}
                      onClick={() => setOpenFormFor({ id: item.recommendation_id, action: "edit" })}
                      className="rounded border border-slate-300 px-3 py-1 text-xs text-slate-700"
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      disabled={submitting}
                      onClick={() => setOpenFormFor({ id: item.recommendation_id, action: "reject" })}
                      className="rounded border border-red-300 px-3 py-1 text-xs text-red-700"
                    >
                      Reject
                    </button>
                  </div>
                )}
              </div>
            </div>
            {openFormFor?.id === item.recommendation_id && (
              <OverrideForm
                action={openFormFor.action}
                initialJustification={item.rationale}
                controls={controls}
                submitting={submitting}
                onSubmit={(correctedControlId, justification) =>
                  submitAction(item, openFormFor.action, correctedControlId, justification)
                }
                onCancel={() => setOpenFormFor(null)}
              />
            )}
          </li>
        ))}
      </ul>
      {visible.length === 0 && (
        <p className="mt-4 text-sm text-slate-600">No recommendations match the current filters.</p>
      )}
      {toast && <Toast key={toast.stamp} message={toast.message} tone={toast.tone} />}
    </div>
  );
}
