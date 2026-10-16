/**
 * __tests__/evidence-upload-admission.test.ts — the upload proxy's per-process admission cap and its deadlines (PILOT-READY-004, G6).
 *
 * What:  drives fresh copies of the real POST handler with real timers and
 *        short deadlines. Overlapping uploads fill the cap, whether they are
 *        still receiving or already forwarding, and the next one is refused
 *        with a 503 at once, before any of its body is read. After every way
 *        an upload can end — stored, refused by the backend, session-check
 *        timeout, client gone during the session check, reception timeout,
 *        over-limit body, client gone mid-body, forwarding timeout, client gone
 *        mid-forward, a broken connection to the backend (a controlled 502, or
 *        the busy 503 whose headers got through), or a whole backend answer
 *        that is not JSON (a controlled 502) — no slot is held, and the
 *        cap still admits exactly CAP and refuses one more. A delayed or
 *        trickled body hits the absolute reception deadline (408) and is read
 *        no further; a stalled session check (request or answer body) is a 502
 *        and a stalled backend (request or answer body) a 504, each aborting
 *        or cancelling what it was waiting on. A client that leaves at any
 *        stage — session request or answer, body, backend request or answer —
 *        stops the work in hand at once, not at the next deadline. By the time
 *        the proxy has answered, every timer it started has fired or been
 *        cleared, and no abort listener it added is still attached.
 * Why:   issue #22 (G6): every upload path needs a finite lifetime and bounded
 *        admission, and the proxy's own waits must end without leaving timers
 *        behind (Node keeps an AbortSignal.timeout timer pending until it fires,
 *        however soon the request finished).
 * How:   npm test -- evidence-upload-admission; mocked cookies and backend,
 *        a fresh module registry per test so no slot count leaks between tests.
 *        Timers are watched with node:async_hooks, so both setTimeout and
 *        AbortSignal.timeout are seen; abort listeners by recording
 *        EventTarget.addEventListener and asking node:events which remain.
 */

import { createHook } from "node:async_hooks";
import { getEventListeners } from "node:events";

import { NextRequest } from "next/server";

const cookieGet = jest.fn();

const ORIGIN = "https://kerno.example.test";
const UPLOAD_URL = `${ORIGIN}/api/evidence`;
const SESSION_URL = "http://backend.test/api/v1/auth/me";
const BOUNDARY = "KernoPilotReady004Boundary";
const MULTIPART = `multipart/form-data; boundary=${BOUNDARY}`;
const STORED = { record_id: "e0000000-0000-4000-e000-000000000041", deduplicated: false };
const BUSY = { detail: "evidence uploads are busy; try again shortly" };
const LATE_BODY = { detail: "upload was not received within the time limit" };
const LATE_BACKEND = { detail: "the evidence service did not respond in time" };
const UNVERIFIED = { detail: "the session could not be verified" };
const CAP = 2;
const RETRY_AFTER_SECONDS = 5;
const GENEROUS_MS = 10_000;
const SHORT_MS = 250;
// Timers may fire a clock tick early by Date.now(); a refusal sooner than this did not wait for its deadline.
const EARLIEST_MS = SHORT_MS - 50;
// Far longer than any deadline used here and far shorter than a test's timeout.
const PROMPT_MS = 2_000;
const TRICKLE_CHUNK = "x".repeat(8);
const TRICKLE_INTERVAL_MS = 20;
const TRICKLE_CHUNKS = 100;
const POLL_MS = 5;
// Deep enough to reach the proxy's frame beneath Node's own timer, event and async_hooks frames.
const CALLER_STACK_DEPTH = 50;
// The proxy's own source files, as they appear in a stack frame.
const PROXY_SOURCE = /[\\/](?:lib[\\/]upload-intake|app[\\/]api[\\/]evidence[\\/]route)\.ts:/;

const BASE_LIMITS = {
  MAX_EVIDENCE_FILE_BYTES: 1024,
  EVIDENCE_UPLOAD_ENVELOPE_ALLOWANCE_BYTES: 1024,
  EVIDENCE_UPLOAD_MAX_BODY_BYTES: 2048,
  EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS: CAP,
  EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS: RETRY_AFTER_SECONDS,
  EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS: GENEROUS_MS,
  EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS: GENEROUS_MS,
  EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS: GENEROUS_MS,
};
type Limits = typeof BASE_LIMITS;

