/**
 * lib/api.ts — the single typed gateway from the Next.js SERVER to FastAPI.
 *
 * What:  apiFetch() attaches the session JWT (from the httpOnly cookie) as a
 *        Bearer header and calls the FastAPI backend; fetchMe() is the
 *        per-page-load session check (KER-301 AC-4).
 * Why:   one wrapper so every story calls FastAPI the same way — and only
 *        from the server. KERNO_API_URL is a server-side env var, never
 *        NEXT_PUBLIC_*: the browser must never call FastAPI directly; all
 *        calls go through Next.js route handlers and server components,
 *        which hold the httpOnly cookie (decided 15 July 2026).
 * How:   import { apiFetch, fetchMe } from "@/lib/api" in server code only.
 *        Tests: npm test (frontend/__tests__/).
 */

import { cookies } from "next/headers";

/** Name of the httpOnly cookie carrying the FastAPI JWT. */
export const SESSION_COOKIE = "kerno_session";

/** The logged-in identity as returned by GET /api/v1/auth/me — display strings only. */
export interface Me {
  email: string;
  role: string;
}

/** Return the FastAPI base URL from the server-side environment, without a trailing slash. */
export function apiBaseUrl(): string {
  const url = process.env.KERNO_API_URL;
  if (!url) {
    throw new Error("KERNO_API_URL is not set (server-side env var — see .env.example)");
  }
  return url.replace(/\/+$/, "");
}

/**
 * Call a FastAPI path with the session JWT from the httpOnly cookie.
 * Server-only: uses next/headers, which does not exist in the browser bundle.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const cookieStore = await cookies();
  const token = cookieStore.get(SESSION_COOKIE)?.value;
  const headers = new Headers(init.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(`${apiBaseUrl()}${path}`, { ...init, headers, cache: "no-store" });
}

/**
 * Validate the current session against GET /api/v1/auth/me.
 * Returns the identity display strings, or null for any failure — the caller
 * (the dashboard layout) redirects to /login on null.
 */
export async function fetchMe(): Promise<Me | null> {
  try {
    const response = await apiFetch("/api/v1/auth/me");
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as Me;
  } catch {
    return null;
  }
}

/** One NIS2 category's met/partial/gap counts (KER-109 shape). */
export interface CategoryCoverage {
  category: string;
  met: number;
  partial: number;
  gap: number;
  total: number;
}

/** The tenant-wide coverage summary from GET /api/v1/coverage/summary (KER-302). */
export interface CoverageSummary {
  total_controls: number;
  met: number;
  partial: number;
  gap: number;
  categories: CategoryCoverage[];
  last_recalculated_at: string | null;
}

/** One control row from GET /api/v1/coverage/controls (KER-109 system of record). */
export interface CoverageControl {
  control_id: string;
  control_ref: string;
  title: string;
  category: string;
  framework: string;
  status: string;
  status_source: string;
  human_confirmed: boolean;
  confidence_level: string | null;
  confidence_score: number | null;
  evidence_count: number;
}

/** Fetch the coverage summary; throws on a non-OK response (layout guarantees auth). */
export async function fetchCoverageSummary(): Promise<CoverageSummary> {
  const response = await apiFetch("/api/v1/coverage/summary");
  if (!response.ok) {
    throw new Error(`coverage summary failed: ${response.status}`);
  }
  return (await response.json()) as CoverageSummary;
}

/** Fetch the control list, optionally filtered to one NIS2 category. */
export async function fetchCoverageControls(category?: string): Promise<CoverageControl[]> {
  const query = category ? `?category=${encodeURIComponent(category)}` : "";
  const response = await apiFetch(`/api/v1/coverage/controls${query}`);
  if (!response.ok) {
    throw new Error(`coverage controls failed: ${response.status}`);
  }
  return (await response.json()) as CoverageControl[];
}

/** One open recommendation from GET /api/v1/recommendations (KER-303). */
export interface OpenRecommendation {
  recommendation_id: string;
  control_id: string;
  control_ref: string | null;
  control_title: string | null;
  category: string | null;
  status: string;
  confidence_level: string;
  confidence_score: number;
  rationale: string;
  evidence_count: number;
  generated_at: string;
}

/** The paginated review queue response shape. */
export interface RecommendationPage {
  items: OpenRecommendation[];
  total: number;
  page: number;
  page_size: number;
}

/** Fetch one page of the open-recommendation review queue. */
export async function fetchOpenRecommendations(page = 1): Promise<RecommendationPage> {
  const response = await apiFetch(`/api/v1/recommendations?page=${page}`);
  if (!response.ok) {
    throw new Error(`recommendations failed: ${response.status}`);
  }
  return (await response.json()) as RecommendationPage;
}

