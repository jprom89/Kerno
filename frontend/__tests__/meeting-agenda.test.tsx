/**
 * @jest-environment jsdom
 *
 * __tests__/meeting-agenda.test.tsx — the chair sees the ask, not a raw catalogue.
 *
 * What:  a gap with no evidence shows the plain-English meaning, the
 *        artefact tick-list, and the ask; Copy notes writes the markdown.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";

import MeetingAgenda from "@/components/MeetingAgenda";
import type { MeetingControl, MeetingPack } from "@/lib/api";

function control(partial: Partial<MeetingControl> & Pick<MeetingControl, "control_id" | "control_ref" | "title" | "status">): MeetingControl {
  return {
    category: "operational_resilience",
    what_this_means: "",
    evidence_count: 0,
    evidence_titles: [],
    open_recommendation_rationale: null,
    ask_in_the_meeting: "",
    approve_only_when: "Approve as met only if linked: Backup exists.",
    inspection_items: [],
    skip_unless_asked: false,
    ...partial,
  };
}

const PACK: MeetingPack = {
  generated_at: "2026-08-13T12:00:00Z",
  met: 1,
  partial: 0,
  gap: 1,
  total_controls: 2,
  decisions_needed: [
    control({
      control_id: "c-gap",
      control_ref: "NIS2-Art21-2-c",
      title: "Backup, Recovery and Crisis Management",
      what_this_means: "Are there backups, and when was the last restore test?",
      status: "gap",
      ask_in_the_meeting:
        "Nothing is linked. Who will provide evidence, or set a treatment date and an owner?",
      approve_only_when:
        "Approve as met only if linked: Backup exists. Without Last restore-test date this is partial, never met.",
      inspection_items: [
        {
          label: "Backup exists",
          pass_if: "A console screenshot or vendor page showing backups for this service.",
          fail_if: "'We have backups' with no screenshot.",
          required_for_met: true,
        },
        {
          label: "Last restore-test date",
          pass_if: "An email, ticket, or log with the date a restore was actually tested.",
          fail_if: "Backups with no restore date. Missing this item makes the control partial, not met.",
          required_for_met: false,
        },
      ],
    }),
  ],
  skip_unless_asked: [
    control({
      control_id: "c-met",
      control_ref: "NIS2-Art21-2-a",
      title: "Policies",
      category: "risk_management",
      what_this_means: "Is there an information-security or IT policy on file?",
      status: "met",
      evidence_count: 1,
      evidence_titles: ["IS policy"],
      ask_in_the_meeting: "Met — skip unless someone wants to challenge it.",
      skip_unless_asked: true,
    }),
  ],
  notes_markdown: "# NIS2 exception review\n\nNothing is linked.\n",
  preamble: "A NIS2 control is one legal requirement for this service.",
  review_minutes: 90,
};

describe("MeetingAgenda", () => {
  it("shows what the control is, the artefact ticks, and the exact ask", () => {
    render(<MeetingAgenda pack={PACK} />);

    expect(screen.getByText(/A NIS2 control is one legal requirement/)).toBeInTheDocument();
    expect(screen.getByText(/Are there backups/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing is linked. Who will provide evidence/)).toBeInTheDocument();
    expect(screen.getByText(/1 met \/ 0 partial \/ 1 gap/)).toBeInTheDocument();
    expect(screen.getByText(/Required — Backup exists/)).toBeInTheDocument();
    expect(screen.getByText(/Else partial — Last restore-test date/)).toBeInTheDocument();
    expect(screen.getByText(/Pass if: A console screenshot/)).toBeInTheDocument();
  });

  it("copies the markdown notes", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<MeetingAgenda pack={PACK} />);

    fireEvent.click(screen.getByRole("button", { name: "Copy notes" }));
    expect(writeText).toHaveBeenCalledWith(PACK.notes_markdown);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });
});
