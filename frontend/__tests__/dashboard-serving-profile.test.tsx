/**
 * @jest-environment jsdom
 *
 * __tests__/dashboard-serving-profile.test.tsx — the dashboard's Recalculate control per serving profile (PILOT-READY-003).
 *
 * What:  renders the real dashboard server component: compliance_lead and
 *        vciso get Recalculate in the full profile and not in pilot; the
 *        read-only timestamp and the export control stay in both; a profile
 *        configuration error throws before any backend read.
 * Why:   the pilot API does not register manual recalculation, so a pilot
 *        dashboard offering it would show a control that can only fail.
 * How:   npm test -- dashboard-serving-profile; lib/api is mocked, and so is
 *        isFullProfile(): the server resolves its profile once per process, so
 *        one module registry cannot render several profiles, and a fresh
 *        registry would load a second React. How the profile itself resolves
 *        (development, production, missing, blank, unrecognised) is proved in
 *        serving-profile.test.ts.
 */

import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

// A relative path: the "@/" alias is rewritten in imports only, not in jest.mock.
jest.mock("../lib/api", () => ({ fetchCoverageSummary: jest.fn(), fetchMe: jest.fn() }));
jest.mock("../lib/servingProfile", () => ({
  ...jest.requireActual("../lib/servingProfile"),
  isFullProfile: jest.fn(),
}));
jest.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: jest.fn(), push: jest.fn() }),
}));

import DashboardPage from "@/app/dashboard/page";
import { fetchCoverageSummary, fetchMe } from "@/lib/api";
import { isFullProfile, ServingProfileConfigError } from "@/lib/servingProfile";

const SUMMARY = {
  total_controls: 4,
  met: 2,
  partial: 1,
  gap: 1,
  categories: [{ category: "governance", met: 2, partial: 1, gap: 1, total: 4 }],
  last_recalculated_at: null,
};

/** Render the dashboard for a server whose resolved profile is full or pilot, and one role. */
async function renderDashboard(profile: "full" | "pilot", role: string) {
  (isFullProfile as jest.Mock).mockReturnValue(profile === "full");
  (fetchCoverageSummary as jest.Mock).mockResolvedValue(SUMMARY);
  (fetchMe as jest.Mock).mockResolvedValue({ email: "u@example.test", role });
  render(await DashboardPage());
}

afterEach(() => jest.restoreAllMocks());

describe("dashboard Recalculate control", () => {
  it.each([
    ["full", "compliance_lead"],
    ["full", "vciso"],
  ] as const)("is offered under profile %p to %s", async (profile, role) => {
    await renderDashboard(profile, role);
    expect(screen.getByRole("button", { name: "Recalculate now" })).toBeInTheDocument();
  });

  it.each(["compliance_lead", "vciso"])("is not offered under pilot to %s", async (role) => {
    await renderDashboard("pilot", role);
    expect(screen.queryByRole("button", { name: "Recalculate now" })).not.toBeInTheDocument();
  });

  it("keeps the role gate in the full profile", async () => {
    await renderDashboard("full", "auditor");
    expect(screen.queryByRole("button", { name: "Recalculate now" })).not.toBeInTheDocument();
  });

  it("keeps the timestamp and the export control under pilot", async () => {
    await renderDashboard("pilot", "compliance_lead");
    expect(screen.getByText("Last recalculated: Never calibrated")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
  });

  it("throws on a profile configuration error before reading the backend", async () => {
    (isFullProfile as jest.Mock).mockImplementation(() => {
      throw new ServingProfileConfigError("KERNO_SERVING_PROFILE must be set to \"full\" or \"pilot\" outside development");
    });
    await expect(DashboardPage()).rejects.toThrow(ServingProfileConfigError);
    expect(fetchCoverageSummary).not.toHaveBeenCalled();
    expect(fetchMe).not.toHaveBeenCalled();
  });
});
