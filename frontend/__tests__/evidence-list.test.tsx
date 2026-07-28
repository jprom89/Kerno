/**
 * @jest-environment jsdom
 *
 * __tests__/evidence-list.test.tsx — the evidence library's orphan surfacing,
 * link flow, and role gating (KER-407).
 *
 * What:  the unlinked filter and orphan badge (the whole point of the page),
 *        the link POST shape, auditor read-only, and the empty state.
 * Why:   "which evidence is linked to nothing" is the question this page
 *        exists to answer; if the filter or badge regress, the orphan gap
 *        silently returns.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import EvidenceList from "@/components/EvidenceList";
import type { EvidenceRecord } from "@/lib/api";

const refresh = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push: jest.fn() }),
}));

const CONTROLS = [
  { control_id: "cid-1", control_ref: "NIS2-21.2a", title: "Risk analysis" },
  { control_id: "cid-2", control_ref: "NIS2-21.2b", title: "Incident handling" },
];

function record(overrides: Partial<EvidenceRecord> = {}): EvidenceRecord {
  return {
    record_id: "rec-1",
    source_system: "upload",
    external_id: "policy.pdf",
    record_type: "policy",
    title: "Access review policy",
    created_at: "2026-07-28T00:00:00Z",
    link_count: 0,
    ...overrides,
  };
}

describe("EvidenceList", () => {
  it("surfaces orphans with a badge and an unlinked filter", () => {
    render(
      <EvidenceList
        records={[record(), record({ record_id: "rec-2", link_count: 2 })]}
        controls={CONTROLS}
        readOnly={false}
      />,
    );

    expect(screen.getByText("1 not linked to any control")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter by link status"), {
      target: { value: "unlinked" },
    });
    expect(screen.getByTestId("evidence-rec-1")).toBeInTheDocument();
    expect(screen.queryByTestId("evidence-rec-2")).not.toBeInTheDocument();
  });

  it("posts control_id and relevance_score when linking", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ link_id: "l-1" }),
    } as unknown as Response);

    render(<EvidenceList records={[record()]} controls={CONTROLS} readOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Link to control" }));
    fireEvent.change(screen.getByLabelText("Control to link"), {
      target: { value: "cid-2" },
    });
    // The form's submit is "Save link", distinct from the row's "Link to
    // control" toggle — two buttons sharing one accessible name is an a11y
    // problem, not just an ambiguous selector.
    fireEvent.click(screen.getByRole("button", { name: "Save link" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      "/api/evidence/rec-1/links",
      expect.objectContaining({ method: "POST" }),
    ));
    const sent = JSON.parse((global.fetch as jest.Mock).mock.calls[0][1].body);
    expect(sent.control_id).toBe("cid-2");
    expect(typeof sent.relevance_score).toBe("number");
  });

  it("hides upload and link actions for a read-only auditor", () => {
    render(<EvidenceList records={[record()]} controls={CONTROLS} readOnly={true} />);

    expect(screen.queryByRole("button", { name: "Link to control" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Evidence file")).not.toBeInTheDocument();
  });

  it("renders a guiding empty state when there is no evidence", () => {
    render(<EvidenceList records={[]} controls={CONTROLS} readOnly={false} />);

    expect(screen.getByText(/No evidence yet/)).toBeInTheDocument();
  });
});
