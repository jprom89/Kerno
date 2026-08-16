/**
 * components/RegisterList.tsx — the DORA Register of Information (KER-410).
 *
 * What:  every ICT third-party relationship on record, with criticality and
 *        contract dates, plus an add action for the roles allowed to write.
 * Why:   this register is the object Kerno exists to hold. Until now it had an
 *        API and no product surface — the only UI was the legacy dashboard,
 *        which Ticket B turned off outside development.
 * How:   rendered by app/dashboard/register/page.tsx. Tests: npm test.
 */

"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import RegisterEntryForm, { type RegisterEntryFields } from "@/components/RegisterEntryForm";
import Toast from "@/components/Toast";
import type { RegisterEntry } from "@/lib/api";

interface RegisterListProps {
  entries: RegisterEntry[];
  readOnly: boolean;
}

interface ToastState {
  message: string;
  tone: "success" | "error";
  stamp: number;
}

const CRITICALITY_STYLES: Record<string, string> = {
  critical: "bg-red-100 text-red-800",
  high: "bg-amber-100 text-amber-800",
  standard: "bg-slate-100 text-slate-700",
};

export default function RegisterList({ entries, readOnly }: RegisterListProps) {
  const router = useRouter();
  const [addOpen, setAddOpen] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);

  async function createEntry(fields: RegisterEntryFields) {
    const response = await fetch("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    });
    if (response.ok) {
      setAddOpen(false);
      setToast({
        message: `${fields.provider_name} added to the register.`,
        tone: "success",
        stamp: Date.now(),
      });
      router.refresh();
      return;
    }
    const body = await response.json().catch(() => ({}));
    setToast({
      message: `Could not add the provider (${response.status}): ${
        body.detail ?? "see backend logs"
      }`,
      tone: "error",
      stamp: Date.now(),
    });
  }

  return (
    <div>
      {!readOnly && (
        <div className="mb-4">
          {addOpen ? (
            <RegisterEntryForm
              submitLabel="Add provider"
              onSubmit={createEntry}
              onCancel={() => setAddOpen(false)}
            />
          ) : (
            <button
              type="button"
              onClick={() => setAddOpen(true)}
              className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
            >
              Add provider
            </button>
          )}
        </div>
      )}

      {entries.length === 0 ? (
        <p className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-500">
          No providers on the register yet. A DORA filing needs at least one
          active ICT third-party relationship.
        </p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-3">Provider</th>
                <th className="px-4 py-3">Service</th>
                <th className="px-4 py-3">Type</th>
                <th className="px-4 py-3">Criticality</th>
                <th className="px-4 py-3">Function</th>
                <th className="px-4 py-3">Contract ends</th>
                <th className="px-4 py-3">Status</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr
                  key={entry.register_entry_id}
                  className="border-b border-slate-100 last:border-0 hover:bg-slate-50"
                >
                  <td className="px-4 py-3">
                    <a
                      href={`/dashboard/register/${entry.register_entry_id}`}
                      className="font-medium text-slate-900 underline-offset-2 hover:underline"
                    >
                      {entry.provider_name}
                    </a>
                  </td>
                  <td className="px-4 py-3 text-slate-600">{entry.service_name}</td>
                  <td className="px-4 py-3 text-slate-600">
                    {entry.provider_type.replace(/_/g, " ")}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`rounded-full px-2 py-1 text-xs font-medium ${
                        CRITICALITY_STYLES[entry.criticality_level] ??
                        CRITICALITY_STYLES.standard
                      }`}
                    >
                      {entry.criticality_level}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-600">{entry.business_function}</td>
                  <td className="px-4 py-3 text-slate-600">
                    {entry.contract_end_date ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-slate-600">
                    {entry.is_active ? "Active" : "Inactive"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {toast && <Toast key={toast.stamp} message={toast.message} tone={toast.tone} />}
    </div>
  );
}
