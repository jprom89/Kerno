/**
 * __tests__/serving-profile.test.ts — the server-side serving profile, its startup check and the Recalculate proxy (PILOT-READY-003).
 *
 * What:  the resolver follows the API's contract (unset or blank is "full" only
 *        on a development server and a configuration error anywhere else; only
 *        "full" and "pilot" are recognised); each server resolves the profile
 *        once and keeps that answer, or that error, whatever the environment
 *        says later; instrumentation.ts refuses to start on a configuration
 *        error; and the recalculate proxy answers 404 in pilot and 503 on a
 *        configuration error before any origin check, cookie read, body read or
 *        backend call, keeping its existing full-profile order otherwise.
 * Why:   the pilot API does not register manual recalculation; a working
 *        frontend proxy would be an alternate path to a surface the pilot
 *        excludes, and a guessed profile would be the same mistake quietly.
 * How:   npm test -- serving-profile. Every scenario loads fresh modules, as a
 *        newly started server would, so the once-per-server cache is real.
 */

import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { NextRequest } from "next/server";

const cookieGet = jest.fn().mockReturnValue({ value: "synthetic-session" });
jest.mock("next/headers", () => ({ cookies: async () => ({ get: cookieGet }) }));

type ProfileModule = typeof import("../lib/servingProfile");
type RouteModule = typeof import("../app/api/recalculate/route");
type InstrumentationModule = typeof import("../instrumentation");
type ServerMode = "development" | "production";

interface FreshServer {
  profile: ProfileModule;
  route: RouteModule;
}

const ORIGIN = "https://kerno.example.test";
const BACKEND = "http://backend.test";
const RECALCULATION_URL = `${BACKEND}/api/v1/scheduler/run-recalculation`;
const ORIGIN_POLICY_UNSET = "";
const MODES: ServerMode[] = ["development", "production"];
const UNSET_OR_BLANK = [undefined, "", "   ", "\t\n"];
const UNRECOGNISED = ["Pilot", "FULL", "staging", "1", "true", "pilot,full", "full pilot"];
const MISSING_OUTSIDE_DEVELOPMENT = /KERNO_SERVING_PROFILE must be set to "full" or "pilot" outside development/;
const NOT_RECOGNISED = /KERNO_SERVING_PROFILE must be "full" or "pilot"; the configured value is not recognised/;

/** Replace this server's environment: its mode, its profile (undefined leaves it unset) and the proxy settings. */
function serverEnvironment(mode: ServerMode, profile: string | undefined, trustedOrigins = ORIGIN) {
  jest.replaceProperty(process, "env", {
    ...process.env,
    NODE_ENV: mode,
    NEXT_RUNTIME: "nodejs",
    KERNO_SERVING_PROFILE: profile,
    KERNO_TRUSTED_ORIGINS: trustedOrigins,
    KERNO_API_URL: BACKEND,
  });
}

/** Load fresh copies of the profile module and the proxy, sharing one isolated module registry. */
async function freshServer(): Promise<FreshServer> {
  let loaded: FreshServer | undefined;
  await jest.isolateModulesAsync(async () => {
    loaded = {
      profile: await import("../lib/servingProfile"),
      route: await import("../app/api/recalculate/route"),
    };
  });
  if (loaded === undefined) throw new Error("modules did not load");
  return loaded;
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
  it.each(UNSET_OR_BLANK)("treats %p on a development server as full", async (value) => {
    const { profile } = await freshServer();
    expect(profile.resolveServingProfile(value, true)).toBe("full");
  });

  it.each(UNSET_OR_BLANK)("refuses %p outside development instead of widening it to full", async (value) => {
    const { profile } = await freshServer();
    expect(() => profile.resolveServingProfile(value, false)).toThrow(profile.ServingProfileConfigError);
    expect(() => profile.resolveServingProfile(value, false)).toThrow(MISSING_OUTSIDE_DEVELOPMENT);
  });

  it.each([
    ["full", "full"],
    ["pilot", "pilot"],
    [" pilot ", "pilot"],
    ["\tfull\n", "full"],
  ])("resolves %p to %p on development and production servers", async (value, expected) => {
    const { profile } = await freshServer();
    expect(profile.resolveServingProfile(value, true)).toBe(expected);
    expect(profile.resolveServingProfile(value, false)).toBe(expected);
  });

  it.each(UNRECOGNISED)("refuses %p in every mode without echoing it", async (value) => {
    const { profile } = await freshServer();
    for (const development of [true, false]) {
      expect(() => profile.resolveServingProfile(value, development)).toThrow(NOT_RECOGNISED);
      expect(() => profile.resolveServingProfile(value, development)).not.toThrow(value);
    }
  });
});