/** One freshly loaded proxy, its intake module's slot count and slot taker, and the live limits both modules read. */
interface Proxy {
  POST: (request: NextRequest) => Promise<Response>;
  activeUploads: () => number;
  admitUpload: () => (() => void) | null;
  limits: Limits;
}

/** What became of one backend answer whose body stalls: how often it was read, and whether it was cancelled and why. */
interface StalledAnswer {
  pulls: number;
  cancelled: boolean;
  reason?: unknown;
}

/** A Node timer as async_hooks hands it over: its delay (until cleared), and _destroyed, set once it has fired or been cleared. */
interface NodeTimer {
  _idleTimeout?: number;
  _destroyed?: boolean;
}

/** One timer the proxy started, with the delay it was started with. */
interface ProxyTimer {
  timer: NodeTimer;
  delayMs?: number;
}

/** One abort listener the proxy attached, and the signal it attached it to. */
interface ProxyListener {
  target: EventTarget;
  listener: EventListenerOrEventListenerObject | null;
}

interface Call {
  url: string;
  init: RequestInit;
}

/** What a body probe has been asked for so far. */
interface BodyProbe {
  stream: ReadableStream<Uint8Array>;
  pulled: () => number;
  cancelled: () => boolean;
  finish: () => void;
}

/** How a probe body behaves once its chunks are handed out: end, fail as a lost client does, or wait for finish(). */
type BodyEnd = "close" | "fail" | "hold";

/** A promise that something else settles: a backend answer or a body that waits on the test. */
class Gate {
  private openGate: () => void = () => undefined;
  readonly opened = new Promise<void>((resolve) => {
    this.openGate = resolve;
  });

  open(): void {
    this.openGate();
  }
}

let calls: Call[];
let leavingClients: AbortController[];
let madeRequests: NextRequest[];
let arrivingBodies: BodyProbe[];
let stalledAnswers: StalledAnswer[];
let proxyTimers: ProxyTimer[];
let proxyListeners: ProxyListener[];
let sessionAnswer: (init: RequestInit) => Promise<Response>;
let operation: (init: RequestInit) => Promise<Response>;

/**
 * Whether the code that asked for a timer or listener is the proxy's own: the
 * nearest stack frame outside Node and the recorder that noticed it is in
 * upload-intake.ts or the route. That leaves out the test's own sleeps and
 * listeners, even those a body probe starts from inside one of the proxy's reads.
 */
function requestedByProxy(recorder: string): boolean {
  const depth = Error.stackTraceLimit;
  Error.stackTraceLimit = CALLER_STACK_DEPTH;
  const frames = (new Error().stack ?? "").split("\n").slice(1);
  Error.stackTraceLimit = depth;
  const creator = frames.find((frame) =>
    !frame.includes("node:") && !frame.includes(recorder) && !frame.includes("requestedByProxy"));
  return creator !== undefined && PROXY_SOURCE.test(creator);
}

/** Keep every Timeout the proxy created, through setTimeout and AbortSignal.timeout alike. */
function recordProxyTimer(_asyncId: number, type: string, _triggerAsyncId: number, resource: object): void {
  if (type !== "Timeout" || !requestedByProxy("recordProxyTimer")) return;
  const timer = resource as NodeTimer;
  proxyTimers.push({ timer, delayMs: timer._idleTimeout });
}

const timerWatch = createHook({ init: recordProxyTimer });
const attachListener = EventTarget.prototype.addEventListener;

/** Stand-in for EventTarget's addEventListener (AbortSignal inherits it) that keeps every abort listener the proxy attaches. */
function recordProxyListener(
  this: EventTarget,
  type: string,
  listener: EventListenerOrEventListenerObject | null,
  options?: AddEventListenerOptions | boolean,
): void {
  if (type === "abort" && requestedByProxy("recordProxyListener")) proxyListeners.push({ target: this, listener });
  attachListener.call(this, type, listener, options);
}

/**
 * Assert that nothing the proxy started outlives its answer: every timer it
 * created has fired or been cleared, every abort listener it attached, on any
 * signal, is gone, and no abort listener at all is left on any request the
 * test made or on any signal the proxy handed the backend.
 */
function expectNothingOutlives(): void {
  const pendingDelays = proxyTimers.filter(({ timer }) => !timer._destroyed).map(({ delayMs }) => delayMs);
  expect(pendingDelays).toEqual([]);
  const attached = proxyListeners.filter(({ target, listener }) =>
    getEventListeners(target, "abort").some((current) => current === listener));
  expect(attached).toHaveLength(0);
  const requestSignals = madeRequests.map((request) => request.signal);
  const backendSignals = calls.flatMap((call) => (call.init.signal ? [call.init.signal] : []));
  for (const signal of [...requestSignals, ...backendSignals]) {
    expect(getEventListeners(signal, "abort")).toHaveLength(0);
  }
}

