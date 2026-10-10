/**
 * __tests__/evidence-upload-route.test.ts — the evidence upload proxy verifies the session, then bounds the body (SEC-REMED-003).
 *
 * What:  drives the real POST handler with request bodies that record every
 *        chunk pulled from them. A caller without a session the backend
 *        verifies is refused before one body byte is read; headers alone can
 *        refuse; otherwise the bytes that actually arrive are counted against
 *        the limit and reading stops at the first chunk over it. A verified
 *        upload within the limit is forwarded unparsed and byte-identical.
 * Why:   regression for finding resource-exhaustion.evidence-buffering — the
 *        proxy used to call request.formData() before any session check.
 * How:   npm test -- evidence-upload-route; mocked cookies, limits and backend.
 */

import { NextRequest } from "next/server";

import { POST } from "@/app/api/evidence/route";
import { config as middlewareConfig } from "@/middleware";

const cookieGet = jest.fn();
jest.mock("next/headers", () => ({ cookies: async () => ({ get: cookieGet }) }));

const TEST_FILE_LIMIT = 1024;
const TEST_BODY_LIMIT = 2048;
jest.mock("../lib/evidence-upload-limits", () => ({
  MAX_EVIDENCE_FILE_BYTES: 1024,
  EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES: 1024,
  EVIDENCE_UPLOAD_MAX_BODY_BYTES: 2048,
}));

const ORIGIN = "https://kerno.example.test";
const UPLOAD_URL = `${ORIGIN}/api/evidence`;
const SESSION_URL = "http://backend.test/api/v1/auth/me";
const BOUNDARY = "KernoSecRemed003Boundary";
const MULTIPART = `multipart/form-data; boundary=${BOUNDARY}`;
const CHUNK_BYTES = 600;
const STORED = { record_id: "e0000000-0000-4000-e000-000000000031", deduplicated: false };

interface BodyProbe {
  stream: ReadableStream<Uint8Array>;
  pulled: () => number;
}

interface Forwarded {
  url: string;
  init: RequestInit;
}

let session: number | "unreachable";
let forwarded: Forwarded[];
let operation: (init: RequestInit) => Promise<Response>;

/** A request body that hands out one chunk per read and counts them; optionally fails after some. */
function probeBody(chunks: Uint8Array<ArrayBuffer>[], failAfter?: number): BodyProbe {
  let pulled = 0;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (pulled === failAfter) {
        controller.error(new Error("client disconnected"));
      } else if (pulled === chunks.length) {
        controller.close();
      } else {
        controller.enqueue(chunks[pulled]);
        pulled += 1;
      }
    },
  }, { highWaterMark: 0 });
  return { stream, pulled: () => pulled };
}

function bytes(text: string): Uint8Array<ArrayBuffer> {
  return new TextEncoder().encode(text);
}

function filled(length: number): Uint8Array<ArrayBuffer> {
  return new Uint8Array(length).fill("x".charCodeAt(0));
}

function chunked(body: Uint8Array<ArrayBuffer>, size = CHUNK_BYTES): Uint8Array<ArrayBuffer>[] {
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  for (let start = 0; start < body.byteLength; start += size) chunks.push(body.slice(start, start + size));
  return chunks;
}

/** A hand-built multipart body with a known boundary, so forwarding can be compared byte for byte. */
function multipart(content: Uint8Array<ArrayBuffer>, filename = "policy.txt", type = "text/plain"): Uint8Array<ArrayBuffer> {
  const head = bytes(`--${BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="${filename}"\r\n`
    + `Content-Type: ${type}\r\n\r\n`);
  const tail = bytes(`\r\n--${BOUNDARY}\r\nContent-Disposition: form-data; name="record_type"\r\n\r\npolicy\r\n`
    + `--${BOUNDARY}--\r\n`);
  const body = new Uint8Array(head.byteLength + content.byteLength + tail.byteLength);
  body.set(head, 0);
  body.set(content, head.byteLength);
  body.set(tail, head.byteLength + content.byteLength);
  return body;
}

