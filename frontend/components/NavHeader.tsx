/**
 * components/NavHeader.tsx — who is logged in, on every dashboard page (KER-301 AC-6).
 *
 * What:  Kerno logo, the logged-in user's email, a role badge, and logout.
 * Why:   EU AI Act Article 14 — human oversight requires identified human
 *        actors; the identity strings come from the server-validated /me call,
 *        never from anything client-readable.
 * How:   rendered by app/dashboard/layout.tsx with server-fetched props.
 *        Tests: npm test.
 */

"use client";

import { usePathname, useRouter } from "next/navigation";

interface NavHeaderProps {
  email: string;
  role: string;
}

// Register leads: it is the record the product maintains. Submissions is the
// other half — filing that record. Coverage and the rest are views onto it.
const NAV_LINKS = [
  { href: "/dashboard/register", label: "Register" },
  { href: "/dashboard/submissions", label: "Submissions" },
  { href: "/dashboard", label: "Coverage" },
  { href: "/dashboard/recommendations", label: "Recommendations" },
  { href: "/dashboard/evidence", label: "Evidence" },
];

export default function NavHeader({ email, role }: NavHeaderProps) {
  const router = useRouter();
  const pathname = usePathname();

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
    router.refresh();
  }

  return (
    <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3">
      <nav className="flex items-center gap-6">
        <span className="text-lg font-semibold tracking-tight text-slate-900">Kerno</span>
        {NAV_LINKS.map((link) => {
          // /dashboard is the Coverage home, so it must match exactly —
          // startsWith would mark it current on every dashboard page.
          const current =
            link.href === "/dashboard" ? pathname === link.href : pathname.startsWith(link.href);
          return (
            <a
              key={link.href}
              href={link.href}
              aria-current={current ? "page" : undefined}
              className={
                current
                  ? "text-sm font-medium text-slate-900"
                  : "text-sm text-slate-600 hover:text-slate-900"
              }
            >
              {link.label}
            </a>
          );
        })}
      </nav>
      <div className="flex items-center gap-4">
        <span className="text-sm text-slate-600">{email}</span>
        <span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium uppercase tracking-wide text-slate-700">
          {role.replace(/_/g, " ")}
        </span>
        <button
          type="button"
          onClick={handleLogout}
          className="rounded border border-slate-300 px-3 py-1 text-sm text-slate-700 hover:bg-slate-50"
        >
          Log out
        </button>
      </div>
    </header>
  );
}