/** Load fresh copies of the proxy route and its intake module, with these limits, in one new module registry. */
async function freshProxy(overrides: Partial<Limits> = {}): Promise<Proxy> {
  const limits = { ...BASE_LIMITS, ...overrides };
  jest.resetModules();
  jest.doMock("next/headers", () => ({ cookies: async () => ({ get: cookieGet }) }));
  jest.doMock("../lib/evidence-upload-limits", () => limits);
  const route = await import("../app/api/evidence/route");
  const intake = await import("../lib/upload-intake");
  return { POST: route.POST, activeUploads: intake.activeUploads, admitUpload: intake.admitUpload, limits };
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

/** Wait until a condition holds, failing the test if it does not within PROMPT_MS. */
async function until(condition: () => boolean, what: string): Promise<void> {
  const giveUpAt = Date.now() + PROMPT_MS;
  while (!condition()) {
    if (Date.now() > giveUpAt) throw new Error(`timed out waiting for ${what}`);
    await sleep(POLL_MS);
  }
}

function bytes(text: string): Uint8Array<ArrayBuffer> {
  return new TextEncoder().encode(text);
}

/** A small hand-built multipart body with a known boundary. */
function multipart(content = "synthetic evidence"): Uint8Array<ArrayBuffer> {
  return bytes(`--${BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="policy.txt"\r\n`
    + `Content-Type: text/plain\r\n\r\n${content}\r\n`
    + `--${BOUNDARY}\r\nContent-Disposition: form-data; name="record_type"\r\n\r\npolicy\r\n--${BOUNDARY}--\r\n`);
}

/**
 * A request body that hands out one chunk per read, each after intervalMs,
 * counts the reads and records whether it was cancelled; then it ends as told.
 */
function probeBody(chunks: Uint8Array[], end: BodyEnd, intervalMs = 0): BodyProbe {
  let pulled = 0;
  let cancelled = false;
  const finished = new Gate();
  const stream = new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (pulled < chunks.length) {
        if (intervalMs > 0) await sleep(intervalMs);
        if (cancelled) return;
        controller.enqueue(chunks[pulled]);
        pulled += 1;
      } else if (end === "fail") {
        controller.error(new Error("client disconnected"));
      } else {
        if (end === "hold") await finished.opened;
        if (!cancelled) controller.close();
      }
    },
    cancel() {
      cancelled = true;
      finished.open();
    },
  }, { highWaterMark: 0 });
  return { stream, pulled: () => pulled, cancelled: () => cancelled, finish: () => finished.open() };
}

/** An upload request; every one made is kept, so the test can check what is left attached to its signal. */
function upload(body: BodyInit | null, signal?: AbortSignal): NextRequest {
  const init = { method: "POST", body, duplex: "half", signal, headers: { origin: ORIGIN, "content-type": MULTIPART } };
  const made = new NextRequest(UPLOAD_URL, init as ConstructorParameters<typeof NextRequest>[1]);
  madeRequests.push(made);
  return made;
}

/** A backend request that never answers; like fetch, it rejects with the signal's reason once aborted. */
function neverAnswers(init: RequestInit): Promise<Response> {
  return new Promise((_resolve, reject) => {
    init.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
  });
}

/**
 * A response whose headers have arrived but whose JSON body stalls half-way:
 * the first read gets half the JSON and the second never completes, so two
 * reads mean the reader is waiting mid-body. Records the reads and the
 * body's cancellation.
 */
function stalledAnswer(status: number, record: StalledAnswer): Response {
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      record.pulls += 1;
      if (record.pulls === 1) controller.enqueue(bytes('{"detail": "half'));
    },
    cancel(reason) {
      record.cancelled = true;
      record.reason = reason;
    },
  }, { highWaterMark: 0 });
  return new Response(body, { status, headers: { "content-type": "application/json" } });
}

/** A fresh stalled answer, kept in stalledAnswers so the test can see what became of it. */
function nextStalledAnswer(status: number): Response {
  const record: StalledAnswer = { pulls: 0, cancelled: false };
  stalledAnswers.push(record);
  return stalledAnswer(status, record);
}

