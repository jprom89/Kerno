/**
 * @jest-environment jsdom
 *
 * __tests__/register-list.test.tsx — the DORA register list and its role gating (KER-410).
 *
 * What:  the register renders its lines, the create POST carries the field
 *        shape the backend expects, and an auditor gets no write controls.
 * Why:   this register is the record the product exists to maintain, and the
 *        auditor case is the one that must not silently regress — the UI hide
 *        is a courtesy, but a visible Add button for a role the server 403s is
 *        a broken promise to the user either way.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import RegisterList from "@/components/RegisterList";
import type { RegisterEntry } from "@/lib/api";

const refresh = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ refresh, push: jest.fn() }),
}));

function entry(overrides: Partial<RegisterEntry> = {}): RegisterEntry {
  return {
    register_entry_id: "reg-1",
    tenant_id: "tenant-1",
    provider_name: "Acme Cloud",
    service_name: "Hosting",
    provider_type: "cloud",
    criticality_level: "critical",
    business_function: "Platform",
    data_types: ["operational"],
    countries_supported: ["DE"],
    contract_start_date: "2026-01-01",
    contract_end_date: "2027-01-01",
    exit_strategy_summary: null,
    is_active: true,
    source_record_id: null,
    created_at: "2026-08-14T00:00:00Z",
    updated_at: "2026-08-14T00:00:00Z",
    ...overrides,
  };
}

describe("RegisterList", () => {
  beforeEach(() => {
    refresh.mockClear();
  });

  it("lists each provider with its criticality and links to the detail page", () => {
    render(
      <RegisterList
        entries={[entry(), entry({ register_entry_id: "reg-2", provider_name: "Beta Telecom" })]}
        readOnly={false}
      />,
    );

    expect(screen.getByText("Acme Cloud")).toHaveAttribute(
      "href",
      "/dashboard/register/reg-1",
    );
    expect(screen.getByText("Beta Telecom")).toBeInTheDocument();
    expect(screen.getAllByText("critical")).toHaveLength(2);
  });

  it("hides every write control from an auditor", () => {
    render(<RegisterList entries={[entry()]} readOnly={true} />);

    expect(screen.queryByRole("button", { name: /add provider/i })).not.toBeInTheDocument();
    // The register itself stays fully visible — read-only is the auditor's job,
    // not a reduced view.
    expect(screen.getByText("Acme Cloud")).toBeInTheDocument();
  });

  it("offers the add form to a writing role", () => {
    render(<RegisterList entries={[entry()]} readOnly={false} />);

    expect(screen.getByRole("button", { name: /add provider/i })).toBeInTheDocument();
  });

  it("says the register is empty rather than showing a bare table", () => {
    render(<RegisterList entries={[]} readOnly={false} />);

    expect(screen.getByText(/no providers on the register yet/i)).toBeInTheDocument();
  });

  it("posts the created provider in the shape the backend expects", async () => {
    global.fetch = jest.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ register_entry_id: "reg-new" }),
    } as Response);

    render(<RegisterList entries={[]} readOnly={false} />);
    fireEvent.click(screen.getByRole("button", { name: /add provider/i }));

    fireEvent.change(screen.getByLabelText("Provider name"), {
      target: { value: "Gamma Systems" },
    });
    fireEvent.change(screen.getByLabelText("Service name"), {
      target: { value: "Payments" },
    });
    fireEvent.change(screen.getByLabelText("Business function supported"), {
      target: { value: "Treasury" },
    });
    fireEvent.change(screen.getByLabelText("Data types (comma separated)"), {
      target: { value: "operational, personal" },
    });
    fireEvent.change(screen.getByLabelText("Countries supported (comma separated)"), {
      target: { value: "DE, FR" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add provider" }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [url, init] = (global.fetch as jest.Mock).mock.calls[0];
    expect(url).toBe("/api/register");
    expect(init.method).toBe("POST");
    const sent = JSON.parse(init.body);
    expect(sent.provider_name).toBe("Gamma Systems");
    // Comma-separated input becomes a real list, trimmed — the backend rejects
    // an empty list with a 422.
    expect(sent.data_types).toEqual(["operational", "personal"]);
    expect(sent.countries_supported).toEqual(["DE", "FR"]);
    // Nothing about the actor is sent: the backend takes it from the JWT.
    expect(sent).not.toHaveProperty("tenant_id");
    expect(sent).not.toHaveProperty("actor_id");
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });
});
