/**
 * components/EvidenceUpload.tsx — drag-and-drop evidence upload (KER-407).
 *
 * What:  select or drop one document, choose its type, upload it.
 * Why:   this is the step that did not exist — a customer had no way to get a
 *        document into Kerno at all except by wiring signed webhooks.
 * How:   posts multipart to the /api/evidence proxy (the browser never calls
 *        FastAPI directly). A file over the shared size limit is refused here,
 *        before any request: the proxy closes the connection on an over-limit
 *        body (SEC-REMED-003), which a browser may report as a network error
 *        rather than a 413. Whatever happens, including a response whose body
 *        cannot be read, the Upload button is restored, and nothing is retried.
 *        Tests: npm test -- evidence-upload.
 */

"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { MAX_EVIDENCE_FILE_BYTES } from "@/lib/evidence-upload-limits";

// Mirrors config.constants.SUPPORTED_EVIDENCE_EXTENSIONS — the backend rejects
// anything else with a 422, so the picker offers only what will succeed.
const ACCEPTED_EXTENSIONS = ".txt,.md,.csv,.pdf";

const RECORD_TYPES = ["policy", "report", "runbook", "assessment", "attestation", "evidence"];

const TOO_LARGE_MESSAGE = "That file is too large.";
const INTERRUPTED_MESSAGE = "Upload failed: the connection was interrupted.";

// A success status whose body cannot be read or decoded: the document may or
// may not have been stored, so neither outcome is claimed, the form is kept,
// and nothing is sent again automatically.
const UNCONFIRMED_MESSAGE = "The upload's result could not be read, so it is unconfirmed. "
  + "Reload the evidence list before uploading this file again.";

/** The parts of a stored upload's response that the success message uses. */
interface UploadResult {
  title?: string | null;
  deduplicated?: boolean;
}

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

  /** Upload the chosen file, refusing one over the size limit before any request is sent. */
  async function handleUpload() {
    if (!file) {
      return;
    }
    if (file.size > MAX_EVIDENCE_FILE_BYTES) {
      setError(TOO_LARGE_MESSAGE);
      return;
    }
    setUploading(true);
    setError(null);
    try {
      await sendUpload(file);
    } finally {
      setUploading(false);
    }
  }

  /** Post the file once and report what came back; nothing here retries. */
  async function sendUpload(chosen: File) {
    let response: Response;
    try {
      response = await fetch("/api/evidence", { method: "POST", body: uploadForm(chosen) });
    } catch {
      setError(INTERRUPTED_MESSAGE);
      return;
    }
    if (response.ok) {
      await reportStored(response, chosen);
    } else {
      await reportRefusal(response);
    }
  }

  /** Build the form the proxy forwards: the file, its type, and the title when one was given. */
  function uploadForm(chosen: File): FormData {
    const formData = new FormData();
    formData.append("file", chosen);
    formData.append("record_type", recordType);
    if (title.trim()) {
      formData.append("title", title.trim());
    }
    return formData;
  }

  /** Confirm a stored upload and clear the form, or report it unconfirmed when its result cannot be read. */
  async function reportStored(response: Response, chosen: File) {
    let result: UploadResult;
    try {
      result = await response.json();
    } catch {
      setError(UNCONFIRMED_MESSAGE);
      return;
    }
    onUploaded?.(
      result.deduplicated
        ? `"${result.title ?? chosen.name}" was already in your evidence library.`
        : `Uploaded "${result.title ?? chosen.name}".`,
    );
    setFile(null);
    setTitle("");
    if (inputRef.current) {
      inputRef.current.value = "";
    }
    router.refresh();
  }

  /** Show why the upload was refused, using the backend's detail when it can be read. */
  async function reportRefusal(response: Response) {
    const body = await response.json().catch(() => ({}));
    setError(
      response.status === 413
        ? TOO_LARGE_MESSAGE
        : `Upload failed: ${body.detail ?? response.status}`,
    );
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