describe("serverServingProfile", () => {
  it("resolves a missing profile on a development server to full", async () => {
    serverEnvironment("development", undefined);
    const { profile } = await freshServer();
    expect(profile.isDevelopmentServer()).toBe(true);
    expect(profile.serverServingProfile()).toBe("full");
    expect(profile.isFullProfile()).toBe(true);
  });

  it.each([undefined, "", "  "])("refuses %p on a production server as a configuration error", async (value) => {
    serverEnvironment("production", value);
    const { profile } = await freshServer();
    expect(profile.isDevelopmentServer()).toBe(false);
    expect(() => profile.serverServingProfile()).toThrow(MISSING_OUTSIDE_DEVELOPMENT);
    expect(() => profile.isFullProfile()).toThrow(profile.ServingProfileConfigError);
  });

  it("treats any NODE_ENV other than development (here test) as outside development", async () => {
    jest.replaceProperty(process, "env", { ...process.env, NODE_ENV: "test", KERNO_SERVING_PROFILE: undefined });
    const { profile } = await freshServer();
    expect(profile.isDevelopmentServer()).toBe(false);
    expect(() => profile.serverServingProfile()).toThrow(MISSING_OUTSIDE_DEVELOPMENT);
  });

  it.each(MODES.flatMap((mode) => [[mode, "full", true], [mode, "pilot", false]] as const))(
    "on a %s server resolves explicit %p (full profile: %p)", async (mode, value, full) => {
      serverEnvironment(mode, value);
      const { profile } = await freshServer();
      expect(profile.serverServingProfile()).toBe(value);
      expect(profile.isFullProfile()).toBe(full);
    },
  );

  it.each(MODES)("refuses an unrecognised value on a %s server", async (mode) => {
    serverEnvironment(mode, "Pilot");
    const { profile } = await freshServer();
    expect(() => profile.serverServingProfile()).toThrow(NOT_RECOGNISED);
  });

  it.each(MODES)("ignores a NEXT_PUBLIC_ variable of the same name on a %s server", async (mode) => {
    serverEnvironment(mode, undefined);
    process.env.NEXT_PUBLIC_KERNO_SERVING_PROFILE = "pilot";
    const { profile } = await freshServer();
    if (mode === "development") {
      expect(profile.serverServingProfile()).toBe("full");
    } else {
      expect(() => profile.serverServingProfile()).toThrow(MISSING_OUTSIDE_DEVELOPMENT);
    }
  });

  it("keeps the first answer for the life of the server, whatever the environment says later", async () => {
    serverEnvironment("production", "pilot");
    const { profile } = await freshServer();
    expect(profile.serverServingProfile()).toBe("pilot");
    serverEnvironment("production", "full");
    expect(profile.serverServingProfile()).toBe("pilot");
    expect(profile.isFullProfile()).toBe(false);
  });

  it("keeps a configuration error for the life of the server, so setting the variable later does not widen it", async () => {
    serverEnvironment("production", undefined);
    const { profile } = await freshServer();
    expect(() => profile.serverServingProfile()).toThrow(MISSING_OUTSIDE_DEVELOPMENT);
    serverEnvironment("production", "full");
    expect(() => profile.serverServingProfile()).toThrow(MISSING_OUTSIDE_DEVELOPMENT);
  });
});

/** Stand in for process.exit: record the code and stop the hook, as a real exit would. */
function interceptExit(): jest.SpyInstance {
  jest.spyOn(console, "error").mockImplementation(() => undefined);
  return jest.spyOn(process, "exit").mockImplementation((code) => {
    throw new Error(`process.exit(${code})`);
  });
}

/**
 * Load instrumentation.ts and the profile module from a reset registry, as a
 * newly started server would. register() imports its Node.js-only check when
 * it is called, so it must share this registry, not an isolated one.
 */
async function startedServer(): Promise<{ instrumentation: InstrumentationModule; profile: ProfileModule }> {
  jest.resetModules();
  const instrumentation = await import("../instrumentation");
  const profile = await import("../lib/servingProfile");
  return { instrumentation, profile };
}

