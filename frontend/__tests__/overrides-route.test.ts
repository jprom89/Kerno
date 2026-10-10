/**
 * The override proxy forwards the reviewed recommendation's identity unchanged to exactly one backend call and relays
 * the backend's verdict, including a 409 for a replaced recommendation (SEC-REMED-005). The origin policy still refuses
 * before any body read, cookie access or backend call.
 */

import { NextRequest } from "next/server";

import { POST } from "@/app/api/overrides/route";

const cookieGet = jest.fn().mockReturnValue({ value: "synthetic-reviewer-session" });
jest.mock("next/headers", () => ({
  cookies: async () => ({ get: cookieGet }),
}));

const ORIGIN = "https://kerno.example.test";
const OVERRIDES_URL = "http://backend.test/api/v1/overrides";
const REVIEWED_RECOMMENDATION_ID = "5b0e8c1a-3d4f-4a6b-9c2d-7e8f9a0b1c2d";

const APPROVE_BODY = {
  action_type: "approve",
  original_control_id: "cid-1",
  recommendation_id: REVIEWED_RECOMMENDATION_ID,
  corrected_control_id: null,
  justification_text: null,
};

const REJECT_BODY = {
  action_type: "reject",
  original_control_id: "cid-1",
  recommendation_id: REVIEWED_RECOMMENDATION_ID,
  corrected_control_id: "cid-2",
  justification_text: "The evidence covers incident handling, not risk analysis.",
};

/** Build a cookie-bearing JSON review request (a null origin omits the header). */
function review(body: unknown, origin: string | null = ORIGIN): NextRequest {
  const headers = new Headers({
    cookie: "kerno_session=synthetic-reviewer-session",
    "content-type": "application/json",
  });
  if (origin !== null) headers.set("origin", origin);
  return new NextRequest(`${ORIGIN}/api/overrides`, { method: "POST", body: JSON.stringify(body), headers });
}

function requestedUrls(): string[] {
  return (global.fetch as jest.Mock).mock.calls.map(([url]) => url);
}

beforeEach(() => {
  jest.replaceProperty(process, "env", { ...process.env,
    KERNO_TRUSTED_ORIGINS: ORIGIN, KERNO_API_URL: "http://backend.test" });
  global.fetch = jest.fn().mockResolvedValue(Response.json({ override_id: "o-1" }, { status: 201 }));
});
afterEach(() => jest.restoreAllMocks());

describe("POST /api/overrides — a trusted review", () => {
  it.each([
    ["approve", APPROVE_BODY],
    ["reject", REJECT_BODY],
  ])("forwards the %s body unchanged in exactly one call to the overrides endpoint", async (_label, body) => {
    const response = await POST(review(body));

    expect(response.status).toBe(201);
    expect(requestedUrls()).toEqual([OVERRIDES_URL]);
    const init = (global.fetch as jest.Mock).mock.calls[0][1];
    expect(init.method).toBe("POST");
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer synthetic-reviewer-session");
    expect(headers.get("Content-Type")).toBe("application/json");
    const forwarded = JSON.parse(init.body);
    expect(forwarded).toStrictEqual(body);
    expect(forwarded.recommendation_id).toBe(REVIEWED_RECOMMENDATION_ID);
  });

  it.each([
    [201, {
      override_id: "o-1",
      action_type: "reject",
      original_control_id: "cid-1",
      recommendation_id: REVIEWED_RECOMMENDATION_ID,
      corrected_control_id: "cid-2",
      created_at: "2026-10-10T09:00:00Z",
    }],
    [401, { detail: "synthetic unauthenticated refusal" }],
    [403, { detail: "synthetic role refusal" }],
    [404, { detail: "entry not found" }],
    [409, { detail: `recommendation ${REVIEWED_RECOMMENDATION_ID} has been replaced by a newer recommendation` }],
    [422, { detail: [{ type: "missing", loc: ["body", "recommendation_id"], msg: "Field required" }] }],
  ])("relays a backend %s and its JSON body unchanged without calling anything else", async (status, body) => {
    global.fetch = jest.fn().mockResolvedValue(Response.json(body, { status }));

    const response = await POST(review(REJECT_BODY));

    expect(response.status).toBe(status);
    expect(await response.json()).toStrictEqual(body);
    expect(requestedUrls()).toEqual([OVERRIDES_URL]);
  });
});

describe("POST /api/overrides — refused before any side effect", () => {
  it.each([null, "https://evil.test"])(
    "rejects origin %s without reading the body, the cookie or the backend", async (origin) => {
      const request = review(REJECT_BODY, origin);
      const json = jest.spyOn(request, "json");

      const response = await POST(request);

      expect(response.status).toBe(403);
      expect(json).not.toHaveBeenCalled();
      expect(cookieGet).not.toHaveBeenCalled();
      expect(global.fetch).not.toHaveBeenCalled();
    },
  );

  it("answers 503 without reading the body or calling the backend when no trusted origin is configured", async () => {
    delete process.env.KERNO_TRUSTED_ORIGINS;
    const request = review(REJECT_BODY);
    const json = jest.spyOn(request, "json");

    const response = await POST(request);

    expect(response.status).toBe(503);
    expect(json).not.toHaveBeenCalled();
    expect(cookieGet).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
