/**
 * @jest-environment jsdom
 *
 * __tests__/evidence-upload.test.tsx — the upload form's size check and failure recovery (SEC-REMED-003).
 *
 * What:  a file over the shared limit is refused before any request; a file at
 *        the limit is posted with its record type; an interrupted connection
 *        leaves the form usable again instead of stuck in "Uploading…".
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
