/**
 * app/dashboard/submissions/[runId]/page.tsx — one submission run (KER-411).
 *
 * What:  the recorded outcome of validating the register against one window.
 * Why:   the run is the evidence that a filing was attempted, what the register
 *        looked like at the time, and whether it passed — the thing an auditor
 *        asks to see. A missing or malformed id renders the 404 page.
 * How:   server component; data via lib/api.ts. Tests: npm test.
 */

import { notFound } from "next/navigation";

import { fetchSubmissionRun } from "@/lib/api";

/** One label/value pair in the run summary. */
function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-1 text-sm text-slate-900">{value || "—"}</dd>
    </div>
  );
}

export default async function SubmissionRunPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const run = await fetchSubmissionRun(runId);
  if (run === null) {
    notFound();
  }

  return (
    <section>
      <a
        href="/dashboard/submissions"
        className="text-sm text-slate-500 underline-offset-2 hover:underline"
      >
        ← Submissions
      </a>
      <h1 className="mt-2 text-xl font-semibold text-slate-900">
        Submission run · {run.reporting_year}
      </h1>
      <p className="mt-1 text-sm text-slate-500">
        Last run {new Date(run.updated_at).toLocaleString()}
      </p>

      <dl className="mt-6 grid grid-cols-1 gap-6 rounded-lg border border-slate-200 bg-white p-6 md:grid-cols-3">
        <Detail label="Status" value={run.status} />
        <Detail label="Validation" value={run.validation_overall_status} />
        <Detail label="Register entries included" value={String(run.entry_count)} />
        <Detail label="Validation issues" value={String(run.validation_issue_count)} />
        <Detail label="Window" value={run.submission_window_id} />
        <Detail label="First recorded" value={new Date(run.created_at).toLocaleString()} />
        <Detail
          label="Submitted to authority"
          value={run.submitted_at ? new Date(run.submitted_at).toLocaleString() : "Not submitted"}
        />
        <Detail label="Submission reference" value={run.submission_reference ?? ""} />
      </dl>

      {run.entry_count === 0 && (
        <p className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          This run covered no register entries, so it cannot pass validation. A
          DORA filing needs at least one active ICT third-party relationship —
          add providers on the{" "}
          <a href="/dashboard/register" className="underline underline-offset-2">
            register
          </a>{" "}
          and run again.
        </p>
      )}
    </section>
  );
}
