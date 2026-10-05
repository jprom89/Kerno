/**
 * What: exercise the shared SEC-REMED-002 origin and login media-type policy.
 * Why: compare whole configured origins and fail closed on ambiguous input.
 * How: npm test -- csrf.test.ts; synthetic requests only, no backend or database.
 */
import { rejectUnsafeRequest } from "@/lib/csrf";

const TRUSTED = "https://kerno.example.test";
const FORBIDDEN = 403;
const UNCONFIGURED = 503;
const WRONG_MEDIA_TYPE = 415;

/** Build a synthetic request; Host and proxy headers are deliberately untrusted. */
function request(origin: string | null, contentType = "application/json"): Request {
  const headers = new Headers({ "content-type": contentType, host: "evil.test" });
  if (origin !== null) headers.set("origin", origin);
  headers.set("x-forwarded-host", "evil.test");
  headers.set("x-forwarded-proto", "https");
  headers.set("forwarded", "host=evil.test;proto=https");
  return new Request("https://evil.test/api/auth/login", { method: "POST", headers });
}

beforeEach(() => {
  jest.replaceProperty(process, "env", { ...process.env, KERNO_TRUSTED_ORIGINS: TRUSTED });
});
afterEach(() => jest.restoreAllMocks());

it.each([
  null, "", "null", "https://evil.test", `${TRUSTED}.evil.test`,
  "https://child.kerno.example.test", "http://kerno.example.test",
  `${TRUSTED}:8443`, `${TRUSTED}/`, `${TRUSTED}/path`, `${TRUSTED}?q=1`,
  `${TRUSTED}#fragment`, "https://user@kerno.example.test",
  `${TRUSTED},https://evil.test`, `${TRUSTED} https://evil.test`,
  "https://kerno.example.test:443", "HTTPS://KERNO.EXAMPLE.TEST", "not a URL",
])("rejects missing, malformed or nonmatching origin %s", (origin) => {
  expect(rejectUnsafeRequest(request(origin))?.status).toBe(FORBIDDEN);
});

it.each([undefined, "", " ", "null", "*", "https://*.example.test", `${TRUSTED}/`,
  `${TRUSTED},`, `${TRUSTED},https://user@evil.test`, `${TRUSTED},not-an-origin`,
])("fails the entire policy closed for invalid configuration %s", (configured) => {
  if (configured === undefined) delete process.env.KERNO_TRUSTED_ORIGINS;
  else process.env.KERNO_TRUSTED_ORIGINS = configured;
  expect(rejectUnsafeRequest(request(TRUSTED))?.status).toBe(UNCONFIGURED);
});

it("uses server configuration independently of URL, Host and forwarded headers", () => {
  expect(rejectUnsafeRequest(request(TRUSTED))).toBeNull();
  expect(rejectUnsafeRequest(request("https://evil.test"))?.status).toBe(FORBIDDEN);
});

it("permits each explicitly configured origin, including a development port", () => {
  process.env.KERNO_TRUSTED_ORIGINS = `${TRUSTED}, http://localhost:3000`;
  expect(rejectUnsafeRequest(request(TRUSTED))).toBeNull();
  expect(rejectUnsafeRequest(request("http://localhost:3000"))).toBeNull();
  expect(rejectUnsafeRequest(request("http://localhost:3001"))?.status).toBe(FORBIDDEN);
});

it.each(["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
  "application/jsonp", "application/problem+json", "application/json,text/plain", "",
])("rejects the login media type %s", (mediaType) => {
  expect(rejectUnsafeRequest(request(TRUSTED, mediaType), true)?.status).toBe(WRONG_MEDIA_TYPE);
});

it.each(["application/json", "application/json; charset=utf-8", "Application/JSON"])(
  "permits intended JSON media type %s", (mediaType) => {
    expect(rejectUnsafeRequest(request(TRUSTED, mediaType), true)).toBeNull();
  },
);

it("keeps trusted multipart and bodyless sibling mutations compatible", () => {
  expect(rejectUnsafeRequest(request(TRUSTED, "multipart/form-data; boundary=x"))).toBeNull();
  expect(rejectUnsafeRequest(request(TRUSTED, ""))).toBeNull();
});
