/**
 * app/dashboard/submissions/[runId]/page.tsx — one submission run (KER-411).
 *
 * What:  the recorded outcome of validating the register against one window,
 *        plus a frozen-filing download for roles that may file.
 * Why:   the run is the evidence that a filing was attempted, what the register
 *        looked like at the time, and whether it passed — the thing an auditor
 *        asks to see. A missing or malformed id renders the 404 page.
 * How:   server component; data via lib/api.ts. Tests: npm test.
 */

import { notFound } from "next/navigation";

import SubmissionRunView from "@/components/SubmissionRunView";
import { fetchMe, fetchSubmissionRun } from "@/lib/api";
import { SUBMISSION_WRITE_ROLES } from "@/lib/roles";

export default async function SubmissionRunPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const [run, me] = await Promise.all([fetchSubmissionRun(runId), fetchMe()]);
  if (run === null) {
    notFound();
  }
  const canDownload = me !== null && SUBMISSION_WRITE_ROLES.includes(me.role);

  return <SubmissionRunView run={run} canDownload={canDownload} />;
}
