/**
 * app/api/register/route.ts — browser-safe proxy for the DORA register (KER-410).
 *
 * What:  GET lists the tenant's register entries; POST creates one.
 * Why:   the browser never calls FastAPI directly (§14 KER-301 decision 4) —
 *        the session JWT lives in an httpOnly cookie only the server can read.
 *        The backend attributes the write to that JWT's user and appends the
 *        KER-107 ledger entry (KER-409), so nothing about the actor is or can
 *        be supplied from here.
 * How:   called by RegisterList. Tests: npm test.
 */

import { NextRequest, NextResponse } from "next/server";

import { apiFetch } from "@/lib/api";

export async function GET(): Promise<NextResponse> {
  const backendResponse = await apiFetch("/api/v1/register/entries");
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  const backendResponse = await apiFetch("/api/v1/register/entries", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(await request.json()),
  });
  return NextResponse.json(await backendResponse.json(), {
    status: backendResponse.status,
  });
}
