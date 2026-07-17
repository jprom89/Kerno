/**
 * app/dashboard/controls/page.tsx — the category drill-down (KER-302 AC-5).
 *
 * What:  /dashboard/controls?category=… lists that category's controls with
 *        their system-of-record status badges; without a category it lists all.
 * Why:   each category card on the dashboard links here for the detail view.
 * How:   server component; data via lib/api.ts. Tests: npm test.
 */

import Link from "next/link";

import ControlList from "@/components/ControlList";
import { fetchCoverageControls } from "@/lib/api";

export default async function ControlsPage({
  searchParams,
}: {
  searchParams: Promise<{ category?: string }>;
}) {
  const { category } = await searchParams;
  const controls = await fetchCoverageControls(category);

  return (
    <section>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold capitalize text-slate-900">
          {category ? category.replace(/_/g, " ") : "All controls"}
        </h1>
        <Link href="/dashboard" className="text-sm text-slate-600 hover:text-slate-900">
          ← Back to dashboard
        </Link>
      </div>
      <ControlList controls={controls} />
    </section>
  );
}
