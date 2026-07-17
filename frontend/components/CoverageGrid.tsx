/**
 * components/CoverageGrid.tsx — the NIS2 category card grid (KER-302 AC-2/AC-5).
 *
 * What:  one card per NIS2 category showing met (green) / partial (amber) /
 *        gap (red) counts and a proportional bar; each card links to the
 *        category's control list.
 * Why:   the design-partner view of posture at a glance. Colour pairs are
 *        WCAG AA: dark-on-light badges (green-800/green-100 etc.) and the bar
 *        carries an aria-label so the numbers are never colour-only.
 * How:   rendered by app/dashboard/page.tsx. Tests: npm test.
 */

import Link from "next/link";

import type { CategoryCoverage } from "@/lib/api";

const FULL_BAR_PERCENT = 100;

function percent(part: number, total: number): number {
  return total === 0 ? 0 : Math.round((part / total) * FULL_BAR_PERCENT);
}

function CategoryCard({ category }: { category: CategoryCoverage }) {
  const metPercent = percent(category.met, category.total);
  const partialPercent = percent(category.partial, category.total);
  const gapPercent = FULL_BAR_PERCENT - metPercent - partialPercent;
  return (
    <Link
      href={`/dashboard/controls?category=${encodeURIComponent(category.category)}`}
      className="block rounded-lg border border-slate-200 bg-white p-4 shadow-sm transition hover:border-slate-400"
      data-testid={`category-card-${category.category}`}
    >
      <h3 className="mb-2 text-sm font-semibold capitalize text-slate-900">
        {category.category.replace(/_/g, " ")}
      </h3>
      <div className="mb-3 flex gap-2 text-xs font-medium">
        <span className="rounded bg-green-100 px-2 py-0.5 text-green-800">
          {category.met} met
        </span>
        <span className="rounded bg-amber-100 px-2 py-0.5 text-amber-800">
          {category.partial} partial
        </span>
        <span className="rounded bg-red-100 px-2 py-0.5 text-red-800">
          {category.gap} gap
        </span>
      </div>
      <div
        className="flex h-2 overflow-hidden rounded bg-slate-100"
        role="img"
        aria-label={`${metPercent}% met, ${partialPercent}% partial, ${Math.max(gapPercent, 0)}% gap`}
      >
        <div className="bg-green-600" style={{ width: `${metPercent}%` }} />
        <div className="bg-amber-500" style={{ width: `${partialPercent}%` }} />
        <div className="bg-red-600" style={{ width: `${Math.max(gapPercent, 0)}%` }} />
      </div>
      <p className="mt-2 text-xs text-slate-500">{category.total} controls</p>
    </Link>
  );
}

export default function CoverageGrid({ categories }: { categories: CategoryCoverage[] }) {
  if (categories.length === 0) {
    return (
      <p className="rounded border border-slate-200 bg-white p-6 text-sm text-slate-600">
        No controls in the catalogue yet — coverage appears once controls are loaded.
      </p>
    );
  }
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
      {categories.map((category) => (
        <CategoryCard key={category.category} category={category} />
      ))}
    </div>
  );
}
