/**
 * @jest-environment jsdom
 *
 * __tests__/coverage-grid.test.tsx — the category grid's counts, links, and
 * accessibility (KER-302 AC-2/AC-5).
 *
 * What:  cards show met/partial/gap counts, link to the category drill-down,
 *        expose the proportions as an aria-label, and the empty state renders.
 * Why:   the grid is the design-partner first impression; the numbers must
 *        never be colour-only.
 * How:   npm test
 */

import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";

import CoverageGrid from "@/components/CoverageGrid";

const GOVERNANCE = { category: "governance", met: 2, partial: 1, gap: 1, total: 4 };

describe("CoverageGrid", () => {
  it("renders counts, drill-down link, and accessible proportions", () => {
    render(<CoverageGrid categories={[GOVERNANCE]} />);

    expect(screen.getByText("2 met")).toBeInTheDocument();
    expect(screen.getByText("1 partial")).toBeInTheDocument();
    expect(screen.getByText("1 gap")).toBeInTheDocument();
    expect(screen.getByTestId("category-card-governance")).toHaveAttribute(
      "href",
      "/dashboard/controls?category=governance",
    );
    expect(screen.getByRole("img")).toHaveAttribute(
      "aria-label",
      "50% met, 25% partial, 25% gap",
    );
  });

  it("renders the empty state when no categories exist", () => {
    render(<CoverageGrid categories={[]} />);

    expect(screen.getByText(/No controls in the catalogue yet/)).toBeInTheDocument();
  });
});
