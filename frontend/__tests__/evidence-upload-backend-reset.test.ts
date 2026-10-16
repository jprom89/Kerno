/**
 * __tests__/evidence-upload-backend-reset.test.ts — the API's busy 503 on a real socket that is then closed with the upload unread (PILOT-READY-004, G6).
 *
 * What:  a real node:http listener on 127.0.0.1 stands in for the API. It
 *        verifies the session (GET /api/v1/auth/me) and answers every upload
 *        the way the API answers one at a full house: 503 with Retry-After: 5
 *        and Connection: close, before its handler reads one byte of the body.
 *        The socket is then closed with the body unread, either at once, as
 *        uvicorn closes its transport, or as Node closes it (discarding what it
 *        has buffered first). Bodies of 64 KiB, 1 MiB and 10 MiB go through
 *        the real POST handler and Node's real fetch — only the cookie store
 *        is mocked, and the limits are the real ones. Every answer is a
 *        controlled refusal: the busy 503 with its Retry-After, or a 502 with a
 *        string detail; never a throw and never a 500. The listener sees
 *        exactly one upload request per upload (nothing is retried), and every
 *        upload slot is free afterwards.
 * Why:   finding proxy-busy-503-relay-lost-on-socket-reset. Closing a socket
 *        with unread data resets it, often while the proxy is still writing
 *        the upload, so fetch rejects or the answer's body fails after its
 *        headers; the proxy used to rethrow both, and Next.js answered 500. A
 *        mocked fetch has no socket and cannot show this. On this Windows host
 *        (Node 24.14.1) closing at once reset every upload of every size
 *        before the 503 was read, and Node's own close let the 503 through up
 *        to 1 MiB and reset the larger uploads, so both outcomes occur here.
 * How:   npm test -- evidence-upload-backend-reset. Loopback only: no
 *        database and no credentials.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

import { NextRequest } from "next/server";

import { POST } from "@/app/api/evidence/route";
import { EVIDENCE_UPLOAD_MAX_BODY_BYTES } from "@/lib/evidence-upload-limits";
import { activeUploads } from "@/lib/upload-intake";

const cookieGet = jest.fn();
jest.mock("next/headers", () => ({ cookies: async () => ({ get: cookieGet }) }));

const ORIGIN = "https://kerno.example.test";
const UPLOAD_URL = `${ORIGIN}/api/evidence`;
const LOOPBACK = "127.0.0.1";
const SESSION_PATH = "/api/v1/auth/me";
const BOUNDARY = "KernoPilotReady004ResetBoundary";
const MULTIPART = `multipart/form-data; boundary=${BOUNDARY}`;
const OK_STATUS = 200;
const SERVICE_UNAVAILABLE_STATUS = 503;
const RETRY_AFTER = "5";
const BUSY_DETAIL = "evidence uploads are busy; try again shortly";
const KIB = 1024;
const MIB = 1024 * KIB;
const BODY_SIZES = [64 * KIB, MIB, 10 * MIB];
// A few tries per case, because whether the 503 survives the close varies from one try to the next.
const TRIES_PER_CASE = 3;
// Far beyond what a loopback refusal takes, far inside the proxy's own deadlines.
const TEST_TIMEOUT_MS = 30_000;

// The only answers the proxy may give: the busy refusal relayed, or a 502 because the connection broke.
const CONTROLLED_OUTCOMES = [
  `503 retry-after=${RETRY_AFTER} ${BUSY_DETAIL}`,
  "502 retry-after=null the evidence service could not be reached",
];

/** How the API stand-in closes the socket after its refusal: at once, or leaving it to Node. */
type Closing = "at once" | "as Node closes it";

let listener: Server;
let apiUrl: string;
let closing: Closing;
let uploadRequests: number;

/**
 * The API stand-in: verify any session as a compliance lead; refuse every
 * upload as the API does at a full house, before reading its body, and close
 * the socket with the body unread once the refusal has been written.
 */
