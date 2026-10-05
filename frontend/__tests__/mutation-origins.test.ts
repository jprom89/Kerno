/**
 * What: run every cookie-backed mutation handler through the shared origin boundary.
 * Why: a guarded login alone would leave sibling routes and logout open to CSRF.
 * How: npm test -- mutation-origins; real handlers, synthetic cookies, mocked fetch.
 */
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { NextRequest } from "next/server";

const cookieGet = jest.fn().mockReturnValue({ value: "synthetic-victim-session" });
jest.mock("next/headers", () => ({ cookies: async () => ({ get: cookieGet }) }));

import { POST as logout } from "@/app/api/auth/logout/route";
import { POST as upload } from "@/app/api/evidence/route";
import { POST as link } from "@/app/api/evidence/[recordId]/links/route";
import { DELETE as unlink } from "@/app/api/evidence/[recordId]/links/[controlId]/route";
import { POST as override } from "@/app/api/overrides/route";
import { POST as recalculate } from "@/app/api/recalculate/route";
import { POST as generate } from "@/app/api/recommendations/generate/route";
import { POST as register } from "@/app/api/register/route";
import { PATCH as amend } from "@/app/api/register/[entryId]/route";
import { POST as run } from "@/app/api/submissions/runs/route";

const ORIGIN = "https://kerno.example.test";
const routes = [
  { path: "auth/logout", method: "POST", invoke: logout },
  { path: "evidence", method: "POST", invoke: upload },
  { path: "evidence/[recordId]/links", method: "POST", invoke: (request: NextRequest) =>
    link(request, { params: Promise.resolve({ recordId: "record" }) }) },
  { path: "evidence/[recordId]/links/[controlId]", method: "DELETE", invoke: (request: NextRequest) =>
    unlink(request, { params: Promise.resolve({ recordId: "record", controlId: "control" }) }) },
  { path: "overrides", method: "POST", invoke: override },
  { path: "recalculate", method: "POST", invoke: recalculate },
  { path: "recommendations/generate", method: "POST", invoke: generate },
  { path: "register", method: "POST", invoke: register },
  { path: "register/[entryId]", method: "PATCH", invoke: (request: NextRequest) =>
    amend(request, { params: Promise.resolve({ entryId: "entry" }) }) },
  { path: "submissions/runs", method: "POST", invoke: run },
];

/** Return a synthetic cookie-bearing request with the handler's legitimate body form. */
function mutation(path: string, method: string, origin: string | null): NextRequest {
  const headers = new Headers({ cookie: "kerno_session=synthetic-victim-session" });
  if (origin !== null) headers.set("origin", origin);
  let body: BodyInit = JSON.stringify({ control_id: "synthetic-control" });
  if (path === "evidence") {
    const form = new FormData();
    form.set("file", new Blob(["synthetic evidence"]), "example.txt");
    body = form;
  } else {
    headers.set("content-type", "application/json");
  }
  return new NextRequest(`${ORIGIN}/api/${path}`, { method, body, headers });
}

beforeEach(() => {
  jest.replaceProperty(process, "env", { ...process.env,
    KERNO_TRUSTED_ORIGINS: ORIGIN, KERNO_API_URL: "http://backend.test" });
  global.fetch = jest.fn().mockImplementation(async () => Response.json({ ok: true }));
});
afterEach(() => jest.restoreAllMocks());

describe.each(routes)("$method /api/$path", ({ path, method, invoke }) => {
  it.each([null, "null", "malformed", "https://evil.test", `${ORIGIN}.evil.test`])(
    "rejects origin %s without body reads, cookie access, forwarding or Set-Cookie", async (origin) => {
      const request = mutation(path, method, origin);
      const json = jest.spyOn(request, "json");
      const form = jest.spyOn(request, "formData");
      const response = await invoke(request);
      expect(response.status).toBe(403);
      expect(json).not.toHaveBeenCalled();
      expect(form).not.toHaveBeenCalled();
      expect(cookieGet).not.toHaveBeenCalled();
      expect(global.fetch).not.toHaveBeenCalled();
      expect(response.headers.get("set-cookie")).toBeNull();
    },
  );

  it("preserves a trusted-origin operation and the existing cookie/backend boundary", async () => {
    const response = await invoke(mutation(path, method, ORIGIN));
    expect(response.status).toBe(200);
    if (path === "auth/logout") {
      expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
      expect(global.fetch).not.toHaveBeenCalled();
    } else {
      expect(global.fetch).toHaveBeenCalledTimes(1);
      const init = (global.fetch as jest.Mock).mock.calls[0][1];
      expect(new Headers(init.headers).get("authorization")).toBe("Bearer synthetic-victim-session");
      expect(init.method).toBe(method);
      expect(response.headers.get("set-cookie")).toBeNull();
    }
  });

  if (path !== "auth/logout") {
    it("preserves backend permission refusals for trusted requests", async () => {
      global.fetch = jest.fn().mockResolvedValue(Response.json({ detail: "forbidden" }, { status: 403 }));
      const response = await invoke(mutation(path, method, ORIGIN));
      expect(response.status).toBe(403);
      expect(global.fetch).toHaveBeenCalledTimes(1);
      expect(response.headers.get("set-cookie")).toBeNull();
    });
  }
});

/** Enumerate actual exported mutations so a new handler cannot silently escape this matrix. */
function actualMutations(directory: string, root = directory): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) found.push(...actualMutations(path, root));
    else if (entry.name === "route.ts") {
      const source = readFileSync(path, "utf8");
      for (const match of source.matchAll(/export async function (POST|PUT|PATCH|DELETE)\(/g)) {
        found.push(`${match[1]} ${relative(root, directory).replaceAll("\\", "/")}`);
      }
    }
  }
  return found;
}

it("covers every Next.js mutation, with login exercised by auth-routes.test.ts", () => {
  const covered = routes.map(({ path, method }) => `${method} ${path}`);
  expect(actualMutations(join(process.cwd(), "app/api")).sort()).toEqual(
    [...covered, "POST auth/login"].sort(),
  );
});
