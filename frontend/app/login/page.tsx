/**
 * app/login/page.tsx — the sign-in form (KER-301, organisation added in KER-408).
 *
 * What:  organisation + email + password, posted to the Next.js /api/auth/login
 *        route, which exchanges them with FastAPI and sets the httpOnly cookie.
 * Why:   the organisation is part of the credential, not a hint: an email is
 *        unique only within one organisation, so email+password alone does not
 *        identify an account. One consultant may legitimately hold accounts in
 *        several client organisations.
 * How:   rendered at /login. Accepts ?org=<slug> to pre-fill, and remembers the
 *        last organisation used so a single-organisation customer effectively
 *        types it once, ever. Tests: npm test.
 */

"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

// The organisation slug is NOT a secret and NOT a credential on its own — it is
// a public identifier (the same slug the Trust Center exposes). Remembering it
// here is a convenience only; nothing that authenticates anything is ever
// placed in client-readable storage. The session JWT remains httpOnly.
const LAST_ORG_KEY = "kerno_last_org";

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [organisation, setOrganisation] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    // A link with ?org= wins; otherwise fall back to the last one used here.
    const fromUrl = searchParams.get("org");
    const remembered =
      typeof window === "undefined" ? null : window.localStorage.getItem(LAST_ORG_KEY);
    if (fromUrl) {
      setOrganisation(fromUrl);
    } else if (remembered) {
      setOrganisation(remembered);
    }
  }, [searchParams]);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        tenant_slug: organisation.trim().toLowerCase(),
      }),
    });
    if (response.ok) {
      if (typeof window !== "undefined") {
        window.localStorage.setItem(LAST_ORG_KEY, organisation.trim().toLowerCase());
      }
      router.push("/dashboard");
      router.refresh();
    } else {
      // Uniform message: never reveal whether the organisation, the email, or
      // the password was the wrong one.
      setError("Invalid organisation, email, or password.");
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-8 shadow-sm"
      aria-label="Sign in to Kerno"
    >
      <h1 className="mb-6 text-2xl font-semibold text-slate-900">Kerno</h1>
      <label className="mb-4 block">
        <span className="mb-1 block text-sm font-medium text-slate-700">Organisation</span>
        <input
          type="text"
          required
          autoComplete="organization"
          placeholder="your-organisation"
          value={organisation}
          onChange={(event) => setOrganisation(event.target.value)}
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none"
        />
      </label>
      <label className="mb-4 block">
        <span className="mb-1 block text-sm font-medium text-slate-700">Email</span>
        <input
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none"
        />
      </label>
      <label className="mb-6 block">
        <span className="mb-1 block text-sm font-medium text-slate-700">Password</span>
        <input
          type="password"
          required
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="w-full rounded border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none"
        />
      </label>
      {error && (
        <p role="alert" className="mb-4 text-sm text-red-700">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={submitting}
        className="w-full rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
      >
        {submitting ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50">
      {/* useSearchParams requires a Suspense boundary in the App Router. */}
      <Suspense fallback={null}>
        <LoginForm />
      </Suspense>
    </main>
  );
}