/** Wait until every one of count stalled answers has been read up to its stall. */
async function untilAnswersStall(count: number): Promise<void> {
  await until(() => stalledAnswers.length === count && stalledAnswers.every((answer) => answer.pulls >= 2),
    "the stalled answers to be read up to their stall");
}

function forwardedCalls(): Call[] {
  return calls.filter((call) => call.url !== SESSION_URL);
}

function sessionCalls(): Call[] {
  return calls.filter((call) => call.url === SESSION_URL);
}

/** Restore the backend double's normal answers: a verified compliance lead, and every upload stored. */
function answerNormally(): void {
  sessionAnswer = async () => Response.json({ email: "u@example.test", role: "compliance_lead" });
  operation = async () => Response.json(STORED, { status: 201 });
}

/** Make the backend hold every upload until the returned gate opens, then store it. */
function holdBackend(): Gate {
  const gate = new Gate();
  operation = async () => {
    await gate.opened;
    return Response.json(STORED, { status: 201 });
  };
  return gate;
}

/**
 * Prove the cap is whole: CAP overlapping uploads are all admitted and held
 * at the backend, and one more is refused at once with nothing read. A slot
 * that never came back would refuse one of the first CAP; one returned twice
 * would admit the extra one.
 */
async function expectWholeCap(proxy: Proxy): Promise<void> {
  const already = forwardedCalls().length;
  const gate = holdBackend();
  const held = Array.from({ length: CAP }, () => proxy.POST(upload(multipart())));
  await until(() => forwardedCalls().length === already + CAP, "the held uploads to reach the backend");
  const extra = probeBody([multipart()], "close");
  const refused = await proxy.POST(upload(extra.stream));
  expect(refused.status).toBe(503);
  expect(extra.pulled()).toBe(0);
  gate.open();
  for (const response of await Promise.all(held)) expect(response.status).toBe(201);
  expect(proxy.activeUploads()).toBe(0);
}

beforeEach(() => {
  jest.replaceProperty(process, "env", { ...process.env,
    KERNO_TRUSTED_ORIGINS: ORIGIN, KERNO_API_URL: "http://backend.test" });
  cookieGet.mockReturnValue({ value: "synthetic-session" });
  calls = [];
  leavingClients = [];
  madeRequests = [];
  arrivingBodies = [];
  stalledAnswers = [];
  proxyTimers = [];
  proxyListeners = [];
  answerNormally();
  global.fetch = jest.fn().mockImplementation(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    return url === SESSION_URL ? sessionAnswer(init) : operation(init);
  });
  timerWatch.enable();
  EventTarget.prototype.addEventListener = recordProxyListener;
});
afterEach(() => {
  timerWatch.disable();
  EventTarget.prototype.addEventListener = attachListener;
  jest.restoreAllMocks();
  expectNothingOutlives();
});

