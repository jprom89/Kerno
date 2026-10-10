/**
 * @jest-environment jsdom
 *
 * The review queue's action mapping, role gating, filtering and empty state (KER-303), and the identity of the
 * recommendation each decision names. A decision must carry the displayed row's recommendation_id, and a 409 must
 * leave that row visibly replaced without ever re-sending the decision (SEC-REMED-005).
 */

import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import RecommendationList from "@/components/RecommendationList";
import type { OpenRecommendation } from "@/lib/api";

const CONTROLS = [
  { control_id: "cid-1", control_ref: "NIS2-21.2a", title: "Risk analysis policy" },
  { control_id: "cid-2", control_ref: "NIS2-21.2b", title: "Incident handling" },
];

const STALE_DETAIL =
  "recommendation rec-1 has been replaced by a newer recommendation for this control; " +
  "review the newer recommendation. This decision was not recorded.";

// Long enough for any follow-up request a retry would make to have been sent.
const SETTLE_MILLISECONDS = 50;

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

const SECOND_ROW = item({
  recommendation_id: "rec-2",
  control_id: "cid-2",
  control_ref: "NIS2-21.2b",
  control_title: "Incident handling",
});

function respond(status: number, body: unknown): Response {
  return { status, json: async () => body } as unknown as Response;
}

function okFetch() {
  return jest.fn().mockResolvedValue(respond(201, { override_id: "o-1" }));
}

function staleThenOkFetch() {
  return jest.fn()
    .mockResolvedValueOnce(respond(409, { detail: STALE_DETAIL }))
    .mockResolvedValue(respond(201, { override_id: "o-2" }));
}

function sentBody(callIndex = 0) {
  return JSON.parse((global.fetch as jest.Mock).mock.calls[callIndex][1].body);
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MILLISECONDS));
  });
}

describe("RecommendationList", () => {
  it("Approve posts action_type=approve for the displayed recommendation and removes the row", async () => {
    global.fetch = okFetch();
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      "/api/overrides",
      expect.objectContaining({ method: "POST" }),
    ));
    const sent = sentBody();
    expect(sent.action_type).toBe("approve");
    expect(sent.original_control_id).toBe("cid-1");
    expect(sent.recommendation_id).toBe("rec-1");
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
    const sent = sentBody();
    expect(sent.action_type).toBe("edit");
    expect(sent.recommendation_id).toBe("rec-1");
    expect(sent.corrected_control_id).toBe("cid-2");
    expect(sent.justification_text).toBe("Partial coverage found."); // pre-filled rationale
  });

  it("Reject posts action_type=reject for the displayed recommendation and removes the row", async () => {
    global.fetch = okFetch();
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    fireEvent.change(screen.getByLabelText("Corrected control"), {
      target: { value: "cid-2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit reject" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(1));
    const sent = sentBody();
    expect(sent.action_type).toBe("reject");
    expect(sent.original_control_id).toBe("cid-1");
    expect(sent.recommendation_id).toBe("rec-1");
    expect(sent.corrected_control_id).toBe("cid-2");
    await waitFor(() =>
      expect(screen.queryByTestId("recommendation-rec-1")).not.toBeInTheDocument(),
    );
  });

  it("a 409 keeps the row visible but replaced, explains why, and never retries", async () => {
    global.fetch = staleThenOkFetch();
    render(
      <RecommendationList initialItems={[item(), SECOND_ROW]} controls={CONTROLS} readOnly={false} />,
    );
    const firstRow = screen.getByTestId("recommendation-rec-1");

    fireEvent.click(within(firstRow).getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(within(firstRow).getByText(/A newer recommendation has replaced this one/)).toBeInTheDocument(),
    );
    await settle();
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(sentBody().recommendation_id).toBe("rec-1");
    expect(firstRow).toBeInTheDocument();
    expect(within(firstRow).getByText(/Your decision was not recorded/)).toBeInTheDocument();
    expect(within(firstRow).getByRole("link", { name: "Reload the queue" })).toHaveAttribute(
      "href",
      "/dashboard/recommendations",
    );
    expect(within(firstRow).queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(within(firstRow).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(within(firstRow).queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    const toast = screen.getByRole("status");
    expect(toast).toHaveTextContent(/not recorded/);
    expect(toast).toHaveTextContent(/a newer recommendation has replaced the one you reviewed for NIS2-21\.2a/);
    expect(toast).toHaveTextContent(/Reload the queue to review it/);
  });

  it("a replaced row leaves the other rows actionable for their own recommendation", async () => {
    global.fetch = staleThenOkFetch();
    render(
      <RecommendationList initialItems={[item(), SECOND_ROW]} controls={CONTROLS} readOnly={false} />,
    );
    const firstRow = screen.getByTestId("recommendation-rec-1");
    fireEvent.click(within(firstRow).getByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(within(firstRow).getByRole("link", { name: "Reload the queue" })).toBeInTheDocument(),
    );

    const secondRow = screen.getByTestId("recommendation-rec-2");
    const approveSecond = within(secondRow).getByRole("button", { name: "Approve" });
    await waitFor(() => expect(approveSecond).toBeEnabled());
    fireEvent.click(approveSecond);

    await waitFor(() =>
      expect(screen.queryByTestId("recommendation-rec-2")).not.toBeInTheDocument(),
    );
    expect(global.fetch).toHaveBeenCalledTimes(2);
    expect(sentBody(1).recommendation_id).toBe("rec-2");
    expect(sentBody(1).original_control_id).toBe("cid-2");
    expect(firstRow).toBeInTheDocument();
    expect(within(firstRow).getByText(/Your decision was not recorded/)).toBeInTheDocument();
  });

  it("a 409 on a submitted Edit closes that row's form", async () => {
    global.fetch = staleThenOkFetch();
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Corrected control"), {
      target: { value: "cid-2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit edit" }));

    await waitFor(() =>
      expect(screen.getByText(/A newer recommendation has replaced this one/)).toBeInTheDocument(),
    );
    expect(screen.queryByLabelText("Justification")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Corrected control")).not.toBeInTheDocument();
    await settle();
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  it("a request that never completes shows an error and re-enables the actions", async () => {
    global.fetch = jest.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/NIS2-21\.2a: the request did not complete/),
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Edit" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(screen.getByTestId("recommendation-rec-1")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Reload the queue" })).not.toBeInTheDocument();
  });

  it("a validation error with a list detail is shown as readable text", async () => {
    global.fetch = jest.fn().mockResolvedValue(respond(422, {
      detail: [
        { type: "missing", loc: ["body", "recommendation_id"], msg: "Field required" },
        { type: "string_type", loc: ["body", "action_type"], msg: "Input should be a valid string" },
      ],
    }));
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Action failed (422): Field required; Input should be a valid string",
      ),
    );
    expect(screen.getByRole("status")).not.toHaveTextContent("[object Object]");
  });

  it("a string detail is shown verbatim and the row stays actionable", async () => {
    global.fetch = jest.fn().mockResolvedValue(respond(404, { detail: "entry not found" }));
    render(<RecommendationList initialItems={[item()]} controls={CONTROLS} readOnly={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Action failed (404): entry not found"),
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.queryByRole("link", { name: "Reload the queue" })).not.toBeInTheDocument();
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
