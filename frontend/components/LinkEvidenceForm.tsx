/**
 * components/LinkEvidenceForm.tsx — attach one evidence record to a control (KER-407).
 *
 * What:  searchable control picker plus a relevance score, submitted to the
 *        /api/evidence/{id}/links proxy.
 * Why:   the relevance score IS the verdict — the engine's confidence is the
 *        mean of these numbers — so the rubric is shown inline rather than
 *        left to the reviewer's guess. A vague score produces a vague
 *        recommendation, and nothing downstream can recover the difference.
 * How:   rendered inline by EvidenceList. Tests: npm test.
 */

"use client";

import { useMemo, useState } from "react";

export interface ControlOption {
  control_id: string;
  control_ref: string;
  title: string;
}

// The approved curation rubric. Kept visible at the point of judgement because
// a reviewer picking 0.5 "because it seemed middling" and one picking 0.5 for
// "related but does not cover this control" produce identical data with very
// different meanings.
const RUBRIC = [
  { band: "0.85 – 1.0", meaning: "directly addresses the control, verifiable (dated, signed, logged)" },
  { band: "0.6 – 0.84", meaning: "addresses the control, partial verification" },
  { band: "0.3 – 0.59", meaning: "related, but does not fully cover this control" },
  { band: "0.0 – 0.29", meaning: "outdated, draft-only, or barely related" },
];

const DEFAULT_SCORE = 0.75;

interface LinkEvidenceFormProps {
  controls: ControlOption[];
  submitting: boolean;
  onSubmit: (controlId: string, relevanceScore: number, note: string) => void;
  onCancel: () => void;
}

export default function LinkEvidenceForm({
  controls,
  submitting,
  onSubmit,
  onCancel,
}: LinkEvidenceFormProps) {
  const [search, setSearch] = useState("");
  const [controlId, setControlId] = useState("");
  const [score, setScore] = useState(DEFAULT_SCORE);
  const [note, setNote] = useState("");

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

  return (
    <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-4">
      <label className="mb-2 block">
        <span className="mb-1 block text-xs font-medium text-slate-700">Control</span>
        <input
          type="search"
          placeholder="Search by ref or title…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          aria-label="Search controls"
          className="mb-2 w-full rounded border border-slate-300 px-3 py-2 text-sm"
        />
        <select
          value={controlId}
          onChange={(event) => setControlId(event.target.value)}
          size={Math.min(matches.length + 1, 5)}
          aria-label="Control to link"
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
        >
          <option value="">— select a control —</option>
          {matches.map((control) => (
            <option key={control.control_id} value={control.control_id}>
              {control.control_ref} — {control.title}
            </option>
          ))}
        </select>
      </label>

      <label className="mb-1 block">
        <span className="mb-1 block text-xs font-medium text-slate-700">
          Relevance score: <span className="font-mono">{score.toFixed(2)}</span>
        </span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={score}
          onChange={(event) => setScore(Number(event.target.value))}
          aria-label="Relevance score"
          className="w-full"
        />
      </label>
      <dl className="mb-3 text-xs text-slate-500">
        {RUBRIC.map((entry) => (
          <div key={entry.band} className="flex gap-2">
            <dt className="w-20 shrink-0 font-mono">{entry.band}</dt>
            <dd>{entry.meaning}</dd>
          </div>
        ))}
      </dl>

      <label className="mb-3 block">
        <span className="mb-1 block text-xs font-medium text-slate-700">Note (optional)</span>
        <input
          type="text"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          aria-label="Link note"
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm"
        />
      </label>

      <div className="flex gap-2">
        <button
          type="button"
          disabled={!controlId || submitting}
          onClick={() => onSubmit(controlId, score, note.trim())}
          className="rounded bg-slate-900 px-3 py-1 text-sm font-medium text-white disabled:opacity-40"
        >
          {submitting ? "Linking…" : "Save link"}
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