function upload(body: BodyInit | null, headers: Record<string, string> = {}, signal?: AbortSignal): NextRequest {
  return request(body, { "content-type": MULTIPART, ...headers }, signal);
}

function request(body: BodyInit | null, headers: Record<string, string>, signal?: AbortSignal): NextRequest {
  const init = { method: "POST", body, duplex: "half", signal, headers: { origin: ORIGIN, ...headers } };
  return new NextRequest(UPLOAD_URL, init as ConstructorParameters<typeof NextRequest>[1]);
}

async function forwardedBytes(index = 0): Promise<Uint8Array<ArrayBuffer>> {
  return new Uint8Array(await new Response(forwarded[index].init.body as BodyInit).arrayBuffer());
}

beforeEach(() => {
  jest.replaceProperty(process, "env", { ...process.env,
    KERNO_TRUSTED_ORIGINS: ORIGIN, KERNO_API_URL: "http://backend.test" });
  cookieGet.mockReturnValue({ value: "synthetic-session" });
  session = 200;
  forwarded = [];
  operation = async () => Response.json(STORED, { status: 201 });
  global.fetch = jest.fn().mockImplementation(async (url: string, init: RequestInit = {}) => {
    if (url === SESSION_URL) {
      if (session === "unreachable") throw new TypeError("fetch failed");
      return Response.json({ email: "u@example.test", role: "compliance_lead" }, { status: session });
    }
    forwarded.push({ url, init });
    return operation(init);
  });
});
afterEach(() => jest.restoreAllMocks());

