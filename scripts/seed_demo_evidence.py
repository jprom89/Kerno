"""seed_demo_evidence.py — dev-only: realistic NIS2 demo evidence for the KER-401 demo.

What:  Inserts six NIS2 Article 21(2) controls' worth of context_records and
       links each to its control via control_evidence_links, under the dev
       tenant (admin@kerno.local), with curated relevance scores that produce
       a deliberate 2 met / 1 partial / 3 gap spread when the hybrid engine
       scores them.
Why:   The dev tenant has zero evidence, so POST /recommendations/generate
       returns a uniform wall of gaps. This gives the design-partner demo real
       contrast (a strong "met", a genuine "partial", and clear "gap" cases,
       including the "policy on paper, no enforcement" story on 21.2e).
How:   KERNO_ENV=development python scripts/seed_demo_evidence.py
       Idempotent — deterministic UUIDs + ON CONFLICT DO NOTHING, so re-running
       changes nothing. Dev-only: hard-exits unless KERNO_ENV=development.

WARNING: these are FICTIONAL documents for a dev demo tenant. If real
design-partner data ever shares this tenant, prune this seed first so nobody
mistakes a fake attestation for a real one.
"""

from __future__ import annotations

import os
import sys
import uuid

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2

_DEV_EMAIL = "admin@kerno.local"

# Fixed namespace so every record_id / link_id is a stable function of the
# evidence's external_id — the source of idempotency (re-runs collide on the
# primary key and ON CONFLICT DO NOTHING skips them).
_UUID_NAMESPACE = uuid.UUID("5eed0000-0000-4000-8000-000000000000")

# One entry per control. Each evidence dict maps 1:1 to a context_records row
# and a control_evidence_links row. relevance_score follows the approved rubric;
# the per-control means are the verdict (mean vs HIGH=0.75 / MEDIUM=0.40).
_EVIDENCE_BY_CONTROL: dict[str, list[dict]] = {
    "NIS2-21.2a": [  # projected met/high (mean 0.90)
        {
            "external_id": "ISP-v3.2", "source_system": "confluence",
            "record_type": "policy", "relevance_score": 0.92,
            "title": "Information Security Policy v3.2",
            "body": ("Board-approved ISMS policy signed by the CISO on 2026-01-15, "
                     "on an annual review cycle. Defines scope, roles and "
                     "responsibilities, and risk-acceptance criteria."),
            "note": "Directly addresses the control; verifiable (signed + dated).",
        },
        {
            "external_id": "RISK-REG-2026Q1", "source_system": "grc-tool",
            "record_type": "register", "relevance_score": 0.88,
            "title": "Enterprise Risk Register - Q1 2026",
            "body": ("Live risk register: 47 tracked risks with owners, "
                     "likelihood/impact ratings and treatment plans. Last "
                     "updated 2026-03-30."),
            "note": "Directly addresses risk analysis; verifiable (dated, owned).",
        },
    ],
    "NIS2-21.2b": [  # projected met/high (mean 0.88)
        {
            "external_id": "IR-RUNBOOK-v4", "source_system": "confluence",
            "record_type": "runbook", "relevance_score": 0.90,
            "title": "Incident Response Runbook v4",
            "body": ("Formal IR runbook approved by the CISO and tested "
                     "quarterly. Covers detection, triage, escalation tiers and "
                     "post-incident review."),
            "note": "Directly addresses; verifiable (approved + tested).",
        },
        {
            "external_id": "SOC-ESCLOG-2026Q1", "source_system": "pagerduty",
            "record_type": "log", "relevance_score": 0.86,
            "title": "SOC Incident Escalation Log - 2026 Q1",
            "body": ("Operational log of 12 incidents triaged in Q1 with "
                     "timestamps, on-call acknowledgement times and resolution "
                     "notes."),
            "note": "Directly addresses; verifiable (logged, timestamped).",
        },
    ],
    "NIS2-21.2c": [  # projected gap/low (mean 0.20)
        {
            "external_id": "BCP-DRAFT-2025", "source_system": "sharepoint",
            "record_type": "draft", "relevance_score": 0.20,
            "title": "Business Continuity Plan - DRAFT",
            "body": ("One-page outline, never approved, last edited 14 months "
                     "ago. No RTO/RPO targets, no test record, no crisis-comms "
                     "plan."),
            "note": "Outdated + draft-only; barely covers the control.",
        },
    ],
    "NIS2-21.2d": [  # projected partial/medium (mean 0.55)
        {
            "external_id": "VENDOR-RA-2025", "source_system": "grc-tool",
            "record_type": "assessment", "relevance_score": 0.62,
            "title": "Third-Party Vendor Risk Assessment 2025",
            "body": ("Risk assessment covering the top 12 critical vendors (of "
                     "~40) with tiering and due-diligence notes."),
            "note": "Addresses the control but partial coverage (top tier only).",
        },
        {
            "external_id": "SOC2-CLOUDCO-2025", "source_system": "vendor-portal",
            "record_type": "attestation", "relevance_score": 0.48,
            "title": "SOC 2 Type II Report - CloudCo (hosting)",
            "body": "SOC 2 Type II report for one key hosting supplier, dated 2025.",
            "note": "Related, but one vendor of many; not the supply chain.",
        },
    ],
    "NIS2-21.2e": [  # projected gap/low (mean 0.32) - policy on paper, no enforcement
        {
            "external_id": "SDLC-POL-v1.0", "source_system": "confluence",
            "record_type": "policy", "relevance_score": 0.42,
            "title": "Secure SDLC Policy v1.0",
            "body": ("Documented secure-development policy: mandatory code "
                     "review, SAST and dependency-scanning gates."),
            "note": "Addresses the control on paper; no evidence of enforcement.",
        },
        {
            "external_id": "SAST-MAIN-2026", "source_system": "ci-pipeline",
            "record_type": "scan", "relevance_score": 0.22,
            "title": "SAST Scan Summary - main branch",
            "body": ("One SAST run on the primary repo. No remediation-workflow "
                     "evidence and no coverage across the other repositories."),
            "note": "Related but partial; single repo, single run.",
        },
    ],
    "NIS2-21.2f": [  # projected gap/low (mean 0.28)
        {
            "external_id": "IA-REPORT-2024", "source_system": "audit",
            "record_type": "report", "relevance_score": 0.28,
            "title": "Internal Audit Report 2024",
            "body": ("Internal audit from mid-2024 (>18 months old), scoped to "
                     "financial controls; touches security governance only in "
                     "passing."),
            "note": "Outdated and tangential; not a security-effectiveness assessment.",
        },
    ],
}

