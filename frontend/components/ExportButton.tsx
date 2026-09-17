/**
 * components/ExportButton.tsx — the evidence-pack download trigger (KER-304).
 *
 * What:  downloads the KER-111 evidence pack for one control family. With a
 *        fixed `family` prop it is a single button (category detail page);
 *        with a `families` list it renders a picker + button (dashboard).
 *        Spinner + disabled while the export runs (AC-4).
 * Why:   NIS2 Article 23 — one click hands an auditor the deterministic pack.
 *        Visibility is role-gated in the UI ONLY (§14 KER-304 AC-6 records
 *        this honestly): the caller renders it just for the allowed roles;
 *        server-side the endpoint is tenant-scoped + rate-limited for any
 *        authenticated role.
 * How:   rendered by the dashboard and category pages. Tests: npm test.
 */

"use client";

import { useState } from "react";

interface ExportButtonProps {
  family?: string;
  families?: string[];
}

function filenameFromDisposition(disposition: string | null, fallback: string): string {
  const match = disposition?.match(/filename="([^"]+)"/);
  return match ? match[1] : fallback;
}

export default function ExportButton({ family, families = [] }: ExportButtonProps) {
  const [selected, setSelected] = useState(family ?? families[0] ?? "");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleExport() {
    if (!selected) {
      return;
    }
    setRunning(true);
    setError(null);
    const response = await fetch(
      `/api/export?control_family=${encodeURIComponent(selected)}`,
    );
    if (response.ok) {
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filenameFromDisposition(
        response.headers.get("content-disposition"),
        `kerno-evidence-pack-${selected}.json`,
      );
      anchor.click();
      URL.revokeObjectURL(url);
    } else {
      setError(`Export failed (${response.status}).`);
    }
    setRunning(false);
  }

  return (
    <span className="flex items-center gap-2">
      {!family && families.length > 0 && (
        <select
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
          className="rounded border border-slate-300 px-2 py-1 text-sm text-slate-700"
          aria-label="Control family to export"
        >
          {families.map((name) => (
            <option key={name} value={name}>
              {name.replace(/_/g, " ")}
            </option>
          ))}
        </select>
      )}
      <button
        type="button"
        onClick={handleExport}
        disabled={running || !selected}
        className="flex items-center gap-2 rounded border border-slate-300 bg-white px-3 py-1 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
      >
        {running && (
          <span
            aria-hidden="true"
            className="h-3 w-3 animate-spin rounded-full border-2 border-slate-400 border-t-transparent"
          />
        )}
        {running ? "Exporting…" : "Export evidence pack"}
      </button>
      {error && <span className="text-xs text-red-700">{error}</span>}
    </span>
  );
}
