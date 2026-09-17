/**
 * __tests__/middleware.test.ts — protected-route redirect (KER-301 AC-3/AC-8).
 *
 * What:  /dashboard/* without a session cookie redirects to /login; with a
 *        cookie the request passes through to the page (where the layout's
 *        /me call does the cryptographic validation).
 * Why:   this is the outer gate of the two-layer protection.
 * How:   npm test
 */

import { NextRequest } from "next/server";

import { middleware } from "@/middleware";

function dashboardRequest(cookieValue?: string): NextRequest {
  const request = new NextRequest("http://localhost:3000/dashboard");
  if (cookieValue) {
    request.cookies.set("kerno_session", cookieValue);
  }
  return request;
}

describe("middleware", () => {
  it("redirects to /login when no session cookie is present", () => {
    const response = middleware(dashboardRequest());

    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("http://localhost:3000/login");
  });

  it("passes the request through when a session cookie is present", () => {
    const response = middleware(dashboardRequest("some-jwt"));

    expect(response.status).toBe(200);
    expect(response.headers.get("location")).toBeNull();
  });
});
