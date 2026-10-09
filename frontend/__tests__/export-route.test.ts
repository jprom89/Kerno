/**
 * __tests__/export-route.test.ts — the evidence-pack proxy's Fetch Metadata gate (SEC-REMED-002, finding 2.2).
 *
 * What:  only a request whose Sec-Fetch-Site is exactly "same-origin" reaches
 *        the session cookie and the backend; cross-site, same-site, none,
 *        missing, differently-cased, list-valued and unknown values are 403
 *        with no cookie access, no backend call and no attachment. A
 *        same-origin fetch keeps the body, status and Content-Disposition
 *        contract, and every response is no-store and varies on the header.
 * Why:   the backend writes an export_generated ledger entry per call, and a
 *        cross-site navigation carries the Lax cookie; Origin is absent on GET.
 * How:   npm test -- export-route; real handler, mocked cookies and fetch.
 */

import { NextRequest } from "next/server";

import { GET } from "@/app/api/export/route";

const cookieGet = jest.fn().mockReturnValue({ value: "synthetic-victim-session" });
jest.mock("next/headers", () => ({
  cookies: async () => ({ get: cookieGet }),
}));

const EXPORT_URL = "https://kerno.example.test/api/export?control_family=governance";
const ATTACHMENT = 'attachment; filename="kerno-evidence-pack-governance-2026-10-09.json"';

/** Build an export request with the given Sec-Fetch-Site value (null omits the header). */
function request(fetchSite: string | null): NextRequest {
  const headers = new Headers({ cookie: "kerno_session=synthetic-victim-session" });
  if (fetchSite !== null) headers.set("sec-fetch-site", fetchSite);
  return new NextRequest(EXPORT_URL, { headers });
}

function backendPack(status = 200, body = '{"pack":true}'): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "application/json", "Content-Disposition": ATTACHMENT },
  });
}

beforeEach(() => {
  jest.replaceProperty(process, "env", { ...process.env, KERNO_API_URL: "http://backend.test" });
  global.fetch = jest.fn().mockResolvedValue(backendPack());
});
afterEach(() => jest.restoreAllMocks());

describe("GET /api/export — refused before any cookie read or backend call", () => {
  it.each([
    ["cross-site", "cross-site"],
    ["same-site", "same-site"],
    ["none (address bar, bookmark, external link)", "none"],
    ["missing header", null],
    ["differently cased token", "Same-Origin"],
    ["list value", "same-origin, cross-site"],
    ["unknown value", "elsewhere"],
    ["empty value", ""],
  ])("rejects %s", async (_label, fetchSite) => {
    const response = await GET(request(fetchSite));
    expect(response.status).toBe(403);
    expect(cookieGet).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
    expect(response.headers.get("content-disposition")).toBeNull();
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("vary")).toBe("Sec-Fetch-Site");
    expect(await response.json()).toEqual({ detail: "a same-origin dashboard request is required" });
  });
});

describe("GET /api/export — a same-origin fetch keeps its contract", () => {
  it("relays the attachment bytes, Content-Disposition and status with the session bearer", async () => {
    const response = await GET(request("same-origin"));
    expect(response.status).toBe(200);
    expect(response.headers.get("content-disposition")).toBe(ATTACHMENT);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("vary")).toBe("Sec-Fetch-Site");
    await expect(response.text()).resolves.toBe('{"pack":true}');
    expect(cookieGet).toHaveBeenCalledTimes(1);
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("http://backend.test/api/v1/export/evidence-pack?control_family=governance");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer synthetic-victim-session");
  });

  it("encodes the control family exactly as before", async () => {
    await GET(new NextRequest("https://kerno.example.test/api/export?control_family=a%20b%2Fc", {
      headers: { "sec-fetch-site": "same-origin" },
    }));
    expect((global.fetch as jest.Mock).mock.calls[0][0]).toBe(
      "http://backend.test/api/v1/export/evidence-pack?control_family=a%20b%2Fc",
    );
  });

  it.each([403, 404, 429])("relays a backend %s rather than inventing a file", async (status) => {
    global.fetch = jest.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "refused by the backend" }), { status }),
    );
    const response = await GET(request("same-origin"));
    expect(response.status).toBe(status);
    expect(response.headers.get("content-disposition")).toBeNull();
    expect(await response.json()).toEqual({ detail: "refused by the backend" });
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});
