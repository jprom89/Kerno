/**
 * @jest-environment jsdom
 *
 * __tests__/control-list.test.tsx — the per-control Analyse action (KER-402).
 *
 * What:  a permitted role gets one Analyse button per row that POSTs that
 *        row's control_id; a role outside GENERATE_ROLES gets no button at
 *        all; a backend failure surfaces as an error, never a fake success;
 *        and a degraded (template) run says so.
 * Why:   the button is a thin wire to an engine that already exists — the
 *        thing worth guarding is that it stays thin: one control per click,
 *        nothing invented client-side, every backend answer shown honestly.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import ControlList from "@/components/ControlList";
import type { CoverageControl } from "@/lib/api";

const refresh = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push: jest.fn() }),
}));

function control(overrides: Partial<CoverageControl> = {}): CoverageControl {
  return {
    control_id: "c4020000-0000-4000-a000-000000000001",
    control_ref: "NIS2-21.2a",
    title: "Risk analysis policy",
    category: "governance",
    framework: "NIS2",
    status: "gap",
    status_source: "none",
    human_confirmed: false,
    confidence_level: null,
    confidence_score: null,
    evidence_count: 0,
    ...overrides,
  };
}

function generated(overrides: Record<string, unknown> = {}) {
  return {
    recommendation_id: "r4020000-aaaa-4aaa-8aaa-000000000001",
    control_id: "c4020000-0000-4000-a000-000000000001",
    status: "partial",
    confidence_level: "medium",
    confidence_score: 0.5,
    rationale: "Some prose.",
    rationale_source: "llm",
    evidence_ids: [],
    requires_review: false,
    generated_at: "2026-08-18T00:00:00Z",
    ...overrides,
  };
}

describe("ControlList Analyse", () => {
  beforeEach(() => {
    refresh.mockClear();
  });

  it("posts exactly that row's control_id to the generate proxy", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      status: 201,
      json: async () => generated(),
    } as Response);

    render(
      <ControlList
        controls={[
          control(),
          control({ control_id: "c4020000-0000-4000-a000-000000000002", control_ref: "NIS2-21.2b" }),
        ]}
        canGenerate={true}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: "Analyse" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[1]);

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("/api/recommendations/generate");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({
      control_id: "c4020000-0000-4000-a000-000000000002",
    });
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("reports status, confidence, and rationale source on success", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      status: 201,
      json: async () => generated({ rationale_source: "template" }),
    } as Response);

    render(<ControlList controls={[control()]} canGenerate={true} />);
    fireEvent.click(screen.getByRole("button", { name: "Analyse" }));

    // A template rationale means the LLM was unavailable; the toast must not
    // present that run as identical to a full one.
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent(/partial/));
    expect(screen.getByRole("status")).toHaveTextContent(/medium confidence/);
    expect(screen.getByRole("status")).toHaveTextContent(/rationale: template/);
  });

  it("shows no Analyse button to a role outside GENERATE_ROLES", () => {
    render(<ControlList controls={[control()]} canGenerate={false} />);

    expect(screen.queryByRole("button", { name: /analyse/i })).not.toBeInTheDocument();
    // The table itself stays fully visible — read-only, not reduced.
    expect(screen.getByText("NIS2-21.2a")).toBeInTheDocument();
  });

  it("offers no analyse-all: one button per control and nothing else", () => {
    render(<ControlList controls={[control(), control({ control_id: "x2" })]} canGenerate={true} />);

    expect(screen.getAllByRole("button")).toHaveLength(2);
  });

  it("surfaces a 404 as an error, not a fake success", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      status: 404,
      json: async () => ({ detail: "entry not found" }),
    } as Response);

    render(<ControlList controls={[control()]} canGenerate={true} />);
    fireEvent.click(screen.getByRole("button", { name: "Analyse" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/Analysis failed \(404\): entry not found/),
    );
    expect(refresh).not.toHaveBeenCalled();
  });

  it("surfaces a 403 as an error — the server gate outranks the hidden button", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      status: 403,
      json: async () => ({ detail: "your role is not permitted to perform this action" }),
    } as Response);

    render(<ControlList controls={[control()]} canGenerate={true} />);
    fireEvent.click(screen.getByRole("button", { name: "Analyse" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/Analysis failed \(403\)/),
    );
  });

  it("disables the buttons while a run is in flight", async () => {
    let release: (value: unknown) => void = () => {};
    global.fetch = jest.fn().mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );

    render(<ControlList controls={[control()]} canGenerate={true} />);
    const button = screen.getByRole("button", { name: "Analyse" });
    fireEvent.click(button);

    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveTextContent("Analysing…");
    release({ status: 201, json: async () => generated() });
    await waitFor(() => expect(button).not.toBeDisabled());
  });
});