describe("POST /api/evidence — an unverified caller is refused before any body byte is read", () => {
  it("refuses a request with no session cookie without asking the backend", async () => {
    cookieGet.mockReturnValue(undefined);
    const probe = probeBody(chunked(multipart(bytes("synthetic evidence"))));
    const anonymous = upload(probe.stream);
    const formData = jest.spyOn(anonymous, "formData");
    const response = await POST(anonymous);
    expect(response.status).toBe(401);
    expect(response.headers.get("connection")).toBe("close");
    expect(probe.pulled()).toBe(0);
    expect(formData).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it.each([
    ["a forged or expired token", 401, 401],
    ["a backend error while verifying", 500, 502],
    ["an unreachable backend", "unreachable" as const, 502],
  ])("refuses %s, reading nothing and forwarding nothing", async (_label, verification, status) => {
    session = verification;
    const probe = probeBody(chunked(multipart(bytes("synthetic evidence"))));
    const unverified = upload(probe.stream);
    const formData = jest.spyOn(unverified, "formData");
    const response = await POST(unverified);
    expect(response.status).toBe(status);
    expect(response.headers.get("connection")).toBe("close");
    expect(probe.pulled()).toBe(0);
    expect(formData).not.toHaveBeenCalled();
    expect(forwarded).toEqual([]);
    const sessionCall = (global.fetch as jest.Mock).mock.calls[0];
    expect(sessionCall[0]).toBe(SESSION_URL);
    expect(new Headers(sessionCall[1].headers).get("authorization")).toBe("Bearer synthetic-session");
  });

  it("checks the session before judging the body's headers", async () => {
    session = 401;
    const response = await POST(upload(probeBody([]).stream, { "content-type": "application/json" }));
    expect(response.status).toBe(401);
  });
});

describe("POST /api/evidence — headers alone can refuse, before any read", () => {
  it.each(["application/json", "text/plain", "application/x-www-form-urlencoded", "multipart/mixed; boundary=x"])(
    "refuses Content-Type %s with 415", async (contentType) => {
      const probe = probeBody([bytes("{}")]);
      const response = await POST(upload(probe.stream, { "content-type": contentType }));
      expect(response.status).toBe(415);
      expect(response.headers.get("connection")).toBe("close");
      expect(probe.pulled()).toBe(0);
      expect(forwarded).toEqual([]);
    },
  );

  it("refuses a missing Content-Type with 415", async () => {
    const probe = probeBody([bytes("{}")]);
    const response = await POST(request(probe.stream, {}));
    expect(response.status).toBe(415);
    expect(probe.pulled()).toBe(0);
  });

  it.each(["multipart/form-data", "multipart/form-data; boundary=", "multipart/form-data; charset=utf-8"])(
    "refuses %s (no boundary) with 400", async (contentType) => {
      const probe = probeBody([bytes("--x")]);
      const response = await POST(upload(probe.stream, { "content-type": contentType }));
      expect(response.status).toBe(400);
      expect(probe.pulled()).toBe(0);
    },
  );

  it("matches the media type and the boundary parameter without regard to case", async () => {
    const body = multipart(bytes("synthetic evidence"));
    const response = await POST(upload(body, { "content-type": `Multipart/Form-Data; Boundary=${BOUNDARY}` }));
    expect(response.status).toBe(201);
    expect(forwarded).toHaveLength(1);
  });

  it.each(["lots", "-1", "+2", "1_000", "2.0", "0x10"])("refuses a malformed Content-Length %s with 400", async (declared) => {
    const probe = probeBody([bytes("--x")]);
    const response = await POST(upload(probe.stream, { "content-length": declared }));
    expect(response.status).toBe(400);
    expect(probe.pulled()).toBe(0);
  });

  it("refuses a declared over-limit body with 413 and Connection: close, reading none of it", async () => {
    const probe = probeBody(chunked(filled(TEST_BODY_LIMIT * 2)));
    const response = await POST(upload(probe.stream, { "content-length": String(TEST_BODY_LIMIT + 1) }));
    expect(response.status).toBe(413);
    expect(response.headers.get("connection")).toBe("close");
    expect(await response.json()).toEqual({ detail: "upload exceeds the size limit" });
    expect(probe.pulled()).toBe(0);
    expect(forwarded).toEqual([]);
  });
});

describe("POST /api/evidence — the bytes that actually arrive are the authority", () => {
  it("cuts off an unknown-length over-limit body at the chunk that crosses the limit", async () => {
    const probe = probeBody(chunked(filled(CHUNK_BYTES * 10)));
    const response = await POST(upload(probe.stream));
    expect(response.status).toBe(413);
    expect(response.headers.get("connection")).toBe("close");
    expect(probe.pulled()).toBe(Math.floor(TEST_BODY_LIMIT / CHUNK_BYTES) + 1);
    expect(probe.stream.locked).toBe(false);
    expect(forwarded).toEqual([]);
  });

  it("does not let an understated Content-Length lift the limit", async () => {
    const probe = probeBody(chunked(filled(CHUNK_BYTES * 10)));
    const response = await POST(upload(probe.stream, { "content-length": "10" }));
    expect(response.status).toBe(413);
    expect(probe.pulled()).toBe(Math.floor(TEST_BODY_LIMIT / CHUNK_BYTES) + 1);
    expect(forwarded).toEqual([]);
  });

  it("forwards a body of exactly the limit unparsed and byte-identical", async () => {
    const envelope = multipart(new Uint8Array(0)).byteLength;
    const body = multipart(filled(TEST_BODY_LIMIT - envelope));
    expect(body.byteLength).toBe(TEST_BODY_LIMIT);
    const probe = probeBody(chunked(body));
    const response = await POST(upload(probe.stream));
    expect(response.status).toBe(201);
    expect(forwarded).toHaveLength(1);
    expect(forwarded[0].url).toBe("http://backend.test/api/v1/evidence");
    expect(forwarded[0].init.method).toBe("POST");
    expect(new Headers(forwarded[0].init.headers).get("content-type")).toBe(MULTIPART);
    expect(new Headers(forwarded[0].init.headers).get("authorization")).toBe("Bearer synthetic-session");
    expect(await forwardedBytes()).toEqual(body);
  });

  it("refuses a body one byte over the limit", async () => {
    const envelope = multipart(new Uint8Array(0)).byteLength;
    const probe = probeBody(chunked(multipart(filled(TEST_BODY_LIMIT - envelope + 1))));
    const response = await POST(upload(probe.stream));
    expect(response.status).toBe(413);
    expect(forwarded).toEqual([]);
  });

  it("stops and forwards nothing when the client disconnects mid-body", async () => {
    const probe = probeBody(chunked(multipart(filled(TEST_FILE_LIMIT))), 1);
    const response = await POST(upload(probe.stream));
    expect(response.status).toBe(400);
    expect(probe.stream.locked).toBe(false);
    expect(forwarded).toEqual([]);
  });

  it("cancels the backend request when the client leaves while it is in flight", async () => {
    const client = new AbortController();
    let upstreamSignal: AbortSignal | undefined;
    operation = (init) => new Promise((_resolve, reject) => {
      upstreamSignal = init.signal ?? undefined;
      upstreamSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      client.abort();
    });
    const response = await POST(upload(multipart(bytes("synthetic evidence")), {}, client.signal));
    expect(upstreamSignal?.aborted).toBe(true);
    expect(response.status).toBe(400);
  });
});

describe("POST /api/evidence — a verified upload keeps its contract", () => {
  it("forwards a browser FormData text upload with its own boundary and relays the stored record", async () => {
    const form = new FormData();
    form.set("file", new Blob(["Access review policy v2."], { type: "text/plain" }), "policy.txt");
    form.set("record_type", "policy");
    form.set("title", "Access review");
    const browserShaped = request(form, {});
    const response = await POST(browserShaped);
    expect(response.status).toBe(201);
    expect(await response.json()).toEqual(STORED);
    const contentType = new Headers(forwarded[0].init.headers).get("content-type") ?? "";
    expect(contentType).toBe(browserShaped.headers.get("content-type"));
    const relayed = await new Response(await forwardedBytes(), { headers: { "content-type": contentType } }).formData();
    expect(await (relayed.get("file") as File).text()).toBe("Access review policy v2.");
    expect((relayed.get("file") as File).name).toBe("policy.txt");
    expect(relayed.get("record_type")).toBe("policy");
    expect(relayed.get("title")).toBe("Access review");
  });

  it("forwards binary PDF bytes untouched", async () => {
    const pdf = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x34, 0x0a, 0x00, 0xff, 0xe2, 0x80, 0x0d, 0x0a]);
    const body = multipart(pdf, "isms.pdf", "application/pdf");
    const response = await POST(upload(probeBody(chunked(body, 7)).stream));
    expect(response.status).toBe(201);
    expect(await forwardedBytes()).toEqual(body);
  });

  it("relays a duplicate-upload response unchanged", async () => {
    operation = async () => Response.json({ ...STORED, deduplicated: true }, { status: 201 });
    const response = await POST(upload(multipart(bytes("synthetic evidence"))));
    expect(response.status).toBe(201);
    expect((await response.json()).deduplicated).toBe(true);
  });

  it.each([
    [403, "your role is not permitted to perform this action"],
    [413, "file exceeds the 10485760-byte upload limit"],
    [422, "record_type is required"],
  ])("relays the backend's %i refusal", async (status, detail) => {
    operation = async () => Response.json({ detail }, { status });
    const response = await POST(upload(multipart(bytes("synthetic evidence"))));
    expect(response.status).toBe(status);
    expect(await response.json()).toEqual({ detail });
  });
});

describe("POST /api/evidence — what Next.js does before the route runs", () => {
  it("keeps API routes outside the middleware matcher, so Next.js never buffers or truncates the upload body", () => {
    // Next.js 16.2.10 clones the request body for every request its middleware
    // matches, buffering without backpressure and cutting it off silently at
    // 10 MB, before this route's session check runs (verified in
    // next/dist/server/body-streams.js). Only dashboard pages may match.
    expect(middlewareConfig.matcher.every((path: string) => path.startsWith("/dashboard"))).toBe(true);
  });
});