describe("POST /api/evidence — overlapping uploads share a per-process cap", () => {
  it("refuses one more upload with a 503 at once while the cap is held at the backend, reading none of its body", async () => {
    const proxy = await freshProxy();
    const gate = holdBackend();
    const held = [proxy.POST(upload(multipart())), proxy.POST(upload(multipart()))];
    await until(() => forwardedCalls().length === CAP, "both uploads to reach the backend");
    expect(proxy.activeUploads()).toBe(CAP);
    const extra = probeBody([multipart()], "close");
    const startedAt = Date.now();
    const refused = await proxy.POST(upload(extra.stream));
    expect(Date.now() - startedAt).toBeLessThan(PROMPT_MS);
    expect(refused.status).toBe(503);
    expect(refused.headers.get("retry-after")).toBe(String(RETRY_AFTER_SECONDS));
    expect(refused.headers.get("connection")).toBe("close");
    expect(await refused.json()).toEqual(BUSY);
    expect(extra.pulled()).toBe(0);
    expect(forwardedCalls()).toHaveLength(CAP);
    gate.open();
    for (const response of await Promise.all(held)) expect(response.status).toBe(201);
    expect(proxy.activeUploads()).toBe(0);
    expect((await proxy.POST(upload(multipart()))).status).toBe(201);
  });

  it("counts uploads whose bodies are still arriving, and admits again once they finish", async () => {
    const proxy = await freshProxy();
    const arriving = [probeBody([multipart()], "hold"), probeBody([multipart()], "hold")];
    const held = arriving.map((probe) => proxy.POST(upload(probe.stream)));
    await until(() => arriving.every((probe) => probe.pulled() === 1), "both bodies to start arriving");
    expect(proxy.activeUploads()).toBe(CAP);
    const extra = probeBody([multipart()], "close");
    const refused = await proxy.POST(upload(extra.stream));
    expect(refused.status).toBe(503);
    expect(extra.pulled()).toBe(0);
    expect(forwardedCalls()).toEqual([]);
    for (const probe of arriving) probe.finish();
    for (const response of await Promise.all(held)) expect(response.status).toBe(201);
    expect(proxy.activeUploads()).toBe(0);
    expect((await proxy.POST(upload(multipart()))).status).toBe(201);
  });

  it("gives a slot back once however often its release is called", async () => {
    const proxy = await freshProxy();
    const release = proxy.admitUpload();
    expect(release).not.toBeNull();
    release?.();
    release?.();
    expect(proxy.activeUploads()).toBe(0);
    const taken = Array.from({ length: CAP }, () => proxy.admitUpload());
    expect(taken.every((slot) => slot !== null)).toBe(true);
    expect(proxy.admitUpload()).toBeNull();
    for (const slot of taken) slot?.();
    expect(proxy.activeUploads()).toBe(0);
  });

  it("verifies the session and role of a refused upload before refusing it as busy", async () => {
    const proxy = await freshProxy();
    const gate = holdBackend();
    const held = [proxy.POST(upload(multipart())), proxy.POST(upload(multipart()))];
    await until(() => forwardedCalls().length === CAP, "both uploads to reach the backend");
    sessionAnswer = async () => Response.json({ email: "u@example.test", role: "auditor" });
    const refused = await proxy.POST(upload(multipart()));
    expect(refused.status).toBe(403);
    expect(sessionCalls()).toHaveLength(CAP + 1);
    gate.open();
    await Promise.all(held);
    expect(proxy.activeUploads()).toBe(0);
  });
});

