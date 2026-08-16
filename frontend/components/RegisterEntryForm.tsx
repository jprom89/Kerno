/**
 * components/RegisterEntryForm.tsx — add or amend one register line (KER-410).
 *
 * What:  the twelve DORA Register of Information fields, used for both create
 *        and edit. The caller supplies the submit behaviour and, when editing,
 *        the current values.
 * Why:   one form for both paths so a field added to the register cannot end up
 *        on the create screen and missing from the amend screen.
 * How:   rendered by RegisterList (create) and RegisterEntryDetail (edit).
 *        Tests: npm test.
 */

"use client";

import { useState } from "react";

import type { RegisterEntry } from "@/lib/api";

// Mirrors src/models/dora_register_entry.py — the backend rejects anything else
// with a 422, so these are the values, not a display convenience.
const PROVIDER_TYPES = ["cloud", "software", "managed_service", "telecom", "other"];
const CRITICALITY_LEVELS = ["critical", "high", "standard"];

// config/constants.py MAX_EXIT_SUMMARY_LENGTH — the service truncates past this.
const MAX_EXIT_SUMMARY_LENGTH = 1000;

export interface RegisterEntryFields {
  provider_name: string;
  service_name: string;
  provider_type: string;
  criticality_level: string;
  business_function: string;
  data_types: string[];
  countries_supported: string[];
  contract_start_date: string | null;
  contract_end_date: string | null;
  exit_strategy_summary: string | null;
  is_active: boolean;
  source_record_id: string | null;
}

interface RegisterEntryFormProps {
  entry?: RegisterEntry;
  submitLabel: string;
  onSubmit: (fields: RegisterEntryFields) => Promise<void>;
  onCancel?: () => void;
}

/** Split a comma-separated input into trimmed, non-empty values. */
function toList(raw: string): string[] {
  return raw
    .split(",")
    .map((value) => value.trim())
    .filter((value) => value.length > 0);
}

const FIELD_CLASS =
  "w-full rounded border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none";
const LABEL_CLASS = "mb-1 block text-sm font-medium text-slate-700";

export default function RegisterEntryForm({
  entry,
  submitLabel,
  onSubmit,
  onCancel,
}: RegisterEntryFormProps) {
  const [providerName, setProviderName] = useState(entry?.provider_name ?? "");
  const [serviceName, setServiceName] = useState(entry?.service_name ?? "");
  const [providerType, setProviderType] = useState(entry?.provider_type ?? PROVIDER_TYPES[0]);
  const [criticality, setCriticality] = useState(entry?.criticality_level ?? CRITICALITY_LEVELS[0]);
  const [businessFunction, setBusinessFunction] = useState(entry?.business_function ?? "");
  const [dataTypes, setDataTypes] = useState((entry?.data_types ?? []).join(", "));
  const [countries, setCountries] = useState((entry?.countries_supported ?? []).join(", "));
  const [startDate, setStartDate] = useState(entry?.contract_start_date ?? "");
  const [endDate, setEndDate] = useState(entry?.contract_end_date ?? "");
  const [exitSummary, setExitSummary] = useState(entry?.exit_strategy_summary ?? "");
  const [isActive, setIsActive] = useState(entry?.is_active ?? true);
  const [sourceRecordId, setSourceRecordId] = useState(entry?.source_record_id ?? "");
  const [submitting, setSubmitting] = useState(false);

  const canSubmit =
    providerName.trim().length > 0 &&
    serviceName.trim().length > 0 &&
    businessFunction.trim().length > 0 &&
    toList(dataTypes).length > 0 &&
    toList(countries).length > 0;

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    try {
      await onSubmit({
        provider_name: providerName.trim(),
        service_name: serviceName.trim(),
        provider_type: providerType,
        criticality_level: criticality,
        business_function: businessFunction.trim(),
        data_types: toList(dataTypes),
        countries_supported: toList(countries),
        contract_start_date: startDate || null,
        contract_end_date: endDate || null,
        exit_strategy_summary: exitSummary.trim() || null,
        is_active: isActive,
        source_record_id: sourceRecordId.trim() || null,
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-lg border border-slate-200 bg-white p-6"
      aria-label={submitLabel}
    >
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <label className="block">
          <span className={LABEL_CLASS}>Provider name</span>
          <input
            className={FIELD_CLASS}
            value={providerName}
            onChange={(event) => setProviderName(event.target.value)}
            required
          />
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Service name</span>
          <input
            className={FIELD_CLASS}
            value={serviceName}
            onChange={(event) => setServiceName(event.target.value)}
            required
          />
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Provider type</span>
          <select
            className={FIELD_CLASS}
            value={providerType}
            onChange={(event) => setProviderType(event.target.value)}
          >
            {PROVIDER_TYPES.map((value) => (
              <option key={value} value={value}>
                {value.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Criticality</span>
          <select
            className={FIELD_CLASS}
            value={criticality}
            onChange={(event) => setCriticality(event.target.value)}
          >
            {CRITICALITY_LEVELS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
        <label className="block md:col-span-2">
          <span className={LABEL_CLASS}>Business function supported</span>
          <input
            className={FIELD_CLASS}
            value={businessFunction}
            onChange={(event) => setBusinessFunction(event.target.value)}
            required
          />
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Data types (comma separated)</span>
          <input
            className={FIELD_CLASS}
            value={dataTypes}
            placeholder="operational, personal"
            onChange={(event) => setDataTypes(event.target.value)}
          />
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Countries supported (comma separated)</span>
          <input
            className={FIELD_CLASS}
            value={countries}
            placeholder="DE, FR"
            onChange={(event) => setCountries(event.target.value)}
          />
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Contract start</span>
          <input
            type="date"
            className={FIELD_CLASS}
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
          />
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Contract end</span>
          <input
            type="date"
            className={FIELD_CLASS}
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
          />
        </label>
        <label className="block md:col-span-2">
          <span className={LABEL_CLASS}>Exit strategy summary</span>
          <textarea
            className={FIELD_CLASS}
            rows={3}
            maxLength={MAX_EXIT_SUMMARY_LENGTH}
            value={exitSummary}
            onChange={(event) => setExitSummary(event.target.value)}
          />
        </label>
        <label className="block">
          <span className={LABEL_CLASS}>Source record ID (optional)</span>
          <input
            className={FIELD_CLASS}
            value={sourceRecordId}
            onChange={(event) => setSourceRecordId(event.target.value)}
          />
        </label>
        <label className="flex items-center gap-2 self-end pb-2">
          <input
            type="checkbox"
            checked={isActive}
            onChange={(event) => setIsActive(event.target.checked)}
            className="h-4 w-4 rounded border-slate-300"
          />
          <span className="text-sm text-slate-700">Active relationship</span>
        </label>
      </div>

      <div className="mt-6 flex items-center gap-3">
        <button
          type="submit"
          disabled={!canSubmit || submitting}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {submitting ? "Saving…" : submitLabel}
        </button>
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            className="rounded border border-slate-300 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50"
          >
            Cancel
          </button>
        )}
      </div>
    </form>
  );
}
