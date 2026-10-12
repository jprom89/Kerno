/**
 * __tests__/serving-profile.test.ts — the server-side serving profile and the Recalculate proxy (PILOT-READY-003).
 *
 * What:  the resolver accepts only blank/"full"/"pilot"; the recalculate proxy
 *        answers 404 in pilot and 503 on an unrecognised value before any
 *        origin check, cookie read, body read or backend call, and keeps its
 *        existing full-profile order (origin first, then the backend).
 * Why:   the pilot API does not register manual recalculation; a working
 *        frontend proxy would be an alternate path to a surface the pilot
 *        excludes, and a guessed profile would be the same mistake quietly.
 * How:   npm test -- serving-profile; real handler, mocked cookies and fetch.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { NextRequest } from "next/server";

const cookieGet = jest.fn().mockReturnValue({ value: "synthetic-session" });
jest.mock("next/headers", () => ({ cookies: async () => ({ get: cookieGet }) }));

import { POST as recalculate } from "@/app/api/recalculate/route";
import {
  configuredServingProfile,
  isFullProfile,
  resolveServingProfile,
  ServingProfileConfigError,
} from "@/lib/servingProfile";

const ORIGIN = "https://kerno.example.test";
const BACKEND = "http://backend.test";
const RECALCULATION_URL = `${BACKEND}/api/v1/scheduler/run-recalculation`;
const ORIGIN_POLICY_UNSET = "";
const UNRECOGNISED = ["Pilot", "FULL", "staging", "1", "true", "pilot,full", "full pilot"];

/** Replace the server environment for one test; undefined leaves the profile unset. */
function serverEnvironment(profile: string | undefined, trustedOrigins = ORIGIN) {
  jest.replaceProperty(process, "env", {
    ...process.env,
    KERNO_SERVING_PROFILE: profile,
    KERNO_TRUSTED_ORIGINS: trustedOrigins,
    KERNO_API_URL: BACKEND,
  });
}

/** A cookie-bearing recalculation POST from the given origin (null omits the header). */
function recalculation(origin: string | null): NextRequest {
  const headers = new Headers({
    cookie: "kerno_session=synthetic-session",
    "content-type": "application/json",
  });
  if (origin !== null) headers.set("origin", origin);
  return new NextRequest(`${ORIGIN}/api/recalculate`, { method: "POST", body: "{}", headers });
}

beforeEach(() => {
  global.fetch = jest.fn().mockResolvedValue(
    Response.json({ status: "recalculated", override_count: 3 }),
  );
});
afterEach(() => jest.restoreAllMocks());

describe("resolveServingProfile", () => {
  it.each([undefined, "", "   ", "\t\n"])("treats %p as unset and resolves full", (value) => {
    expect(resolveServingProfile(value)).toBe("full");
  });

  it.each([
    ["full", "full"],
    ["pilot", "pilot"],
    [" pilot ", "pilot"],
    ["\tfull\n", "full"],
  ])("resolves %p to %p", (value, expected) => {
    expect(resolveServingProfile(value)).toBe(expected);
  });

  it.each(UNRECOGNISED)("refuses %p instead of widening it to full", (value) => {
    expect(() => resolveServingProfile(value)).toThrow(ServingProfileConfigError);
  });

  it("does not echo the unrecognised value into the error", () => {
    expect(() => resolveServingProfile("staging")).toThrow(/KERNO_SERVING_PROFILE must be "full" or "pilot"/);
    expect(() => resolveServingProfile("staging")).not.toThrow(/staging/);
  });

});

describe("configuredServingProfile", () => {
  it.each([
    [undefined, "full"],
    ["full", "full"],
    [" pilot ", "pilot"],
  ])("reads %p from this server's own environment as %p", (profile, expected) => {
    serverEnvironment(profile);
    expect(configuredServingProfile()).toBe(expected);
  });

  it("ignores a NEXT_PUBLIC_ variable of the same name", () => {
    serverEnvironment(undefined);
    process.env.NEXT_PUBLIC_KERNO_SERVING_PROFILE = "pilot";
    expect(configuredServingProfile()).toBe("full");
  });
});

