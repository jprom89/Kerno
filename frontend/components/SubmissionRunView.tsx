/**
 * components/SubmissionRunView.tsx — one recorded DORA filing attempt.
 *
 * What:  the run summary (status, validation, counts, window) and, for roles
 *        that may file, the frozen-package download. An auditor sees the
 *        record and no download button.
 * Why:   the run is the evidence that a filing was attempted and what the
 *        register looked like at the time. The download is that freeze, not a
 *        live rebuild. Hiding the button is UX; the 403 is the guarantee.
 * How:   rendered by the run page. Tests: npm test.
 */

import FilingDownloadButton from "@/components/FilingDownloadButton";
import type { SubmissionRun } from "@/lib/api";

interface SubmissionRunViewProps {
  run: SubmissionRun;
  canDownload: boolean;
}

/** One label/value pair in the run summary. */
function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-1 text-sm text-slate-900">{value || "—"}</dd>
    </div>
  );
}

export default function SubmissionRunView({ run, canDownload }: SubmissionRunViewProps) {
  return (
    <section>
      <a
        href="/dashboard/submissions"
        className="text-sm text-slate-500 underline-offset-2 hover:underline"
      >
        ← Submissions
      </a>
      <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold text-slate-900">
          Submission run · {run.reporting_year}
        </h1>
        {canDownload && <FilingDownloadButton runId={run.id} />}
      </div>
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