describe("POST /api/evidence — every outcome gives its slot back, and the cap still holds", () => {
  /**
   * One way an upload can end. start() begins one upload; meanwhile() runs
   * once all CAP have begun; verify() checks what was aborted or cancelled.
   */
  interface Scenario {
    label: string;
    limits: Partial<Limits>;
    status: number;
    start: (proxy: Proxy) => Promise<Response>;
    meanwhile?: () => Promise<void>;
    verify?: () => void;
  }

  /** Begin one upload whose client can be made to leave later. */
  function leavingUpload(proxy: Proxy, body: BodyInit = multipart()): Promise<Response> {
    const client = new AbortController();
    leavingClients.push(client);
    return proxy.POST(upload(body, client.signal));
  }

  /** Make every client that began a leaving upload go away. */
  function clientsLeave(): void {
    for (const client of leavingClients) client.abort();
  }

  function expectAllAborted(made: Call[], reason?: string): void {
    expect(made).toHaveLength(CAP);
    for (const call of made) {
      expect(call.init.signal?.aborted).toBe(true);
      if (reason) expect((call.init.signal?.reason as DOMException).name).toBe(reason);
    }
  }

  const scenarios: Scenario[] = [
    { label: "a stored upload", limits: {}, status: 201, start: (proxy) => proxy.POST(upload(multipart())) },
    {
      label: "a backend refusal", limits: {}, status: 422,
      start: (proxy) => {
        operation = async () => Response.json({ detail: "the PDF could not be read" }, { status: 422 });
        return proxy.POST(upload(multipart()));
      },
    },
    {
      label: "a session-check timeout", limits: { EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS: SHORT_MS }, status: 502,
      start: (proxy) => {
        sessionAnswer = neverAnswers;
        return proxy.POST(upload(multipart()));
      },
      verify: () => {
        expectAllAborted(sessionCalls(), "TimeoutError");
        expect(forwardedCalls()).toEqual([]);
      },
    },
    {
      label: "a client that leaves during the session check", limits: {}, status: 502,
      start: (proxy) => {
        sessionAnswer = neverAnswers;
        return leavingUpload(proxy);
      },
      meanwhile: async () => {
        await until(() => sessionCalls().length === CAP, "both session checks to start");
        clientsLeave();
      },
      verify: () => {
        expectAllAborted(sessionCalls());
        expect(forwardedCalls()).toEqual([]);
      },
    },
    {
      label: "a client that leaves while the session answer is arriving", limits: {}, status: 502,
      start: (proxy) => {
        sessionAnswer = async () => nextStalledAnswer(200);
        return leavingUpload(proxy);
      },
      meanwhile: async () => {
        await untilAnswersStall(CAP);
        clientsLeave();
      },
      verify: () => {
        for (const answer of stalledAnswers) expect(answer.cancelled).toBe(true);
        expect(forwardedCalls()).toEqual([]);
      },
    },
    {
      label: "a reception timeout", limits: { EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS: SHORT_MS }, status: 408,
      start: (proxy) => proxy.POST(upload(probeBody([bytes("--")], "hold").stream)),
      verify: () => expect(forwardedCalls()).toEqual([]),
    },
    {
      // The body neither ends nor fails, so only the client's departure can stop
      // the read: a 400 now, not the 408 GENEROUS_MS later.
      label: "a client that leaves while its body is still arriving", limits: {}, status: 400,
      start: (proxy) => {
        const body = probeBody([bytes("--")], "hold");
        arrivingBodies.push(body);
        return leavingUpload(proxy, body.stream);
      },
      meanwhile: async () => {
        await until(() => arrivingBodies.length === CAP && arrivingBodies.every((body) => body.pulled() === 1),
          "both bodies to start arriving");
        clientsLeave();
      },
      verify: () => {
        for (const body of arrivingBodies) expect(body.cancelled()).toBe(true);
        expect(forwardedCalls()).toEqual([]);
      },
    },
    {
      label: "an over-limit body", limits: {}, status: 413,
      start: (proxy) => proxy.POST(upload(probeBody([new Uint8Array(BASE_LIMITS.EVIDENCE_UPLOAD_MAX_BODY_BYTES + 1)], "close").stream)),
    },
    {
      label: "a client that leaves mid-body", limits: {}, status: 400,
      start: (proxy) => proxy.POST(upload(probeBody([bytes("--")], "fail").stream)),
    },
    {
      label: "a forwarding timeout", limits: { EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS: SHORT_MS }, status: 504,
      start: (proxy) => {
        operation = neverAnswers;
        return proxy.POST(upload(multipart()));
      },
      verify: () => expectAllAborted(forwardedCalls(), "TimeoutError"),
    },
    {
      label: "a client that leaves mid-forward", limits: {}, status: 400,
      start: (proxy) => {
        operation = neverAnswers;
        return leavingUpload(proxy);
      },
      meanwhile: async () => {
        await until(() => forwardedCalls().length === CAP, "both uploads to reach the backend");
        clientsLeave();
      },
      verify: () => expectAllAborted(forwardedCalls()),
    },
    {
      label: "a client that leaves while the backend's answer is arriving", limits: {}, status: 400,
      start: (proxy) => {
        operation = async () => nextStalledAnswer(201);
        return leavingUpload(proxy);
      },
      meanwhile: async () => {
        await untilAnswersStall(CAP);
        clientsLeave();
      },
      verify: () => {
        expectAllAborted(forwardedCalls());
        for (const answer of stalledAnswers) expect(answer.cancelled).toBe(true);
      },
    },
  ];

  it.each(scenarios)("after $label ($status)", async ({ limits, status, start, meanwhile, verify }) => {
    const proxy = await freshProxy(limits);
    const pending = Array.from({ length: CAP }, () => start(proxy));
    await meanwhile?.();
    const outcomes = await Promise.all(pending);
    for (const response of outcomes) expect(response.status).toBe(status);
    verify?.();
    expectNothingOutlives();
    expect(proxy.activeUploads()).toBe(0);
    Object.assign(proxy.limits, BASE_LIMITS);
    answerNormally();
    await expectWholeCap(proxy);
  });

  /** A backend answer whose headers arrived but whose body fails part-way, as a reset connection fails it. */
  function cutOffAnswer(status: number, headers: Record<string, string> = {}): Response {
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes('{"detail": "evid'));
        controller.error(new TypeError("terminated"));
      },
    });
    return new Response(body, { status, headers: { "content-type": "application/json", ...headers } });
  }

  it.each([
    ["an unreachable backend", 502, async (): Promise<Response> => {
      throw new TypeError("fetch failed", { cause: new Error("read ECONNRESET") });
    }],
    ["a stored answer cut off mid-body", 502, async () => cutOffAnswer(201)],
    ["a busy 503 cut off mid-body", 503, async () => cutOffAnswer(503, { "Retry-After": String(RETRY_AFTER_SECONDS) })],
  ])("after a broken connection to the backend (%s) the answer is a controlled %i", async (_label, status, failure) => {
    const proxy = await freshProxy();
    operation = failure;
    const outcomes = await Promise.all(Array.from({ length: CAP }, () => proxy.POST(upload(multipart()))));
    for (const response of outcomes) {
      expect(response.status).toBe(status);
      expect(typeof (await response.json()).detail).toBe("string");
    }
    expect(forwardedCalls()).toHaveLength(CAP);
    expectNothingOutlives();
    expect(proxy.activeUploads()).toBe(0);
    answerNormally();
    await expectWholeCap(proxy);
  });

  it("answers a whole backend answer that is not JSON with a controlled 502, never a thrown 500", async () => {
    const proxy = await freshProxy();
    operation = async () => new Response("<html>bad gateway</html>", { status: 502 });
    const outcomes = await Promise.all(Array.from({ length: CAP }, () => proxy.POST(upload(multipart()))));
    for (const response of outcomes) {
      expect(response.status).toBe(502);
      expect(await response.json()).toEqual({ detail: "the evidence service's answer could not be read" });
    }
    expect(forwardedCalls()).toHaveLength(CAP);
    expectNothingOutlives();
    expect(proxy.activeUploads()).toBe(0);
    answerNormally();
    await expectWholeCap(proxy);
  });
});

