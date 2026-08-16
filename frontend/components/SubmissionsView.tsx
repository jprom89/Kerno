/**
 * components/SubmissionsView.tsx — filing windows and run history (KER-411).
 *
 * What:  the windows open for filing today, a start-run action for the roles
 *        allowed to file, and every run this tenant has recorded.
 * Why:   filing is the second half of "a register you maintain and file". The
 *        register got its surface in KER-410; this is where a compliance lead
 *        actually validates it against a window and records the attempt.
 * How:   rendered by app/dashboard/submissions/page.tsx. Tests: npm test.
 */

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import Toast from "@/components/Toast";
import type { SubmissionRun, SubmissionWindow } from "@/lib/api";

interface SubmissionsViewProps {
  windows: SubmissionWindow[];
  runs: SubmissionRun[];
  readOnly: boolean;
}

interface ToastState {
  message: string;
  tone: "success" | "error";
  stamp: number;
}

const STATUS_STYLES: Record<string, string> = {
  ready: "bg-green-100 text-green-800",
  draft: "bg-amber-100 text-amber-800",
};

const VALIDATION_STYLES: Record<string, string> = {
  pass: "text-green-700",
  warn: "text-amber-700",
  fail: "text-red-700",
};

/**
 * Render a FastAPI error body as a string.
 *
 * Kerno's own handlers return `detail` as a string, but FastAPI's built-in
 * request validation returns an array of objects — reachable here just by
 * sending a malformed body — and interpolating that yields "[object Object]".
 */
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

export default function SubmissionsView({ windows, runs, readOnly }: SubmissionsViewProps) {
  const router = useRouter();
  const [busyWindowId, setBusyWindowId] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastState | null>(null);

  const runByWindow = new Map(runs.map((run) => [run.submission_window_id, run]));

  async function startRun(window: SubmissionWindow) {
    setBusyWindowId(window.id);
    try {
      const response = await fetch("/api/submissions/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // Only the window. Tenant and actor come from the session JWT server-side.
        body: JSON.stringify({ submission_window_id: window.id }),
      });
      const body = await response.json().catch(() => ({}));
      // 200, not 201 — this route sets no status_code, and a second run for the
      // same window updates the existing row rather than creating another.
      if (response.ok) {
        setToast({
          message:
            `Run recorded for ${window.authority_code} ${window.reporting_year} — ` +
            `${body.status} (${body.entry_count} ${
              body.entry_count === 1 ? "entry" : "entries"
            }, validation ${body.validation_overall_status}).`,
          tone: "success",
          stamp: Date.now(),
        });
        router.refresh();
        return;
      }
      setToast({
        message: `Could not start the run (${response.status}): ${errorDetail(body)}`,
        tone: "error",
        stamp: Date.now(),
      });
    } finally {
      setBusyWindowId(null);
    }
  }

  return (
    <div className="space-y-10">
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Open filing windows
        </h2>
        {windows.length === 0 ? (
          <p className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
            No filing window is open today. Windows are supervisory reference data —
            an authority publishes them, and Kerno does not create them — so this is
            normal outside a reporting period rather than an error.
          </p>
        ) : (
          <ul className="space-y-3">
            {windows.map((window) => {
              const existing = runByWindow.get(window.id);
              return (
                <li
                  key={window.id}
                  className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-slate-200 bg-white p-4"
                >
                  <div>
                    <p className="font-medium text-slate-900">
                      {window.authority_code} · {window.reporting_year}
                    </p>
                    <p className="mt-1 text-sm text-slate-500">
                      Open {window.window_open_date} to {window.window_close_date} ·
                      register as at {window.register_reference_date}
                    </p>
                    {existing && (
                      <p className="mt-1 text-sm text-slate-500">
                        Already run —{" "}
                        <a
                          href={`/dashboard/submissions/${existing.id}`}
                          className="underline underline-offset-2"
                        >
                          view run
                        </a>
                        . Running again updates it in place.
                      </p>
                    )}
                  </div>
                  {!readOnly && (
                    <button
                      type="button"
                      onClick={() => startRun(window)}
                      disabled={busyWindowId === window.id}
                      className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
                    >
                      {busyWindowId === window.id
                        ? "Running…"
                        : existing
                          ? "Run again"
                          : "Start run"}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-500">
          Run history
        </h2>
        {runs.length === 0 ? (
          <p className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
            No submission runs yet.
          </p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                <tr>
                  <th className="px-4 py-3">Year</th>
                  <th className="px-4 py-3">Status</th>
                  <th className="px-4 py-3">Validation</th>
                  <th className="px-4 py-3">Entries</th>
                  <th className="px-4 py-3">Issues</th>
                  <th className="px-4 py-3">Last run</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    key={run.id}
                    className="border-b border-slate-100 last:border-0 hover:bg-slate-50"
                  >
                    <td className="px-4 py-3">
                      <a
                        href={`/dashboard/submissions/${run.id}`}
                        className="font-medium text-slate-900 underline-offset-2 hover:underline"
                      >
                        {run.reporting_year}
                      </a>
                    </td>
                    <td className="px-4 py-3">
                      <span
                        className={`rounded-full px-2 py-1 text-xs font-medium ${
                          STATUS_STYLES[run.status] ?? "bg-slate-100 text-slate-700"
                        }`}
                      >
                        {run.status}
                      </span>
                    </td>
                    <td
                      className={`px-4 py-3 font-medium ${
                        VALIDATION_STYLES[run.validation_overall_status] ?? "text-slate-600"
                      }`}
                    >
                      {run.validation_overall_status}
                    </td>
                    <td className="px-4 py-3 text-slate-600">{run.entry_count}</td>
                    <td className="px-4 py-3 text-slate-600">{run.validation_issue_count}</td>
                    <td className="px-4 py-3 text-slate-600">
                      {new Date(run.updated_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {toast && <Toast key={toast.stamp} message={toast.message} tone={toast.tone} />}
    </div>
  );
}
