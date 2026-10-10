/**
 * @jest-environment jsdom
 *
 * __tests__/evidence-upload.test.tsx — the upload form's size check and failure recovery (SEC-REMED-003).
 *
 * What:  a file over the shared limit is refused before any request; a file at
 *        the limit is posted with its record type; an interrupted connection,
 *        or a response whose body cannot be read or decoded, leaves the form
 *        usable again instead of stuck in "Uploading…". An unreadable success
 *        response is reported as unconfirmed, never as stored or as failed,
 *        and is not retried.
 * Why:   the proxy closes the connection on an over-limit body, which a
 *        browser may surface as a network error rather than the 413.
 * How:   npm test -- evidence-upload; mocked limits, router and fetch.
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import EvidenceUpload from "@/components/EvidenceUpload";

jest.mock("next/navigation", () => ({ useRouter: () => ({ refresh: jest.fn(), push: jest.fn() }) }));
jest.mock("../lib/evidence-upload-limits", () => ({ MAX_EVIDENCE_FILE_BYTES: 16 }));

const TEST_FILE_LIMIT = 16;
const SETTLE_MS = 50;
const UNCONFIRMED = "The upload's result could not be read, so it is unconfirmed. "
  + "Reload the evidence list before uploading this file again.";

/** A fetch result whose body read or JSON decoding rejects with the given error. */
function unreadable(ok: boolean, status: number, failure: Error) {
  return { ok, status, json: async () => { throw failure; } };
}

function choose(content: string, name = "policy.txt"): void {
  const file = new File([content], name, { type: "text/plain" });
  fireEvent.change(screen.getByLabelText("Evidence file"), { target: { files: [file] } });
}

beforeEach(() => {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    status: 201,
    json: async () => ({ title: "policy.txt", deduplicated: false }),
  });
});
afterEach(() => jest.restoreAllMocks());

it("refuses a file over the limit without sending a request", async () => {
  render(<EvidenceUpload />);
  choose("x".repeat(TEST_FILE_LIMIT + 1));
  fireEvent.click(screen.getByRole("button", { name: "Upload" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("That file is too large.");
  expect(global.fetch).not.toHaveBeenCalled();
});

it("posts a file at the limit with its record type", async () => {
  const onUploaded = jest.fn();
  render(<EvidenceUpload onUploaded={onUploaded} />);
  choose("x".repeat(TEST_FILE_LIMIT));
  fireEvent.click(screen.getByRole("button", { name: "Upload" }));
  await waitFor(() => expect(onUploaded).toHaveBeenCalledWith('Uploaded "policy.txt".'));
  const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
  expect(url).toBe("/api/evidence");
  expect((init.body as FormData).get("record_type")).toBe("policy");
});

it("recovers from an interrupted connection", async () => {
  global.fetch = jest.fn().mockRejectedValue(new TypeError("Failed to fetch"));
  render(<EvidenceUpload />);
  choose("synthetic evidence".slice(0, TEST_FILE_LIMIT));
  fireEvent.click(screen.getByRole("button", { name: "Upload" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Upload failed: the connection was interrupted.");
  expect(screen.getByRole("button", { name: "Upload" })).toBeEnabled();
});

it.each([
  ["JSON decoding fails", new SyntaxError("Unexpected end of JSON input")],
  ["the body cannot be read", new TypeError("network error")],
])("reports a successful status as unconfirmed when %s, without retrying", async (_label, failure) => {
  global.fetch = jest.fn().mockResolvedValue(unreadable(true, 201, failure));
  const onUploaded = jest.fn();
  render(<EvidenceUpload onUploaded={onUploaded} />);
  choose("x".repeat(TEST_FILE_LIMIT));
  fireEvent.click(screen.getByRole("button", { name: "Upload" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(UNCONFIRMED);
  await new Promise((settled) => setTimeout(settled, SETTLE_MS));
  expect(screen.getByRole("button", { name: "Upload" })).toBeEnabled();
  expect(onUploaded).not.toHaveBeenCalled();
  expect(screen.getByText("Selected: policy.txt")).toBeInTheDocument();
  expect(global.fetch).toHaveBeenCalledTimes(1);
});

it("restores the button when a refused upload's body cannot be read", async () => {
  global.fetch = jest.fn().mockResolvedValue(unreadable(false, 500, new SyntaxError("Unexpected token <")));
  render(<EvidenceUpload />);
  choose("x".repeat(TEST_FILE_LIMIT));
  fireEvent.click(screen.getByRole("button", { name: "Upload" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Upload failed: 500");
  expect(screen.getByRole("button", { name: "Upload" })).toBeEnabled();
  expect(global.fetch).toHaveBeenCalledTimes(1);
});