describe("POST /api/evidence — nothing the proxy starts outlives its answer", () => {
  it("runs the session check, the reception and the forward each under its own deadline, all cleared once answered", async () => {
    const deadlines = {
      EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS: GENEROUS_MS + 1,
      EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS: GENEROUS_MS + 2,
      EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS: GENEROUS_MS + 3,
    };
    const proxy = await freshProxy(deadlines);
    const request = upload(multipart());
    const response = await proxy.POST(request);
    expect(response.status).toBe(201);
    expect(proxyTimers.map(({ delayMs }) => delayMs)).toEqual(Object.values(deadlines));
    expect(proxyListeners.filter(({ target }) => target === request.signal)).toHaveLength(Object.keys(deadlines).length);
    expectNothingOutlives();
    expect(proxy.activeUploads()).toBe(0);
  });
});

describe("POST /api/evidence — the body must arrive within an absolute deadline", () => {
  it("refuses a body that stops arriving with a 408 and reads no further", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS: SHORT_MS });
    const stalled = probeBody([bytes(`--${BOUNDARY}\r\n`)], "hold");
    const startedAt = Date.now();
    const response = await proxy.POST(upload(stalled.stream));
    const elapsed = Date.now() - startedAt;
    expect(response.status).toBe(408);
    expect(response.headers.get("connection")).toBe("close");
    expect(await response.json()).toEqual(LATE_BODY);
    expect(elapsed).toBeGreaterThanOrEqual(EARLIEST_MS);
    expect(elapsed).toBeLessThan(PROMPT_MS);
    expect(stalled.cancelled()).toBe(true);
    expect(stalled.pulled()).toBe(1);
    expect(forwardedCalls()).toEqual([]);
    expect(proxy.activeUploads()).toBe(0);
  });

  it("counts the wait for the first byte against the deadline", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS: SHORT_MS });
    const silent = probeBody([], "hold");
    const response = await proxy.POST(upload(silent.stream));
    expect(response.status).toBe(408);
    expect(silent.cancelled()).toBe(true);
    expect(forwardedCalls()).toEqual([]);
  });

  it("is not reset by a steady trickle of bytes, each well inside the deadline", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS: SHORT_MS });
    const chunks = Array.from({ length: TRICKLE_CHUNKS }, () => bytes(TRICKLE_CHUNK));
    const trickle = probeBody(chunks, "close", TRICKLE_INTERVAL_MS);
    const startedAt = Date.now();
    const response = await proxy.POST(upload(trickle.stream));
    const elapsed = Date.now() - startedAt;
    expect(response.status).toBe(408);
    expect(await response.json()).toEqual(LATE_BODY);
    expect(elapsed).toBeGreaterThanOrEqual(EARLIEST_MS);
    expect(elapsed).toBeLessThan(TRICKLE_CHUNKS * TRICKLE_INTERVAL_MS);
    expect(trickle.cancelled()).toBe(true);
    const pulledAtRefusal = trickle.pulled();
    expect(pulledAtRefusal).toBeGreaterThan(1);
    expect(pulledAtRefusal).toBeLessThan(TRICKLE_CHUNKS);
    await sleep(TRICKLE_INTERVAL_MS * 5);
    expect(trickle.pulled()).toBe(pulledAtRefusal);
    expect(forwardedCalls()).toEqual([]);
    expect(proxy.activeUploads()).toBe(0);
  });

  it("forwards a slow body that completes inside the deadline, byte for byte", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_RECEPTION_DEADLINE_MS: GENEROUS_MS });
    const body = multipart();
    const pieces = Array.from({ length: Math.ceil(body.byteLength / TRICKLE_CHUNK.length) },
      (_unused, index) => body.slice(index * TRICKLE_CHUNK.length, (index + 1) * TRICKLE_CHUNK.length));
    const response = await proxy.POST(upload(probeBody(pieces, "close", 1).stream));
    expect(response.status).toBe(201);
    const forwarded = forwardedCalls()[0].init;
    expect(new Headers(forwarded.headers).get("content-type")).toBe(MULTIPART);
    expect(new Uint8Array(await new Response(forwarded.body as BodyInit).arrayBuffer())).toEqual(body);
  });
});

