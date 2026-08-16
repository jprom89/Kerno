/**
 * components/RegisterEntryDetail.tsx — one register line, read or amend (KER-410).
 *
 * What:  the full record for one ICT third-party relationship, with an edit
 *        mode for the roles allowed to write.
 * Why:   read is the default because most people looking at a filed register
 *        line are checking it, not changing it. Amending is a deliberate act:
 *        the backend records the previous values in the KER-107 ledger.
 * How:   rendered by app/dashboard/register/[entryId]/page.tsx. Tests: npm test.
 */

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import RegisterEntryForm, { type RegisterEntryFields } from "@/components/RegisterEntryForm";
import Toast from "@/components/Toast";
import type { RegisterEntry } from "@/lib/api";

interface RegisterEntryDetailProps {
  entry: RegisterEntry;
  readOnly: boolean;
}

interface ToastState {
  message: string;
  tone: "success" | "error";
  stamp: number;
}

/** One label/value row in the read view. */
function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-1 text-sm text-slate-900">{value || "—"}</dd>
    </div>
  );
}

export default function RegisterEntryDetail({ entry, readOnly }: RegisterEntryDetailProps) {
  const router = useRouter();
  const [editing, setEditing] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);

  async function saveEntry(fields: RegisterEntryFields) {
    const response = await fetch(`/api/register/${entry.register_entry_id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (response.ok) {
      setEditing(false);
      setToast({ message: "Register entry amended.", tone: "success", stamp: Date.now() });
      router.refresh();
      return;
    }
    const body = await response.json().catch(() => ({}));
    setToast({
      message: `Could not amend the entry (${response.status}): ${
        body.detail ?? "see backend logs"
      }`,
      tone: "error",
      stamp: Date.now(),
    });
  }

  return (
    <section>
      <div className="mb-6 flex items-start justify-between">
        <div>
          <a
            href="/dashboard/register"
            className="text-sm text-slate-500 underline-offset-2 hover:underline"
          >
            ← Register
          </a>
          <h1 className="mt-2 text-xl font-semibold text-slate-900">{entry.provider_name}</h1>
          <p className="mt-1 text-sm text-slate-500">
            {entry.service_name} · last amended {new Date(entry.updated_at).toLocaleString()}
          </p>
        </div>
        {!readOnly && !editing && (
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="rounded border border-slate-300 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50"
          >
            Amend
          </button>
        )}
      </div>

      {editing ? (
        <RegisterEntryForm
          entry={entry}
          submitLabel="Save amendment"
          onSubmit={saveEntry}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <dl className="grid grid-cols-1 gap-6 rounded-lg border border-slate-200 bg-white p-6 md:grid-cols-3">
          <Detail label="Provider type" value={entry.provider_type.replace(/_/g, " ")} />
          <Detail label="Criticality" value={entry.criticality_level} />
          <Detail label="Status" value={entry.is_active ? "Active" : "Inactive"} />
          <Detail label="Business function" value={entry.business_function} />
          <Detail label="Data types" value={entry.data_types.join(", ")} />
          <Detail label="Countries supported" value={entry.countries_supported.join(", ")} />
          <Detail label="Contract start" value={entry.contract_start_date ?? ""} />
          <Detail label="Contract end" value={entry.contract_end_date ?? ""} />
          <Detail label="Source record ID" value={entry.source_record_id ?? ""} />
          <div className="md:col-span-3">
            <dt className="text-xs uppercase tracking-wide text-slate-500">
              Exit strategy summary
            </dt>
            <dd className="mt-1 whitespace-pre-wrap text-sm text-slate-900">
              {entry.exit_strategy_summary || "—"}
            </dd>
          </div>
        </dl>
      )}

      {toast && <Toast key={toast.stamp} message={toast.message} tone={toast.tone} />}
    </section>
  );
}
