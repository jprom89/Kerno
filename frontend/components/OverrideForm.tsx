/**
 * components/OverrideForm.tsx — the shared Edit/Reject inline form (KER-303 AC-3).
 *
 * What:  justification text (required, pre-filled with the recommendation's
 *        rationale) plus a required corrected-control selection from a
 *        searchable dropdown; submits action_type "edit" or "reject".
 * Why:   the backend REQUIRES corrected_control_id for both actions (KER-106,
 *        decided 15 July 2026) — the form cannot be submitted without both
 *        fields, so a well-formed request is guaranteed client-side and
 *        enforced server-side.
 * How:   rendered inline by RecommendationList. Tests: npm test.
 */

"use client";

import { useMemo, useState } from "react";

export interface ControlOption {
  control_id: string;
  control_ref: string;
  title: string;
}

interface OverrideFormProps {
  action: "edit" | "reject";
  initialJustification: string;
  controls: ControlOption[];
  submitting: boolean;
  onSubmit: (correctedControlId: string, justification: string) => void;
  onCancel: () => void;
}

export default function OverrideForm({
  action,
  initialJustification,
  controls,
  submitting,
  onSubmit,
  onCancel,
}: OverrideFormProps) {
  const [justification, setJustification] = useState(initialJustification);
  const [search, setSearch] = useState("");
  const [correctedControlId, setCorrectedControlId] = useState("");

  const matches = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) {
      return controls;
    }
    return controls.filter(
      (control) =>
        control.control_ref.toLowerCase().includes(needle) ||
        control.title.toLowerCase().includes(needle),
    );
  }, [controls, search]);

  const canSubmit = justification.trim().length > 0 && correctedControlId !== "";

  return (
    <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-4">
      <label className="mb-3 block">
        <span className="mb-1 block text-xs font-medium text-slate-700">
          Justification (required)
        </span>
        <textarea
          value={justification}
          onChange={(event) => setJustification(event.target.value)}
          rows={3}
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm text-slate-900"
          aria-label="Justification"
        />
      </label>
      <label className="mb-1 block">
        <span className="mb-1 block text-xs font-medium text-slate-700">
          Corrected control (required)
        </span>
        <input
          type="search"
          placeholder="Search by ref or title…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          className="mb-2 w-full rounded border border-slate-300 px-3 py-2 text-sm text-slate-900"
          aria-label="Search controls"
        />
        <select
          value={correctedControlId}
          onChange={(event) => setCorrectedControlId(event.target.value)}
          size={Math.min(matches.length + 1, 5)}
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm text-slate-900"
          aria-label="Corrected control"
        >
          <option value="">— select the correct control —</option>
          {matches.map((control) => (
            <option key={control.control_id} value={control.control_id}>
              {control.control_ref} — {control.title}
            </option>
          ))}
        </select>
      </label>
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          disabled={!canSubmit || submitting}
          onClick={() => onSubmit(correctedControlId, justification.trim())}
          className="rounded bg-slate-900 px-3 py-1 text-sm font-medium text-white disabled:opacity-40"
        >
          {submitting ? "Submitting…" : `Submit ${action}`}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded border border-slate-300 px-3 py-1 text-sm text-slate-700"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
