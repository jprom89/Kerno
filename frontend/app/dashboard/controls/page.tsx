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
import ExportButton from "@/components/ExportButton";
import { fetchCoverageControls, fetchMe } from "@/lib/api";

// UI gating only (KER-304 AC-6): hidden for auditor and end_customer_admin.
const EXPORT_ROLES = ["compliance_lead", "vciso", "security_engineer", "platform_engineer"];

export default async function ControlsPage({
  searchParams,
}: {
  searchParams: Promise<{ category?: string }>;
}) {
  const { category } = await searchParams;
  const [controls, me] = await Promise.all([fetchCoverageControls(category), fetchMe()]);
  const canExport = me !== null && EXPORT_ROLES.includes(me.role);

  return (
    <section>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold capitalize text-slate-900">
          {category ? category.replace(/_/g, " ") : "All controls"}
        </h1>
        <div className="flex items-center gap-4">
          {canExport && category && <ExportButton family={category} />}
          <Link href="/dashboard" className="text-sm text-slate-600 hover:text-slate-900">
            ← Back to dashboard
          </Link>
        </div>
      </div>
      <ControlList controls={controls} />
    </section>
  );
}
