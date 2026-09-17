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

function loginRequest(body: object): NextRequest {
  return new NextRequest("http://localhost:3000/api/auth/login", {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  process.env.KERNO_API_URL = "http://backend.test";
});

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
    const response = await logoutPost();

    expect(response.status).toBe(200);
    const setCookie = response.headers.get("set-cookie") ?? "";
    expect(setCookie).toContain("kerno_session=");
    expect(setCookie).toContain("Max-Age=0");
    expect(setCookie).toContain("HttpOnly");
  });
});