describe("instrumentation register()", () => {
  it.each([
    ["production", undefined, MISSING_OUTSIDE_DEVELOPMENT],
    ["production", "", MISSING_OUTSIDE_DEVELOPMENT],
    ["production", "   ", MISSING_OUTSIDE_DEVELOPMENT],
    ["production", "Pilot", NOT_RECOGNISED],
    ["development", "staging", NOT_RECOGNISED],
  ] as const)("ends a %s server's process with exit 1 for profile %p, saying why", async (mode, value, reason) => {
    serverEnvironment(mode, value);
    const exit = interceptExit();
    const { instrumentation } = await startedServer();
    await expect(instrumentation.register()).rejects.toThrow("process.exit(1)");
    expect(exit).toHaveBeenCalledWith(1);
    expect(console.error).toHaveBeenCalledWith(expect.stringMatching(reason));
    expect(console.error).not.toHaveBeenCalledWith(expect.stringContaining("staging"));
  });

  it.each([
    ["production", "full"],
    ["production", "pilot"],
    ["development", undefined],
    ["development", "pilot"],
  ] as const)("starts a %s server with profile %p and fixes that answer for the server", async (mode, value) => {
    serverEnvironment(mode, value);
    const exit = interceptExit();
    const { instrumentation, profile } = await startedServer();
    await expect(instrumentation.register()).resolves.toBeUndefined();
    expect(exit).not.toHaveBeenCalled();
    const started = profile.serverServingProfile();
    serverEnvironment(mode, started === "pilot" ? "full" : "pilot");
    expect(profile.serverServingProfile()).toBe(started);
  });

  it("does not validate on the Edge runtime, where middleware does not use the profile", async () => {
    serverEnvironment("production", undefined);
    process.env.NEXT_RUNTIME = "edge";
    const exit = interceptExit();
    const { instrumentation } = await startedServer();
    await expect(instrumentation.register()).resolves.toBeUndefined();
    expect(exit).not.toHaveBeenCalled();
  });

  it("keeps process.exit out of instrumentation.ts, so the Edge bundle never contains it", () => {
    const source = readFileSync(join(process.cwd(), "instrumentation.ts"), "utf8");
    expect(source).not.toMatch(/process\.exit\(/);
    expect(source).toContain('await import("./lib/servingProfileStartup")');
  });
});

describe("POST /api/recalculate under the pilot profile", () => {
  it.each(MODES.flatMap((mode) => [ORIGIN, "https://evil.test", null].map((origin) => [mode, origin] as const)))(
    "on a %s server answers 404 for origin %p without origin, cookie, body or backend use", async (mode, origin) => {
      serverEnvironment(mode, "pilot");
      const { route } = await freshServer();
      const request = recalculation(origin);
      const json = jest.spyOn(request, "json");
      const response = await route.POST(request);
      expect(response.status).toBe(404);
      expect(await response.json()).toEqual({ detail: "Not Found" });
      expect(json).not.toHaveBeenCalled();
      expect(cookieGet).not.toHaveBeenCalled();
      expect(global.fetch).not.toHaveBeenCalled();
      expect(response.headers.get("set-cookie")).toBeNull();
    },
  );

  it("answers 404 even when the origin policy is not configured", async () => {
    serverEnvironment("production", " pilot ", ORIGIN_POLICY_UNSET);
    const { route } = await freshServer();
    const response = await route.POST(recalculation(ORIGIN));
    expect(response.status).toBe(404);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("keeps answering 404 if the environment later says full", async () => {
    serverEnvironment("production", "pilot");
    const { route } = await freshServer();
    expect((await route.POST(recalculation(ORIGIN))).status).toBe(404);
    serverEnvironment("production", "full");
    expect((await route.POST(recalculation(ORIGIN))).status).toBe(404);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe("POST /api/recalculate with a configuration error", () => {
  it.each([
    ...UNSET_OR_BLANK.map((value) => ["production", value] as const),
    ...UNRECOGNISED.map((value) => ["production", value] as const),
    ...UNRECOGNISED.map((value) => ["development", value] as const),
  ])("on a %s server answers a generic 503 for %p without cookie, body or backend use", async (mode, value) => {
    serverEnvironment(mode, value);
    const { route } = await freshServer();
    const request = recalculation(ORIGIN);
    const json = jest.spyOn(request, "json");
    const response = await route.POST(request);
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ detail: "serving profile is not configured" });
    expect(json).not.toHaveBeenCalled();
    expect(cookieGet).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});

describe("POST /api/recalculate under the full profile", () => {
  it.each([
    ["production", "full"],
    ["development", "full"],
    ["development", undefined],
  ] as const)("on a %s server forwards a trusted request with the session for profile %p", async (mode, value) => {
    serverEnvironment(mode, value);
    const { route } = await freshServer();
    const response = await route.POST(recalculation(ORIGIN));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: "recalculated", override_count: 3 });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe(RECALCULATION_URL);
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("authorization")).toBe("Bearer synthetic-session");
  });

  it("still rejects an untrusted origin before the cookie or the backend", async () => {
    serverEnvironment("production", "full");
    const { route } = await freshServer();
    const response = await route.POST(recalculation("https://evil.test"));
    expect(response.status).toBe(403);
    expect(cookieGet).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("still fails closed when the origin policy is not configured", async () => {
    serverEnvironment("production", "full", ORIGIN_POLICY_UNSET);
    const { route } = await freshServer();
    const response = await route.POST(recalculation(ORIGIN));
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ detail: "browser origin policy is not configured" });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("relays the backend's refusal unchanged", async () => {
    serverEnvironment("production", "full");
    global.fetch = jest.fn().mockResolvedValue(Response.json({ detail: "forbidden" }, { status: 403 }));
    const { route } = await freshServer();
    const response = await route.POST(recalculation(ORIGIN));
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
