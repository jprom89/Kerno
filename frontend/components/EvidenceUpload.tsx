/**
 * components/EvidenceUpload.tsx — drag-and-drop evidence upload (KER-407).
 *
 * What:  select or drop one document, choose its type, upload it.
 * Why:   this is the step that did not exist — a customer had no way to get a
 *        document into Kerno at all except by wiring signed webhooks.
 * How:   posts multipart to the /api/evidence proxy (the browser never calls
 *        FastAPI directly). Tests: npm test.
 */

"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

// Mirrors config.constants.SUPPORTED_EVIDENCE_EXTENSIONS — the backend rejects
// anything else with a 422, so the picker offers only what will succeed.
const ACCEPTED_EXTENSIONS = ".txt,.md,.csv,.pdf";

const RECORD_TYPES = ["policy", "report", "runbook", "assessment", "attestation", "evidence"];

interface EvidenceUploadProps {
  onUploaded?: (message: string) => void;
}

export default function EvidenceUpload({ onUploaded }: EvidenceUploadProps) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [recordType, setRecordType] = useState(RECORD_TYPES[0]);
  const [title, setTitle] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  async function handleUpload() {
    if (!file) {
      return;
    }
    setUploading(true);
    setError(null);
    const formData = new FormData();
    formData.append("file", file);
    formData.append("record_type", recordType);
    if (title.trim()) {
      formData.append("title", title.trim());
    }
    const response = await fetch("/api/evidence", { method: "POST", body: formData });
    if (response.ok) {
      const result = await response.json();
      onUploaded?.(
        result.deduplicated
          ? `"${result.title ?? file.name}" was already in your evidence library.`
          : `Uploaded "${result.title ?? file.name}".`,
      );
      setFile(null);
      setTitle("");
      if (inputRef.current) {
        inputRef.current.value = "";
      }
      router.refresh();
    } else {
      const body = await response.json().catch(() => ({}));
      setError(
        response.status === 413
          ? "That file is too large."
          : `Upload failed: ${body.detail ?? response.status}`,
      );
    }
    setUploading(false);
  }

  return (
    <section className="mb-8 rounded-lg border border-slate-200 bg-white p-4">
      <h2 className="mb-3 text-sm font-semibold text-slate-900">Add evidence</h2>
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const dropped = event.dataTransfer.files?.[0];
          if (dropped) {
            setFile(dropped);
          }
        }}
        className={`mb-3 rounded border-2 border-dashed p-6 text-center text-sm ${
          dragging ? "border-slate-500 bg-slate-50" : "border-slate-300"
        }`}
      >
        <p className="mb-2 text-slate-600">
          {file ? `Selected: ${file.name}` : "Drop a document here, or choose a file"}
        </p>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPTED_EXTENSIONS}
          aria-label="Evidence file"
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          className="text-sm text-slate-700"
        />
        <p className="mt-2 text-xs text-slate-500">PDF, plain text, Markdown, or CSV.</p>
      </div>

      <div className="mb-3 flex flex-wrap gap-3">
        <label className="text-sm">
          <span className="mr-2 text-slate-600">Type</span>
          <select
            value={recordType}
            onChange={(event) => setRecordType(event.target.value)}
            aria-label="Evidence type"
            className="rounded border border-slate-300 px-2 py-1"
          >
            {RECORD_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </select>
        </label>
        <label className="flex-1 text-sm">
          <span className="mr-2 text-slate-600">Title</span>
          <input
            type="text"
            value={title}
            placeholder="optional — defaults to the filename"
            onChange={(event) => setTitle(event.target.value)}
            aria-label="Evidence title"
            className="w-full max-w-sm rounded border border-slate-300 px-2 py-1"
          />
        </label>
      </div>

      {error && (
        <p role="alert" className="mb-2 text-sm text-red-700">
          {error}
        </p>
      )}
      <button
        type="button"
        onClick={handleUpload}
        disabled={!file || uploading}
        className="rounded bg-slate-900 px-4 py-1.5 text-sm font-medium text-white disabled:opacity-40"
      >
        {uploading ? "Uploading…" : "Upload"}
      </button>
    </section>
  );
}
