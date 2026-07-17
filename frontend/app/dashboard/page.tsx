/**
 * app/dashboard/page.tsx — dashboard home (KER-301 stub).
 *
 * What:  placeholder landing so the auth flow has a destination.
 * Why:   KER-302 replaces this with the NIS2 coverage dashboard; KER-301 only
 *        needs the route to exist behind the auth guard.
 * How:   rendered at /dashboard inside the guarded layout. Tests: npm test.
 */

export default function DashboardPage() {
  return (
    <section>
      <h1 className="text-xl font-semibold text-slate-900">Dashboard</h1>
      <p className="mt-2 text-sm text-slate-600">
        NIS2 coverage arrives with KER-302.
      </p>
    </section>
  );
}
