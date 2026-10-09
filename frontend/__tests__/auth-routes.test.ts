/**
 * __tests__/auth-routes.test.ts — the KER-301 AC-8 auth-flow tests (server side).
 *
 * What:  valid login → httpOnly cookie set; invalid login → 401, no cookie;
 *        logout → cookie cleared (Max-Age=0); the JWT never appears in the
 *        login response body.
 * Why:   these routes are the entire cookie security model — if the flags or
 *        the body leak, the httpOnly design is void.
 * How:   npm test
 */

import { NextRequest } from "next/server";

import { POST as loginPost } from "@/app/api/auth/login/route";
import { POST as logoutPost } from "@/app/api/auth/logout/route";

const FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.fake.signature";
const TRUSTED_ORIGIN = "https://kerno.example.test";

/** Return a legitimate same-origin JSON login using only synthetic credentials. */
function loginRequest(body: object): NextRequest {
  return new NextRequest("http://localhost:3000/api/auth/login", {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", Origin: TRUSTED_ORIGIN },
  });
}

beforeEach(() => {
  jest.replaceProperty(process, "env", {
    ...process.env,
    KERNO_API_URL: "http://backend.test",
    KERNO_TRUSTED_ORIGINS: TRUSTED_ORIGIN,
  });
});
afterEach(() => jest.restoreAllMocks());

describe("POST /api/auth/login", () => {
  it("sets the session cookie httpOnly on valid credentials and redacts the JWT", async () => {
    global.fetch = jest.fn().mockResolvedValue(
      new Response(JSON.stringify({ access_token: FAKE_JWT, token_type: "bearer" }), {
        status: 200,
      }),
    );
    const response = await loginPost(
      loginRequest({ email: "lead@kerno.local", password: "pw" }),
    );

    expect(response.status).toBe(200);
    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain(`kerno_session=${FAKE_JWT}`);
    expect(setCookie).toContain("HttpOnly");
    expect(setCookie).toContain("SameSite=lax");
    expect(setCookie).toContain("Path=/");
    // The response BODY must never contain the token — only the cookie does.
    const body = await response.json();
    expect(JSON.stringify(body)).not.toContain(FAKE_JWT);
    // The backend was called at the server-side base URL.
    expect(global.fetch).toHaveBeenCalledWith(
      "http://backend.test/api/v1/auth/login",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("returns 401 with no cookie on invalid credentials", async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ detail: "invalid credentials" }), { status: 401 }));
    const response = await loginPost(
      loginRequest({ email: "lead@kerno.local", password: "wrong" }),
    );

    expect(response.status).toBe(401);
    expect(response.headers.get("set-cookie")).toBeNull();
  });
});

describe("POST /api/auth/logout", () => {
  it("clears the session cookie", async () => {
    const response = await logoutPost(new NextRequest(`${TRUSTED_ORIGIN}/api/auth/logout`, {
      method: "POST", headers: { Origin: TRUSTED_ORIGIN },
    }));

    expect(response.status).toBe(200);
    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("kerno_session=");
    expect(setCookie).toContain("Max-Age=0");
    expect(setCookie).toContain("HttpOnly");
  });
});

describe("SEC-REMED-002 login boundary", () => {
  it.each([undefined, "null", "not-an-origin", `${TRUSTED_ORIGIN}.evil.test`,
    "https://evil.test", `${TRUSTED_ORIGIN}:8443`,
  ])("rejects origin %s before parsing, forwarding or replacing a cookie", async (origin) => {
    global.fetch = jest.fn();
    const request = loginRequest({ email: "attacker@example.test", password: "synthetic" });
    request.headers.set("cookie", "kerno_session=synthetic-victim-session");
    if (origin === undefined) request.headers.delete("origin");
    else request.headers.set("origin", origin);
    const parse = jest.spyOn(request, "json");
    const response = await loginPost(request);
    expect(response.status).toBe(403);
    expect(parse).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it.each(["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", null])(
    "rejects even trusted-origin non-JSON login (%s) before side effects", async (mediaType) => {
      global.fetch = jest.fn();
      const request = loginRequest({ email: "attacker@example.test", password: "synthetic" });
      if (mediaType === null) request.headers.delete("content-type");
      else request.headers.set("content-type", mediaType);
      const parse = jest.spyOn(request, "json");
      const response = await loginPost(request);
      expect(response.status).toBe(415);
      expect(parse).not.toHaveBeenCalled();
      expect(global.fetch).not.toHaveBeenCalled();
      expect(response.headers.get("set-cookie")).toBeNull();
    },
  );

  it("rejects an unconfigured policy without consulting the authentication backend", async () => {
    delete process.env.KERNO_TRUSTED_ORIGINS;
    global.fetch = jest.fn();
    const response = await loginPost(loginRequest({}));
    expect(response.status).toBe(503);
    expect(global.fetch).not.toHaveBeenCalled();
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("preserves production cookie flags and forwards the legitimate tenant credentials", async () => {
    jest.replaceProperty(process, "env", { ...process.env, NODE_ENV: "production" });
    global.fetch = jest.fn().mockResolvedValue(Response.json({ access_token: FAKE_JWT }));
    const credentials = { email: "victim@example.test", password: "synthetic", tenant_slug: "victim" };
    const response = await loginPost(loginRequest(credentials));
    expect(response.status).toBe(200);
    expect(global.fetch).toHaveBeenCalledWith("http://backend.test/api/v1/auth/login",
      expect.objectContaining({ body: JSON.stringify(credentials), cache: "no-store" }));
    for (const flag of ["HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=86400"]) {
      expect(response.headers.get("set-cookie")).toContain(flag);
    }
    expect(await response.json()).toEqual({ ok: true });
  });
});
