/**
 * @jest-environment jsdom
 *
 * __tests__/login-page.test.tsx — the login form's redirect behaviour (KER-301 AC-8).
 *
 * What:  successful login navigates to /dashboard; failed login shows the
 *        uniform error and stays put.
 * Why:   completes the AC-8 "valid login → cookie set + redirect" pair — the
 *        cookie half is proven in auth-routes.test.ts; this is the redirect half.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import LoginPage from "@/app/login/page";

const push = jest.fn();
const refresh = jest.fn();
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push, refresh }),
  useSearchParams: () => new URLSearchParams(mockSearch),
}));

let mockSearch = "";

function fillAndSubmit() {
  fireEvent.change(screen.getByLabelText("Organisation"), {
    target: { value: "acme-gmbh" },
  });
  fireEvent.change(screen.getByLabelText("Email"), {
    target: { value: "lead@kerno.local" },
  });
  fireEvent.change(screen.getByLabelText("Password"), {
    target: { value: "correct-horse" },
  });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
}

// jsdom has no Response constructor; the page only reads `ok`, so a plain
// object stands in for the fetch result.
function fetchResult(ok: boolean) {
  return jest.fn().mockResolvedValue({ ok } as Response);
}

describe("LoginPage", () => {
  it("redirects to /dashboard after a successful login", async () => {
    global.fetch = fetchResult(true);
    render(<LoginPage />);
    fillAndSubmit();

    await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
  });

  it("shows a uniform error and does not navigate on invalid credentials", async () => {
    global.fetch = fetchResult(false);
    render(<LoginPage />);
    fillAndSubmit();

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/Invalid organisation, email, or password/),
    );
    expect(push).not.toHaveBeenCalled();
  });

  it("sends the organisation slug as part of the credential", async () => {
    global.fetch = fetchResult(true);
    render(<LoginPage />);
    fillAndSubmit();

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const sent = JSON.parse((global.fetch as jest.Mock).mock.calls[0][1].body);
    // KER-408: email alone does not identify an account.
    expect(sent.tenant_slug).toBe("acme-gmbh");
    expect(sent.email).toBe("lead@kerno.local");
  });

  it("pre-fills the organisation from an ?org= link", async () => {
    mockSearch = "org=from-the-link";
    global.fetch = fetchResult(true);
    render(<LoginPage />);

    await waitFor(() =>
      expect(screen.getByLabelText("Organisation")).toHaveValue("from-the-link"),
    );
    mockSearch = "";
  });
});
