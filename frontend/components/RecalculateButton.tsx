/**
 * components/RecalculateButton.tsx — the manual bias-recalculation trigger (KER-302 AC-4/AC-6).
 *
 * What:  calls the /api/recalculate proxy, then refreshes the dashboard so the
 *        summary and timestamp re-fetch.
 * Why:   compliance_lead and vciso can refresh calibration on demand between
 *        nightly runs; the page only renders this component for those roles
 *        (UI gating — the backend endpoint enforces auth for real).
 * How:   rendered role-conditionally by app/dashboard/page.tsx. Tests: npm test.
 */

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export default function RecalculateButton() {
  const router = useRouter();
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function handleRecalculate() {
    setRunning(true);
    setMessage(null);
    const response = await fetch("/api/recalculate", { method: "POST" });
    if (response.ok) {
      const result = await response.json();
      setMessage(
        result.status === "no_new_overrides"
          ? "No new overrides since the last run."
          : `Recalculated from ${result.override_count} override(s).`,
      );
      router.refresh();
    } else {
      setMessage("Recalculation failed — check the backend logs.");
    }
    setRunning(false);
  }

  return (
    <span className="flex items-center gap-3">
      <button
        type="button"
        onClick={handleRecalculate}
        disabled={running}
        className="rounded border border-slate-300 bg-white px-3 py-1 text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-50"
      >
        {running ? "Recalculating…" : "Recalculate now"}
      </button>
      {message && <span className="text-xs text-slate-500">{message}</span>}
    </span>
  );
}
