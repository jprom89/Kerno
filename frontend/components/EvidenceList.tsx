/**
 * components/EvidenceList.tsx — the evidence library with link management (KER-407).
 *
 * What:  every evidence record with its link count, a filter for orphans, and
 *        a link action per record. Read-only for auditors.
 * Why:   ?linked=false is the answer to "what did we ingest that nobody has
 *        connected to a control?" — the question that had no surface at all
 *        before KER-406, and the reason webhook evidence could sit invisible.
 * How:   rendered by app/dashboard/evidence/page.tsx. Tests: npm test.
 */

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import EvidenceUpload from "@/components/EvidenceUpload";
import LinkEvidenceForm, { type ControlOption } from "@/components/LinkEvidenceForm";
import Toast from "@/components/Toast";
import type { EvidenceRecord } from "@/lib/api";

type LinkFilter = "all" | "linked" | "unlinked";

interface EvidenceListProps {
  records: EvidenceRecord[];
  controls: ControlOption[];
  readOnly: boolean;
}

interface ToastState {
  message: string;
  tone: "success" | "error";
  stamp: number;
}

export default function EvidenceList({ records, controls, readOnly }: EvidenceListProps) {
  const router = useRouter();
  const [filter, setFilter] = useState<LinkFilter>("all");
  const [openFormFor, setOpenFormFor] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);

  const visible = records.filter((record) =>
    filter === "all"
      ? true
      : filter === "linked"
        ? record.link_count > 0
        : record.link_count === 0,
  );
  const orphanCount = records.filter((record) => record.link_count === 0).length;

  async function submitLink(
    record: EvidenceRecord,
    controlId: string,
    relevanceScore: number,
    note: string,
  ) {
    setSubmitting(true);
    const response = await fetch(`/api/evidence/${record.record_id}/links`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        control_id: controlId,
        relevance_score: relevanceScore,
        note: note || null,
      }),
    });
    if (response.ok) {
      const control = controls.find((c) => c.control_id === controlId);
      setToast({
        message: `Linked "${record.title ?? record.external_id}" to ${control?.control_ref ?? "the control"}.`,
        tone: "success",
        stamp: Date.now(),
      });
      setOpenFormFor(null);
      router.refresh();
    } else {
      const body = await response.json().catch(() => ({}));
      setToast({
        message: `Link failed (${response.status}): ${body.detail ?? "see backend logs"}`,
        tone: "error",
        stamp: Date.now(),
      });
    }
    setSubmitting(false);
  }

  return (
    <div>
      {!readOnly && (
        <EvidenceUpload
          onUploaded={(message) => setToast({ message, tone: "success", stamp: Date.now() })}
        />
      )}

      <div className="mb-4 flex items-center gap-3 text-sm">
        <label className="flex items-center gap-2">
          <span className="text-slate-600">Show</span>
          <select
            value={filter}
            onChange={(event) => setFilter(event.target.value as LinkFilter)}
            aria-label="Filter by link status"
            className="rounded border border-slate-300 px-2 py-1"
          >
            <option value="all">all evidence</option>
            <option value="linked">linked</option>
            <option value="unlinked">not linked to any control</option>
          </select>
        </label>
        {orphanCount > 0 && (
          <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800">
            {orphanCount} not linked to any control
          </span>
        )}
      </div>

      {records.length === 0 ? (
        <p className="rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
          No evidence yet. Upload a policy, report, or export to get started — the
          engine can only assess controls that have evidence linked to them.
        </p>
      ) : (
        <ul className="space-y-3">
          {visible.map((record) => (
            <li
              key={record.record_id}
              className="rounded-lg border border-slate-200 bg-white p-4"
              data-testid={`evidence-${record.record_id}`}
            >
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-sm font-medium text-slate-900">
                    {record.title ?? record.external_id ?? "(untitled)"}
                  </p>
                  <p className="mt-1 text-xs text-slate-500">
                    {record.record_type} · {record.source_system} ·{" "}
                    {new Date(record.created_at).toLocaleDateString("en-GB")}
                  </p>
                </div>
                <div className="flex items-center gap-3">
                  <span
                    className={`rounded px-2 py-0.5 text-xs font-medium ${
                      record.link_count > 0
                        ? "bg-green-100 text-green-800"
                        : "bg-amber-100 text-amber-800"
                    }`}
                  >
                    {record.link_count > 0
                      ? `${record.link_count} control${record.link_count === 1 ? "" : "s"}`
                      : "not linked"}
                  </span>
                  {!readOnly && (
                    <button
                      type="button"
                      onClick={() =>
                        setOpenFormFor(
                          openFormFor === record.record_id ? null : record.record_id,
                        )
                      }
                      className="rounded border border-slate-300 px-3 py-1 text-xs text-slate-700"
                    >
                      Link to control
                    </button>
                  )}
                </div>
              </div>
              {openFormFor === record.record_id && (
                <LinkEvidenceForm
                  controls={controls}
                  submitting={submitting}
                  onSubmit={(controlId, score, note) =>
                    submitLink(record, controlId, score, note)
                  }
                  onCancel={() => setOpenFormFor(null)}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      {records.length > 0 && visible.length === 0 && (
        <p className="mt-4 text-sm text-slate-600">No evidence matches this filter.</p>
      )}
      {toast && <Toast key={toast.stamp} message={toast.message} tone={toast.tone} />}
    </div>
  );
}
