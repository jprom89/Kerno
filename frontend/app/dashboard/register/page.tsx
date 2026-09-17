/**
 * app/dashboard/register/page.tsx — the DORA register (KER-410).
 *
 * What:  the tenant's Register of Information — every ICT third-party
 *        relationship, with an add action for the roles allowed to write.
 * Why:   the register is the record Kerno maintains; this is its first product
 *        surface. The legacy static dashboard is development-only since
 *        Ticket B and is not being rebuilt.
 * How:   server component; data via lib/api.ts. Tests: npm test.
 */

import RegisterList from "@/components/RegisterList";
import { fetchMe, fetchRegisterEntries } from "@/lib/api";
// UI gating only. Ticket A already 403s every other role on POST and PATCH
// /api/v1/register/entries — hiding the button is UX, the 403 is the guarantee.
// Auditors reach this page read-only, which is the point of the role.
import { REGISTER_WRITE_ROLES } from "@/lib/roles";

export default async function RegisterPage() {
  const [entries, me] = await Promise.all([fetchRegisterEntries(), fetchMe()]);
  const readOnly = me === null || !REGISTER_WRITE_ROLES.includes(me.role);
  const activeCount = entries.filter((entry) => entry.is_active).length;

  return (
    <section>
      <div className="mb-6">
        <h1 className="text-xl font-semibold text-slate-900">Register of Information</h1>
        <p className="mt-1 text-sm text-slate-500">
          {activeCount} active ICT third-party {activeCount === 1 ? "provider" : "providers"}
          {entries.length !== activeCount && ` (${entries.length} on record)`}. Every
          addition and amendment is recorded against the person who made it.
        </p>
      </div>
      <RegisterList entries={entries} readOnly={readOnly} />
    </section>
  );
}
