/**
 * app/dashboard/register/[entryId]/page.tsx — one register line, and amending it (KER-410).
 *
 * What:  the full detail of a single ICT third-party relationship, editable by
 *        the roles allowed to write.
 * Why:   an amendment to a filed regulatory record needs its own surface, and
 *        the backend records the previous values in the KER-107 ledger when it
 *        happens (KER-409), so the change stays reconstructable.
 * How:   server component; a missing id renders Next's notFound(). Tests: npm test.
 */

import { notFound } from "next/navigation";

import RegisterEntryDetail from "@/components/RegisterEntryDetail";
import { fetchMe, fetchRegisterEntry } from "@/lib/api";
import { REGISTER_WRITE_ROLES } from "@/lib/roles";

export default async function RegisterEntryPage({
  params,
}: {
  params: Promise<{ entryId: string }>;
}) {
  const { entryId } = await params;
  const [entry, me] = await Promise.all([fetchRegisterEntry(entryId), fetchMe()]);
  if (entry === null) {
    notFound();
  }
  const readOnly = me === null || !REGISTER_WRITE_ROLES.includes(me.role);

  return <RegisterEntryDetail entry={entry} readOnly={readOnly} />;
}