describe("POST /api/evidence — the session check has a deadline", () => {
  it("answers 502 when the session check never answers, aborting it, before any body byte is read", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS: SHORT_MS });
    sessionAnswer = neverAnswers;
    const body = probeBody([multipart()], "close");
    const startedAt = Date.now();
    const response = await proxy.POST(upload(body.stream));
    const elapsed = Date.now() - startedAt;
    expect(response.status).toBe(502);
    expect(response.headers.get("connection")).toBe("close");
    expect(await response.json()).toEqual(UNVERIFIED);
    expect(elapsed).toBeGreaterThanOrEqual(EARLIEST_MS);
    expect(elapsed).toBeLessThan(PROMPT_MS);
    const signal = sessionCalls()[0].init.signal;
    expect(signal?.aborted).toBe(true);
    expect((signal?.reason as DOMException).name).toBe("TimeoutError");
    expect(body.pulled()).toBe(0);
    expect(forwardedCalls()).toEqual([]);
    expect(proxy.activeUploads()).toBe(0);
  });

  it("answers 502 when the session answer's body stalls, cancelling that body", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_SESSION_CHECK_DEADLINE_MS: SHORT_MS });
    sessionAnswer = async () => nextStalledAnswer(200);
    const body = probeBody([multipart()], "close");
    const response = await proxy.POST(upload(body.stream));
    expect(response.status).toBe(502);
    expect(stalledAnswers).toHaveLength(1);
    expect(stalledAnswers[0].pulls).toBe(2);
    expect((stalledAnswers[0].reason as DOMException).name).toBe("TimeoutError");
    expect(body.pulled()).toBe(0);
    expect(forwardedCalls()).toEqual([]);
    expectNothingOutlives();
  });
});

describe("POST /api/evidence — the forward to the backend has a deadline", () => {
  it("answers 504 when the backend never answers, aborting the backend request", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS: SHORT_MS });
    operation = neverAnswers;
    const startedAt = Date.now();
    const response = await proxy.POST(upload(multipart()));
    const elapsed = Date.now() - startedAt;
    expect(response.status).toBe(504);
    expect(await response.json()).toEqual(LATE_BACKEND);
    expect(elapsed).toBeGreaterThanOrEqual(EARLIEST_MS);
    expect(elapsed).toBeLessThan(PROMPT_MS);
    const signal = forwardedCalls()[0].init.signal;
    expect(signal?.aborted).toBe(true);
    expect((signal?.reason as DOMException).name).toBe("TimeoutError");
    expect(proxy.activeUploads()).toBe(0);
  });

  it("answers 504 when the backend's answer stalls after its headers, cancelling that body", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS: SHORT_MS });
    operation = async () => nextStalledAnswer(201);
    const response = await proxy.POST(upload(multipart()));
    expect(response.status).toBe(504);
    expect(await response.json()).toEqual(LATE_BACKEND);
    expect(stalledAnswers).toHaveLength(1);
    expect(stalledAnswers[0].pulls).toBe(2);
    expect((stalledAnswers[0].reason as DOMException).name).toBe("TimeoutError");
    expect(proxy.activeUploads()).toBe(0);
    expectNothingOutlives();
  });

  it("still relays a slow backend answer that arrives inside the deadline", async () => {
    const proxy = await freshProxy({ EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS: GENEROUS_MS });
    operation = async () => {
      await sleep(SHORT_MS);
      return Response.json({ detail: "the PDF has more than 500 pages" }, { status: 422 });
    };
    const response = await proxy.POST(upload(multipart()));
    expect(response.status).toBe(422);
    expect(await response.json()).toEqual({ detail: "the PDF has more than 500 pages" });
  });
});
