/**
 * @jest-environment jsdom
 *
 * __tests__/filing-download.test.tsx — frozen DORA filing download on the run page.
 *
 * What:  a permitted role downloads through the same-origin proxy; a 404 is
 *        "unavailable — start a new run" rather than a fake success; an
 *        auditor sees the run and no button.
 * Why:   the file must be the freeze stored at Start-run. Hiding the button
 *        is UX; the 403 is the guarantee.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import FilingDownloadButton from "@/components/FilingDownloadButton";
import SubmissionRunView from "@/components/SubmissionRunView";
import type { SubmissionRun } from "@/lib/api";

function run_(overrides: Partial<SubmissionRun> = {}): SubmissionRun {
  return {
    id: "run-1",
    tenant_id: "tenant-1",
    submission_window_id: "win-1",
    reporting_year: 2032,
    status: "draft",
    validation_overall_status: "fail",
    validation_issue_count: 1,
    entry_count: 0,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T10:00:00Z",
    submitted_at: null,
    submission_reference: null,
    ...overrides,
  };
}

describe("SubmissionRunView", () => {
  it("offers the download to a filing role", () => {
    render(<SubmissionRunView run={run_()} canDownload={true} />);
    expect(screen.getByRole("button", { name: "Download filing" })).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
  });

  it("hides the download from an auditor but keeps the run record", () => {
    render(<SubmissionRunView run={run_()} canDownload={false} />);
    expect(screen.queryByRole("button", { name: /download filing/i })).not.toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Submission run · 2032/ })).toBeInTheDocument();
  });
});

describe("FilingDownloadButton", () => {
  const click = jest.fn();

  beforeEach(() => {
    click.mockClear();
    global.URL.createObjectURL = jest.fn().mockReturnValue("blob:filing");
    global.URL.revokeObjectURL = jest.fn();
    const original = document.createElement.bind(document);
    jest.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const element = original(tag);
      if (tag === "a") {
        element.click = click;
      }
      return element;
    });
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("saves the attachment from the same-origin proxy", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      blob: async () => new Blob(["{\"frozen\":true}"], { type: "application/json" }),
      headers: { get: (name: string) => (name === "content-disposition" ? 'attachment; filename="kerno-dora-filing-2032-run-1.json"' : null) },
    } as unknown as Response);

    render(<FilingDownloadButton runId="run-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Download filing" }));

    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(global.fetch).toHaveBeenCalledWith("/api/submissions/runs/run-1/package");
    expect(global.URL.createObjectURL).toHaveBeenCalled();
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith("blob:filing");
  });

  it("says the filing is unavailable when the freeze is missing", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ detail: "entry not found" }),
    } as Response);

    render(<FilingDownloadButton runId="run-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Download filing" }));

    await waitFor(() => expect(screen.getByText(/Filing unavailable — start a new run/)).toBeInTheDocument());
    expect(click).not.toHaveBeenCalled();
  });

  it("does not pretend a 403 was a download", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ detail: "your role is not permitted to perform this action" }),
    } as Response);

    render(<FilingDownloadButton runId="run-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Download filing" }));

    await waitFor(() => expect(screen.getByText("Download failed (403).")).toBeInTheDocument());
    expect(click).not.toHaveBeenCalled();
  });
});
