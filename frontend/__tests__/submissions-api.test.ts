/**
 * __tests__/submissions-api.test.ts — the server fetchers for submissions (KER-411).
 *
 * What:  fetchSubmissionRun returns null for a run that is not there, so the
 *        page can render Next's 404 instead of throwing.
 * Why:   run ids arrive from URLs people edit and bookmarks that go stale. The
 *        backend answers 404 for a well-formed id that does not exist, but 500
 *        for a malformed one — run_id reaches SQL as a plain string and fails
 *        the uuid cast — and both mean the same thing to whoever followed the
 *        link. This runs in the node environment because lib/api.ts is server
 *        code: it reads the httpOnly cookie via next/headers.
 * How:   npm test
 */

const cookieGet = jest.fn().mockReturnValue({ value: "test-jwt" });
jest.mock("next/headers", () => ({
  cookies: async () => ({ get: cookieGet }),
}));

import { fetchSubmissionRun, fetchSubmissionRuns } from "@/lib/api";

const ORIGINAL_API_URL = process.env.KERNO_API_URL;

beforeAll(() => {
  process.env.KERNO_API_URL = "http://backend.test";
});

afterAll(() => {
  process.env.KERNO_API_URL = ORIGINAL_API_URL;
});

describe("fetchSubmissionRun", () => {
  it("returns null when the run does not exist", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: "entry not found" }),
    } as Response);

    await expect(fetchSubmissionRun("11111111-1111-4111-8111-111111111111")).resolves.toBeNull();
  });

  it("returns null for a malformed id, which the backend 404s", async () => {
    // KER-412: an id that cannot be a UUID is rejected before it reaches the
    // database, so it arrives here as an ordinary 404 like any missing run.
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: "entry not found" }),
    } as Response);

    await expect(fetchSubmissionRun("not-a-uuid")).resolves.toBeNull();
  });

  it("still throws on a genuine server error rather than hiding it as not-found", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 500,
      json: async () => ({ detail: "internal server error" }),
    } as Response);

    await expect(fetchSubmissionRun("run-1")).rejects.toThrow("submission run failed: 500");
  });

  it("returns the run and sends the session token when it exists", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: "run-1", reporting_year: 2031 }),
    } as Response);

    const run = await fetchSubmissionRun("run-1");
    expect(run?.id).toBe("run-1");
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("http://backend.test/api/v1/submissions/runs/run-1");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer test-jwt");
  });

  it("throws on an unexpected failure rather than pretending the run is absent", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({}),
    } as Response);

    await expect(fetchSubmissionRun("run-1")).rejects.toThrow("submission run failed: 401");
  });
});

describe("fetchSubmissionRuns", () => {
  it("returns the history list", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => [{ id: "run-1" }, { id: "run-2" }],
    } as Response);

    await expect(fetchSubmissionRuns()).resolves.toHaveLength(2);
  });
});