describe("isFullProfile", () => {
  it.each([
    [undefined, true],
    ["full", true],
    ["pilot", false],
  ])("profile %p gives %p", (profile, expected) => {
    serverEnvironment(profile);
    expect(isFullProfile()).toBe(expected);
  });

  it("throws on an unrecognised value", () => {
    serverEnvironment("Pilot");
    expect(() => isFullProfile()).toThrow(ServingProfileConfigError);
  });
});

describe("POST /api/recalculate under the pilot profile", () => {
  it.each([ORIGIN, "https://evil.test", null])(
    "answers 404 for origin %p without origin, cookie, body or backend use", async (origin) => {
      serverEnvironment("pilot");
      const request = recalculation(origin);
      const json = jest.spyOn(request, "json");
      const response = await recalculate(request);
      expect(response.status).toBe(404);
      expect(await response.json()).toEqual({ detail: "Not Found" });
      expect(json).not.toHaveBeenCalled();
      expect(cookieGet).not.toHaveBeenCalled();
      expect(global.fetch).not.toHaveBeenCalled();
      expect(response.headers.get("set-cookie")).toBeNull();
    },
  );

  it("answers 404 even when the origin policy is not configured", async () => {
    serverEnvironment(" pilot ", ORIGIN_POLICY_UNSET);
    const response = await recalculate(recalculation(ORIGIN));
    expect(response.status).toBe(404);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe("POST /api/recalculate with an unrecognised profile", () => {
  it.each(UNRECOGNISED)("answers a generic 503 for %p without cookie or backend use", async (profile) => {
    serverEnvironment(profile);
    const response = await recalculate(recalculation(ORIGIN));
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ detail: "serving profile is not configured" });
    expect(cookieGet).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe("POST /api/recalculate under the full profile", () => {
  it.each(["full", undefined])("forwards a trusted request with the session for profile %p", async (profile) => {
    serverEnvironment(profile);
    const response = await recalculate(recalculation(ORIGIN));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "recalculated", override_count: 3 });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(RECALCULATION_URL);
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer synthetic-session");
  });

  it("still rejects an untrusted origin before the cookie or the backend", async () => {
    serverEnvironment("full");
    const response = await recalculate(recalculation("https://evil.test"));
    expect(response.status).toBe(403);
    expect(cookieGet).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("still fails closed when the origin policy is not configured", async () => {
    serverEnvironment("full", ORIGIN_POLICY_UNSET);
    const response = await recalculate(recalculation(ORIGIN));
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ detail: "browser origin policy is not configured" });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("relays the backend's refusal unchanged", async () => {
    serverEnvironment("full");
    global.fetch = jest.fn().mockResolvedValue(Response.json({ detail: "forbidden" }, { status: 403 }));
    const response = await recalculate(recalculation(ORIGIN));
    expect(response.status).toBe(403);
    expect(await response.json()).toEqual({ detail: "forbidden" });
  });
});

// Kickoff §4.3 backend surfaces that the pilot API does not register.
const PILOT_EXCLUDED_SURFACES = [
  "/api/v1/remediation",
  "/api/v1/webhooks",
  "/trust-center",
  "/api/v1/ai-decisions",
  "/api/v1/panel",
  "/api/v1/scheduler",
  "/api/v1/register/windows",
];

/** List maintained-UI source files that name a pilot-excluded backend surface. */
function excludedSurfaceCallers(directory: string, root: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      found.push(...excludedSurfaceCallers(path, root));
    } else if (/\.tsx?$/.test(entry.name)) {
      const source = readFileSync(path, "utf8");
      if (PILOT_EXCLUDED_SURFACES.some((surface) => source.includes(surface))) {
        found.push(relative(root, path).replaceAll("\\", "/"));
      }
    }
  }
  return found;
}

it("gates every maintained-UI caller of a pilot-excluded backend surface", () => {
  const root = process.cwd();
  const callers = ["app", "components", "lib"].flatMap((top) => excludedSurfaceCallers(join(root, top), root));
  expect(callers).toEqual(["app/api/recalculate/route.ts"]);
  for (const caller of callers) {
    expect(readFileSync(join(root, caller), "utf8")).toContain("fullProfileRefusal()");
  }
});