_SELECT_CONTROL_ID = "SELECT control_id FROM compliance_controls WHERE control_ref = %s"

# context_records is FORCE row-level secured (migration 018); the SET LOCAL in
# main() supplies the tenant context this INSERT runs under.
_INSERT_RECORD = """
INSERT INTO context_records
    (record_id, tenant_id, source_system, external_id, record_type, title, body)
VALUES
    (%(record_id)s, %(tenant_id)s, %(source_system)s, %(external_id)s,
     %(record_type)s, %(title)s, %(body)s)
ON CONFLICT (record_id) DO NOTHING
"""

# control_evidence_links is also FORCE row-level secured. linked_at defaults are
# not relied on — set explicitly. removed_at stays NULL so the link is active.
_INSERT_LINK = """
INSERT INTO control_evidence_links
    (link_id, control_id, record_id, linked_by, linked_at, relevance_score, note)
VALUES
    (%(link_id)s, %(control_id)s, %(record_id)s, 'demo-seed', now(),
     %(relevance_score)s, %(note)s)
ON CONFLICT (link_id) DO NOTHING
"""


def _record_uuid(external_id: str) -> str:
    """Return the deterministic record_id for one evidence document."""
    return str(uuid.uuid5(_UUID_NAMESPACE, f"record:{external_id}"))


def _link_uuid(control_ref: str, external_id: str) -> str:
    """Return the deterministic link_id for one (control, evidence) pair."""
    return str(uuid.uuid5(_UUID_NAMESPACE, f"link:{control_ref}:{external_id}"))


def _seed_control(cursor, tenant_id: str, control_ref: str, evidence: list[dict]) -> int:
    """Insert one control's evidence records and links; return the link count.

    Skips the control (with a warning) if its control_ref is not in the
    catalogue. Idempotent — deterministic ids plus ON CONFLICT DO NOTHING.
    """
    cursor.execute(_SELECT_CONTROL_ID, [control_ref])
    row = cursor.fetchone()
    if row is None:
        print(f"  WARNING: control {control_ref} not found; skipped", file=sys.stderr)
        return 0
    control_id = str(row[0])
    for item in evidence:
        record_id = _record_uuid(item["external_id"])
        cursor.execute(_INSERT_RECORD, {
            "record_id": record_id, "tenant_id": tenant_id,
            "source_system": item["source_system"], "external_id": item["external_id"],
            "record_type": item["record_type"], "title": item["title"], "body": item["body"],
        })
        cursor.execute(_INSERT_LINK, {
            "link_id": _link_uuid(control_ref, item["external_id"]),
            "control_id": control_id, "record_id": record_id,
            "relevance_score": item["relevance_score"], "note": item["note"],
        })
    return len(evidence)


def main() -> None:
    """Seed the demo evidence set under the dev tenant. Dev-only, idempotent.

    Refuses to run unless KERNO_ENV=development. Resolves the dev tenant, sets
    its RLS context for the whole transaction (both target tables are FORCE
    row-level secured), and seeds every control in _EVIDENCE_BY_CONTROL.
    """
    if os.getenv("KERNO_ENV", "") != "development":
        print(
            "ERROR: seed_demo_evidence.py refused to run - KERNO_ENV is not "
            "'development'. This inserts fictional demo data and must never "
            "touch a staging or production database.",
            file=sys.stderr,
        )
        sys.exit(1)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(database_url)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tenant_id FROM tenants WHERE email = %s", [_DEV_EMAIL])
            tenant_row = cursor.fetchone()
            if tenant_row is None:
                print(f"ERROR: dev tenant {_DEV_EMAIL} not found; run seed_dev_tenant.py first.",
                      file=sys.stderr)
                sys.exit(1)
            tenant_id = str(tenant_row[0])
            cursor.execute("SET LOCAL app.current_tenant_id = %s", [tenant_id])
            total = 0
            for control_ref, evidence in _EVIDENCE_BY_CONTROL.items():
                total += _seed_control(cursor, tenant_id, control_ref, evidence)
        print(f"Demo evidence seeded under {_DEV_EMAIL}: {total} evidence links "
              f"across {len(_EVIDENCE_BY_CONTROL)} controls (idempotent).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
