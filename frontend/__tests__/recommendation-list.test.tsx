/**
 * @jest-environment jsdom
 *
 * __tests__/recommendation-list.test.tsx — the review queue's action mapping,
 * role gating, filtering, and empty state (KER-303).
 *
 * What:  Approve posts action_type="approve" immediately; Edit opens the form
 *        whose submit is disabled until a corrected control is chosen; the
 *        auditor view hides all action buttons; the confidence filter narrows
 *        rows; the empty state renders.
 * Why:   the action mapping is contract-critical — a wrong action_type would
 *        422 against the KER-106 backend.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import RecommendationList from "@/components/RecommendationList";
import type { OpenRecommendation } from "@/lib/api";

const CONTROLS = [
  { control_id: "cid-1", control_ref: "NIS2-21.2a", title: "Risk analysis policy" },
  { control_id: "cid-2", control_ref: "NIS2-21.2b", title: "Incident handling" },
];

function item(overrides: Partial<OpenRecommendation> = {}): OpenRecommendation {
  return {
    recommendation_id: "rec-1",
    control_id: "cid-1",
    control_ref: "NIS2-21.2a",
    control_title: "Risk analysis policy",
    category: "governance",
    status: "partial",
    confidence_level: "medium",
    confidence_score: 0.66,
    rationale: "Partial coverage found.",
    evidence_count: 2,
    generated_at: "2026-07-14T00:00:00Z",
    ...overrides,
  };
}

function okFetch() {
  return jest.fn().mockResolvedValue({
    status: 201,
    json: async () => ({ override_id: "o-1" }),
  } as unknown as Response);
}

describe("RecommendationList", () => {
  it("Approve posts action_type=approve for the row's control and removes the row", async () => {
    global.fetch = okFetch();
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      "/api/overrides",
      expect.objectContaining({ method: "POST" }),
    ));
    const sent = JSON.parse((global.fetch as jest.Mock).mock.calls[0][1].body);
    expect(sent.action_type).toBe("approve");
    expect(sent.original_control_id).toBe("cid-1");
    expect(sent.corrected_control_id).toBeNull();
    await waitFor(() =>
      expect(screen.queryByTestId("recommendation-rec-1")).not.toBeInTheDocument(),
    );
  });

  it("Edit form cannot submit without a corrected control, then posts action_type=edit", async () => {
    global.fetch = okFetch();
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const submit = screen.getByRole("button", { name: "Submit edit" });
    expect(submit).toBeDisabled(); // justification pre-filled, but no control chosen

    fireEvent.change(screen.getByLabelText("Corrected control"), {
      target: { value: "cid-2" },
    });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const sent = JSON.parse((global.fetch as jest.Mock).mock.calls[0][1].body);
    expect(sent.action_type).toBe("edit");
    expect(sent.corrected_control_id).toBe("cid-2");
    expect(sent.justification_text).toBe("Partial coverage found."); // pre-filled rationale
  });

  it("auditor view hides all action buttons", () => {
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={true} />);

    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });

  it("confidence filter narrows the visible rows", () => {
    render(
      <RecommendationList
        initialItems={[
          item(),
          item({ recommendation_id: "rec-2", confidence_level: "high", control_ref: "NIS2-21.2b" }),
        ]}
        controls={CONTROLS}
        readOnly={true}
      />,
    );

    fireEvent.change(screen.getByLabelText("Filter by confidence"), {
      target: { value: "high" },
    });
    expect(screen.queryByTestId("recommendation-rec-1")).not.toBeInTheDocument();
    expect(screen.getByTestId("recommendation-rec-2")).toBeInTheDocument();
  });

  it("renders the empty state when nothing is open", () => {
    render(<RecommendationList initialItems={[]} controls={CONTROLS} readOnly={false} />);

    expect(screen.getByText(/No open recommendations/)).toBeInTheDocument();
  });
});
