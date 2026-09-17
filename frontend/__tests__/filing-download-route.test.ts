/**
 * __tests__/filing-download-route.test.ts — the Next.js proxy for the frozen filing.
 *
 * What:  GET relays the FastAPI attachment bytes and Content-Disposition;
 *        403 and 404 pass through rather than becoming a fake success.
 * Why:   the browser never calls FastAPI; this route is the only path the
 *        download button has. A flattened 200 would look like a filing.
 * How:   npm test
 */

import { NextRequest } from "next/server";

import { GET } from "@/app/api/submissions/runs/[runId]/package/route";

const cookieGet = jest.fn().mockReturnValue({ value: "test-jwt" });
jest.mock("next/headers", () => ({
  cookies: async () => ({ get: cookieGet }),
}));

const ORIGINAL_API_URL = process.env.KERNO_API_URL;

beforeAll(() => {
  process.env.KERNO_API_URL = "http://backend.test";
});

afterAll(() => {
  process.env.KERNO_API_URL = ORIGINAL_API_URL;
});

function request(): NextRequest {
  return new NextRequest("http://localhost:3000/api/submissions/runs/run-1/package");
}

const params = { params: Promise.resolve({ runId: "run-1" }) };

describe("GET /api/submissions/runs/[runId]/package", () => {
  it("relays the attachment bytes and Content-Disposition", async () => {
    global.fetch = jest.fn().mockResolvedValue(
      new Response('{"frozen":true}', {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Content-Disposition": 'attachment; filename="kerno-dora-filing-2032-run-1.json"',
        },
      }),
    );

    const response = await GET(request(), params);
    expect(response.status).toBe(200);
    expect(response.headers.get("Content-Disposition")).toContain(
      "kerno-dora-filing-2032-run-1.json",
    );
    await expect(response.text()).resolves.toBe('{"frozen":true}');
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("http://backend.test/api/v1/submissions/runs/run-1/package");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer test-jwt");
  });

  it("relays a 404 rather than inventing a file", async () => {
    global.fetch = jest.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "entry not found" }), { status: 404 }),
    );
    const response = await GET(request(), params);
    expect(response.status).toBe(404);
  });

  it("relays a 403 rather than inventing a file", async () => {
    global.fetch = jest.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "your role is not permitted to perform this action" }), {
        status: 403,
      }),
    );
    const response = await GET(request(), params);
    expect(response.status).toBe(403);
  });
});
