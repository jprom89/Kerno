/**
 * components/FilingDownloadButton.tsx — download the frozen DORA filing for one run.
 *
 * What:  fetches the stored filing JSON through the Next.js proxy and saves it
 *        as an attachment. Spinner + disabled while the download runs. A 404
 *        means this run never froze a package (a draft, or a pre-migration
 *        row) and tells the user to start a new run.
 * Why:   the file an auditor hashes must be the package recorded at Start-run,
 *        not a live rebuild of today's register. Visibility is role-gated in
 *        the UI ONLY — Ticket A / SUBMISSION_CAPABLE_ROLES 403s everyone else;
 *        hiding the button is UX, the 403 is the guarantee.
 * How:   rendered by SubmissionRunView. Tests: npm test.
 */

"use client";

import { useState } from "react";

interface FilingDownloadButtonProps {
  runId: string;
}

function filenameFromDisposition(disposition: string | null, fallback: string): string {
  const match = disposition?.match(/filename="([^"]+)"/);
  return match ? match[1] : fallback;
}

export default function FilingDownloadButton({ runId }: FilingDownloadButtonProps) {
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleDownload() {
    setRunning(true);
    setError(null);
    const response = await fetch(`/api/submissions/runs/${encodeURIComponent(runId)}/package`);
    if (response.status === 404) {
      setError("Filing unavailable — start a new run.");
    } else if (response.ok) {
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filenameFromDisposition(
        response.headers.get("content-disposition"),
        `kerno-dora-filing-${runId}.json`,
      );
      anchor.click();
      URL.revokeObjectURL(url);
    } else {
      setError(`Download failed (${response.status}).`);
    }
    setRunning(false);
  }

  return (
    <span className="flex items-center gap-2">
      <button
        type="button"
        onClick={handleDownload}
        disabled={running}
        className="flex items-center gap-2 rounded border border-slate-300 bg-white px-3 py-1 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
      >
        {running && (
          <span
            aria-hidden="true"
            className="h-3 w-3 animate-spin rounded-full border-2 border-slate-400 border-t-transparent"
          />
        )}
        {running ? "Downloading…" : "Download filing"}
      </button>
      {error && <span className="text-xs text-red-700">{error}</span>}
    </span>
  );
}
