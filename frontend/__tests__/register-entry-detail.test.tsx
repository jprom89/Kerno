/**
 * @jest-environment jsdom
 *
 * __tests__/register-entry-detail.test.tsx — one register line, read and amended (KER-410).
 *
 * What:  the read view shows the whole record, an auditor cannot reach the
 *        amend form, and a save PATCHes the entry's own route.
 * Why:   amending a filed regulatory record is the write that most needs to be
 *        deliberate — and the one whose before_state the KER-409 ledger
 *        captures, so the request has to actually reach the backend route that
 *        does it.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import RegisterEntryDetail from "@/components/RegisterEntryDetail";
import type { RegisterEntry } from "@/lib/api";

const refresh = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push: jest.fn() }),
}));

const ENTRY: RegisterEntry = {
  register_entry_id: "reg-1",
  tenant_id: "tenant-1",
  provider_name: "Acme Cloud",
  service_name: "Hosting",
  provider_type: "managed_service",
  criticality_level: "high",
  business_function: "Platform",
  data_types: ["operational", "personal"],
  countries_supported: ["DE", "FR"],
  contract_start_date: "2026-01-01",
  contract_end_date: "2027-01-01",
  exit_strategy_summary: "Migrate to the secondary provider within 90 days.",
  is_active: true,
  source_record_id: "CMDB-42",
  created_at: "2026-08-14T00:00:00Z",
  updated_at: "2026-08-14T10:00:00Z",
};

describe("RegisterEntryDetail", () => {
  beforeEach(() => {
    refresh.mockClear();
  });

  it("shows the whole record, including the fields the list omits", () => {
    render(<RegisterEntryDetail entry={ENTRY} readOnly={false} />);

    expect(screen.getByRole("heading", { name: "Acme Cloud" })).toBeInTheDocument();
    expect(screen.getByText("managed service")).toBeInTheDocument();
    expect(screen.getByText("operational, personal")).toBeInTheDocument();
    expect(screen.getByText("DE, FR")).toBeInTheDocument();
    expect(screen.getByText("CMDB-42")).toBeInTheDocument();
    expect(
      screen.getByText("Migrate to the secondary provider within 90 days."),
    ).toBeInTheDocument();
  });

  it("gives an auditor the record but no way to amend it", () => {
    render(<RegisterEntryDetail entry={ENTRY} readOnly={true} />);

    expect(screen.queryByRole("button", { name: /amend/i })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Acme Cloud" })).toBeInTheDocument();
  });

  it("patches the entry's own route and refreshes on success", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ register_entry_id: "reg-1" }),
    } as Response);

    render(<RegisterEntryDetail entry={ENTRY} readOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Amend" }));

    // The form opens pre-filled, so an amendment changes what it means to
    // change and leaves the rest of the filed record alone.
    expect(screen.getByLabelText("Provider name")).toHaveValue("Acme Cloud");
    fireEvent.change(screen.getByLabelText("Provider name"), {
      target: { value: "Acme Cloud Europe" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save amendment" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("/api/register/reg-1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body).provider_name).toBe("Acme Cloud Europe");
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("keeps the form open and reports the failure when the backend rejects it", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: false,
      status: 422,
      json: async () => ({ detail: "contract_end_date must be after contract_start_date." }),
    } as Response);

    render(<RegisterEntryDetail entry={ENTRY} readOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Amend" }));
    fireEvent.click(screen.getByRole("button", { name: "Save amendment" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/contract_end_date/),
    );
    // Still editing: a rejected amendment must not look like a saved one.
    expect(screen.getByRole("button", { name: "Save amendment" })).toBeInTheDocument();
  });
});
