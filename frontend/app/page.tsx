/**
 * app/page.tsx — the bare root URL routes to the login page (KER-301).
 *
 * What:  redirect / → /login.
 * Why:   the app has no public landing page; every session starts at sign-in
 *        (authenticated users are bounced onward to /dashboard by the flow).
 * How:   rendered at /. Tests: npm test.
 */

import { redirect } from "next/navigation";

export default function RootPage() {
  redirect("/login");
}
