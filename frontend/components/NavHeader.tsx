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

import { useRouter } from "next/navigation";

interface NavHeaderProps {
  email: string;
  role: string;
}

export default function NavHeader({ email, role }: NavHeaderProps) {
  const router = useRouter();

  async function handleLogout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
    router.refresh();
  }

  return (
    <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3">
      <nav className="flex items-center gap-6">
        <span className="text-lg font-semibold tracking-tight text-slate-900">Kerno</span>
        <a href="/dashboard" className="text-sm text-slate-600 hover:text-slate-900">
          Coverage
        </a>
        <a href="/dashboard/recommendations" className="text-sm text-slate-600 hover:text-slate-900">
          Recommendations
        </a>
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
