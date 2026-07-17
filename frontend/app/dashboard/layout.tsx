/**
 * app/dashboard/layout.tsx — the auth guard around every dashboard page (KER-301 AC-3/AC-4).
 *
 * What:  validates the session against GET /api/v1/auth/me on every dashboard
 *        page load; unauthenticated (or forged-cookie) requests are redirected
 *        to /login; valid sessions render the NavHeader with the verified
 *        email + role.
 * Why:   the middleware only checks cookie PRESENCE — this layout is where the
 *        JWT is cryptographically validated, so a forged cookie dies here.
 * How:   Next.js wraps all /dashboard/* pages with this automatically.
 *        Tests: npm test.
 */

import { redirect } from "next/navigation";

import NavHeader from "@/components/NavHeader";
import { fetchMe } from "@/lib/api";

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const me = await fetchMe();
  if (!me) {
    redirect("/login");
  }
  return (
    <div className="min-h-screen bg-slate-50">
      <NavHeader email={me.email} role={me.role} />
      <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
    </div>
  );
}
