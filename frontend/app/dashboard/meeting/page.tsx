/**
 * app/dashboard/meeting/page.tsx — this month's exception-review agenda.
 *
 * What:  loads the live meeting pack and renders the script the chair reads.
 * Why:   the operator should not assemble notes by hand from coverage,
 *        evidence, and recommendations.
 * How:   server component; data via lib/api.ts. Tests: npm test.
 */

import MeetingAgenda from "@/components/MeetingAgenda";
import { fetchMeetingPack } from "@/lib/api";

export default async function MeetingPage() {
  const pack = await fetchMeetingPack();

  return (
    <section>
      <h1 className="mb-2 text-xl font-semibold text-slate-900">This month&apos;s meeting</h1>
      <p className="mb-6 text-sm text-slate-500">
        Built from live coverage, linked evidence, and open recommendations. Share this
        screen. Record decisions in Recommendations after they speak.
      </p>
      <MeetingAgenda pack={pack} />
    </section>
  );
}