/** One evidence record in the tenant's library (KER-406). */
export interface EvidenceRecord {
  record_id: string;
  source_system: string;
  external_id: string | null;
  record_type: string;
  title: string | null;
  created_at: string;
  link_count: number;
}

/** The evidence library response shape. */
export interface EvidencePage {
  items: EvidenceRecord[];
  total: number;
}

/**
 * Fetch the tenant's evidence library, optionally filtered by link status.
 * `linked=false` surfaces orphans — records nothing has been linked to yet.
 */
export async function fetchEvidence(linked?: boolean): Promise<EvidencePage> {
  const query = linked === undefined ? "" : `?linked=${linked}`;
  const response = await apiFetch(`/api/v1/evidence${query}`);
  if (!response.ok) {
    throw new Error(`evidence list failed: ${response.status}`);
  }
  return (await response.json()) as EvidencePage;
}

/**
 * One filing window from GET /api/v1/submissions/windows (KER-411).
 *
 * Note the bare `id` — submission windows and runs use it, while register
 * entries use `register_entry_id` and reporting windows use
 * `reporting_window_id`. Copying a register key pattern here yields undefined.
 *
 * This is global reference data: no tenant_id, and the backend route carries no
 * authentication at all. It is still fetched through the same cookie-bearing
 * proxy as everything else so the page stays behind the dashboard layout.
 */
export interface SubmissionWindow {
  id: string;
  authority_code: string;
  reporting_year: number;
  register_reference_date: string;
  window_open_date: string;
  window_close_date: string;
  created_at: string;
  updated_at: string;
}

/** One submission run from the /api/v1/submissions/runs endpoints (KER-411). */
export interface SubmissionRun {
  id: string;
  tenant_id: string;
  submission_window_id: string;
  reporting_year: number;
  status: string;
  validation_overall_status: string;
  validation_issue_count: number;
  entry_count: number;
  created_at: string;
  updated_at: string;
  submitted_at: string | null;
  submission_reference: string | null;
}

/**
 * Fetch the filing windows that are open today.
 *
 * "Open" is a date range on the server — window_open_date <= today <=
 * window_close_date, inclusive — with no status column behind it. A closed
 * window is simply absent rather than listed as closed, so an empty array means
 * "none open today", which is not the same as "none configured".
 */
export async function fetchOpenWindows(): Promise<SubmissionWindow[]> {
  const response = await apiFetch("/api/v1/submissions/windows");
  if (!response.ok) {
    throw new Error(`submission windows failed: ${response.status}`);
  }
  return (await response.json()) as SubmissionWindow[];
}

/** Fetch the tenant's submission runs, newest reporting year and run first. */
export async function fetchSubmissionRuns(): Promise<SubmissionRun[]> {
  const response = await apiFetch("/api/v1/submissions/runs");
  if (!response.ok) {
    throw new Error(`submission runs failed: ${response.status}`);
  }
  return (await response.json()) as SubmissionRun[];
}

/**
 * Fetch one submission run, or null when it does not exist for this tenant.
 *
 * Treats a 500 as not-found too, deliberately: run_id reaches SQL as a plain
 * string, so a malformed id fails the uuid cast and surfaces as a 500 rather
 * than a 404. Both mean "no such run" to someone following a bad link, and
 * rendering the 404 page beats an error page with a correlation ID.
 */
export async function fetchSubmissionRun(runId: string): Promise<SubmissionRun | null> {
  const response = await apiFetch(`/api/v1/submissions/runs/${encodeURIComponent(runId)}`);
  if (response.status === 404 || response.status === 500) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`submission run failed: ${response.status}`);
  }
  return (await response.json()) as SubmissionRun;
}

/** One ICT third-party provider line in the DORA Register of Information (KER-410). */
export interface RegisterEntry {
  register_entry_id: string;
  tenant_id: string;
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
  created_at: string;
  updated_at: string;
}

/** Fetch every active register entry for the tenant. */
export async function fetchRegisterEntries(): Promise<RegisterEntry[]> {
  const response = await apiFetch("/api/v1/register/entries");
  if (!response.ok) {
    throw new Error(`register entries failed: ${response.status}`);
  }
  return (await response.json()) as RegisterEntry[];
}

/**
 * Fetch one register entry, or null when the backend reports it does not exist.
 * A missing entry is a 404 page, not a crash — the id comes from a URL a user
 * can edit or a stale bookmark.
 */
export async function fetchRegisterEntry(entryId: string): Promise<RegisterEntry | null> {
  const response = await apiFetch(`/api/v1/register/entries/${encodeURIComponent(entryId)}`);
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`register entry failed: ${response.status}`);
  }
  return (await response.json()) as RegisterEntry;
}