function answerAsTheBusyApi(request: IncomingMessage, response: ServerResponse): void {
  if (request.method === "GET" && request.url === SESSION_PATH) {
    response.writeHead(OK_STATUS, { "content-type": "application/json" });
    response.end(JSON.stringify({ email: "u@example.test", role: "compliance_lead" }));
    return;
  }
  uploadRequests += 1;
  response.writeHead(SERVICE_UNAVAILABLE_STATUS, {
    "content-type": "application/json", "retry-after": RETRY_AFTER, connection: "close",
  });
  const refusal = JSON.stringify({ detail: BUSY_DETAIL });
  if (closing === "at once") {
    response.end(refusal, () => request.socket.destroy());
  } else {
    response.end(refusal);
  }
}

/** A multipart body of exactly size bytes, its file part padded to fit. */
function multipartOfSize(size: number): Uint8Array<ArrayBuffer> {
  const encoder = new TextEncoder();
  const head = encoder.encode(`--${BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="policy.txt"\r\n`
    + "Content-Type: text/plain\r\n\r\n");
  const tail = encoder.encode(`\r\n--${BOUNDARY}\r\nContent-Disposition: form-data; name="record_type"\r\n\r\npolicy\r\n`
    + `--${BOUNDARY}--\r\n`);
  const body = new Uint8Array(size).fill("x".charCodeAt(0));
  body.set(head, 0);
  body.set(tail, size - tail.byteLength);
  return body;
}

/** An upload request to the proxy, as the browser sends one. */
function upload(body: Uint8Array<ArrayBuffer>): NextRequest {
  const init = { method: "POST", body, duplex: "half", headers: { origin: ORIGIN, "content-type": MULTIPART } };
  return new NextRequest(UPLOAD_URL, init as ConstructorParameters<typeof NextRequest>[1]);
}

/** One answer as its status, relayed Retry-After and detail, for comparison with the controlled outcomes. */
async function outcomeOf(response: Response): Promise<string> {
  const { detail } = await response.json();
  const shown = typeof detail === "string" ? detail : `non-string detail ${JSON.stringify(detail)}`;
  return `${response.status} retry-after=${response.headers.get("retry-after")} ${shown}`;
}

beforeAll(async () => {
  listener = createServer(answerAsTheBusyApi);
  await new Promise<void>((resolve) => listener.listen(0, LOOPBACK, resolve));
  apiUrl = `http://${LOOPBACK}:${(listener.address() as AddressInfo).port}`;
});
afterAll(async () => {
  listener.closeAllConnections();
  await new Promise((resolve) => listener.close(resolve));
});
beforeEach(() => {
  jest.replaceProperty(process, "env", { ...process.env, KERNO_TRUSTED_ORIGINS: ORIGIN, KERNO_API_URL: apiUrl });
  cookieGet.mockReturnValue({ value: "synthetic-session" });
  uploadRequests = 0;
});
afterEach(() => {
  jest.restoreAllMocks();
});

describe("POST /api/evidence — the API's busy 503 over a socket closed with the upload unread", () => {
  const cases = (["at once", "as Node closes it"] as Closing[]).flatMap((how) => BODY_SIZES.map((size) => ({ how, size })));

  it.each(cases)("answers a $size-byte upload with a controlled refusal when the API closes $how", async ({ how, size }) => {
    expect(size).toBeLessThanOrEqual(EVIDENCE_UPLOAD_MAX_BODY_BYTES);
    closing = how;
    const body = multipartOfSize(size);
    for (let attempt = 1; attempt <= TRIES_PER_CASE; attempt += 1) {
      const response = await POST(upload(body));
      expect(CONTROLLED_OUTCOMES).toContain(await outcomeOf(response));
      expect(uploadRequests).toBe(attempt);
      expect(activeUploads()).toBe(0);
    }
  }, TEST_TIMEOUT_MS);
});
