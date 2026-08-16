/**
 * @jest-environment jsdom
 *
 * __tests__/submissions-view.test.tsx — filing windows, starting a run, role gating (KER-411).
 *
 * What:  windows and history render, the start-run POST carries only the
 *        window id, an auditor gets no button but keeps the whole view, and
 *        the empty states read as normal rather than broken.
 * Why:   the POST body is the load-bearing assertion — tenant and actor come
 *        from the session JWT server-side, and anything the client sent would
 *        be either ignored or a way to file against someone else's register.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import SubmissionsView from "@/components/SubmissionsView";
import type { SubmissionRun, SubmissionWindow } from "@/lib/api";

const refresh = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push: jest.fn() }),
}));

function window_(overrides: Partial<SubmissionWindow> = {}): SubmissionWindow {
  return {
    id: "win-1",
    authority_code: "EBA",
    reporting_year: 2031,
    register_reference_date: "2031-03-31",
    window_open_date: "2031-04-01",
    window_close_date: "2031-04-30",
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
    ...overrides,
  };
}

function run_(overrides: Partial<SubmissionRun> = {}): SubmissionRun {
  return {
    id: "run-1",
    tenant_id: "tenant-1",
    submission_window_id: "win-1",
    reporting_year: 2031,
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

describe("SubmissionsView", () => {
  beforeEach(() => {
    refresh.mockClear();
  });

  it("lists open windows and the run history", () => {
    render(<SubmissionsView windows={[window_()]} runs={[run_()]} readOnly={false} />);

    expect(screen.getByText("EBA · 2031")).toBeInTheDocument();
    expect(screen.getByText(/Open 2031-04-01 to 2031-04-30/)).toBeInTheDocument();
    expect(screen.getByText("2031")).toHaveAttribute(
      "href",
      "/dashboard/submissions/run-1",
    );
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByText("fail")).toBeInTheDocument();
  });

  it("posts only the window id when starting a run", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => run_({ id: "run-new", status: "draft", entry_count: 0 }),
    } as Response);

    render(<SubmissionsView windows={[window_()]} runs={[]} readOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("/api/submissions/runs");
    expect(init.method).toBe("POST");
    const sent = JSON.parse(init.body);
    expect(sent).toEqual({ submission_window_id: "win-1" });
    // Nothing about who is filing, or for whom — both come from the JWT.
    expect(sent).not.toHaveProperty("tenant_id");
    expect(sent).not.toHaveProperty("actor_id");
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("treats an empty-register run as recorded, not as a failure", async () => {
    // status=draft, validation=fail, entry_count=0 is the ROI_000 outcome and a
    // perfectly valid run — the attempt is what gets recorded.
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => run_({ status: "draft", validation_overall_status: "fail", entry_count: 0 }),
    } as Response);

    render(<SubmissionsView windows={[window_()]} runs={[]} readOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Run recorded/));
    expect(screen.getByRole("status")).toHaveTextContent(/0 entries/);
  });

  it("offers to re-run a window that already has a run, and says it updates in place", () => {
    render(<SubmissionsView windows={[window_()]} runs={[run_()]} readOnly={false} />);

    expect(screen.getByRole("button", { name: "Run again" })).toBeInTheDocument();
    expect(screen.getByText(/Running again updates it in place/)).toBeInTheDocument();
  });

  it("hides the start-run action from an auditor but keeps windows and history", () => {
    render(<SubmissionsView windows={[window_()]} runs={[run_()]} readOnly={true} />);

    expect(screen.queryByRole("button", { name: /start run|run again/i })).not.toBeInTheDocument();
    expect(screen.getByText("EBA · 2031")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
  });

  it("says no window is open without implying something is broken", () => {
    render(<SubmissionsView windows={[]} runs={[]} readOnly={false} />);

    expect(screen.getByText(/No filing window is open today/)).toBeInTheDocument();
    expect(screen.getByText(/Kerno does not create them/)).toBeInTheDocument();
    expect(screen.getByText(/No submission runs yet/)).toBeInTheDocument();
  });

  it("renders a validation-error body that FastAPI returns as a list", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({ detail: [{ msg: "Field required", loc: ["body", "submission_window_id"] }] }),
    } as Response);

    render(<SubmissionsView windows={[window_()]} runs={[]} readOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    // Not "[object Object]" — FastAPI's own 422 detail is an array of objects.
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/Field required/));
  });
});
