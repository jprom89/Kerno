"""seed_nis2_controls.py — One-time script to populate the NIS2 control catalogue.

What:  Inserts a representative set of NIS2 controls (covering all seven
       categories) and their cross-framework links to DORA, CRA, and the AI Act
       into the compliance_controls and control_crosswalks tables.

Why:   The Decision layer needs a populated catalogue to reason over. This seed
       provides the minimum viable set for Sprint 1 development and testing.
       The full NIS2 catalogue is a future data task (see Document 11 §6).

How to run:
    Ensure the database is running and migrations are applied through 008:

        alembic upgrade h3i4j5k6

    Then run from the project root (PYTHONPATH must include the root):

        PYTHONPATH=. python scripts/seed_nis2_controls.py

    The script is idempotent: running it twice produces no duplicate rows.
    The DATABASE_URL environment variable must be set to a psycopg2-compatible
    connection string, e.g.:
        postgresql://user:password@localhost:5432/kerno

Seed contents:
    12 NIS2 controls spanning all 7 categories from §3.4.
     4 target controls from DORA, CRA, and AI Act.
     5 crosswalk rows linking NIS2 to at least two other frameworks.
"""

from __future__ import annotations

import os
import re
import sys

import psycopg2

from src.models.compliance_control import (
    CATEGORY_AI_OVERSIGHT,
    CATEGORY_GOVERNANCE,
    CATEGORY_INCIDENT_HANDLING,
    CATEGORY_OPERATIONAL_RESILIENCE,
    CATEGORY_RISK_MANAGEMENT,
    CATEGORY_SUPPLY_CHAIN,
    CATEGORY_VULNERABILITY,
    ENTITY_ESSENTIAL,
    ENTITY_IMPORTANT,
    FRAMEWORK_AI_ACT,
    FRAMEWORK_CRA,
    FRAMEWORK_DORA,
    FRAMEWORK_NIS2,
    RELATIONSHIP_EQUIVALENT,
    RELATIONSHIP_PARTIAL,
    RELATIONSHIP_RELATED,
)
from src.services.control_service import ControlInput, add_crosswalk, list_controls, load_controls

# Regex that converts :name placeholders to %(name)s (psycopg2 format).
# The negative lookbehind avoids converting ::typename PostgreSQL casts.
_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_]\w*)")

# ---------------------------------------------------------------------------
# NIS2 controls — minimum 10, covering all 7 categories from §3.4
# ---------------------------------------------------------------------------

_NIS2_CONTROLS: list[ControlInput] = [
    # CATEGORY_GOVERNANCE (2 controls)
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art20-1",
        category=CATEGORY_GOVERNANCE,
        title="Governance Framework and Executive Responsibility",
        obligation_text=(
            "Member States shall ensure that the management bodies of essential and "
            "important entities approve the cybersecurity risk-management measures taken "
            "by those entities, oversee their implementation and can be held liable for "
            "infringements by the entities of this Directive."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art20-2",
        category=CATEGORY_GOVERNANCE,
        title="Management Body Cybersecurity Training",
        obligation_text=(
            "Member States shall ensure that the members of the management bodies of "
            "essential and important entities are required to follow training, and shall "
            "encourage essential and important entities to offer similar training to their "
            "employees on a regular basis."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    # CATEGORY_RISK_MANAGEMENT (2 controls)
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art21-1",
        category=CATEGORY_RISK_MANAGEMENT,
        title="Cybersecurity Risk-Management Measures",
        obligation_text=(
            "Member States shall ensure that essential and important entities take "
            "appropriate and proportionate technical, operational and organisational "
            "measures to manage the risks posed to the security of network and "
            "information systems which those entities use for their operations or for "
            "the provision of their services."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art21-2-a",
        category=CATEGORY_RISK_MANAGEMENT,
        title="Policies on Risk Analysis and Information System Security",
        obligation_text=(
            "Entities shall implement policies on risk analysis and information system "
            "security as part of the cybersecurity risk-management measures required "
            "under Article 21(1)."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    # CATEGORY_INCIDENT_HANDLING (2 controls)
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art23-1",
        category=CATEGORY_INCIDENT_HANDLING,
        title="Significant Incident Notification to CSIRT",
        obligation_text=(
            "Essential and important entities shall notify, without undue delay, the "
            "CSIRT or, where applicable, the competent authority of any significant "
            "incident. Where applicable, those entities shall notify the recipients of "
            "their services of significant incidents that are likely to adversely affect "
            "the provision of those services."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art23-4",
        category=CATEGORY_INCIDENT_HANDLING,
        title="Incident Notification Timeline and Early Warning",
        obligation_text=(
            "An early warning notification shall be submitted without undue delay, and "
            "in any event within 24 hours of becoming aware of the significant incident. "
            "A full incident notification shall be submitted no later than 72 hours after "
            "becoming aware of the significant incident."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    # CATEGORY_SUPPLY_CHAIN (2 controls)
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art21-2-d",
        category=CATEGORY_SUPPLY_CHAIN,
        title="Supply Chain Security Measures",
        obligation_text=(
            "Entities shall address security in supply chain measures, including "
            "security-related aspects concerning the relationships between each entity "
            "and its direct suppliers or service providers."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art22-1",
        category=CATEGORY_SUPPLY_CHAIN,
        title="Coordinated Supply-Chain Security Risk Assessments",
        obligation_text=(
            "The Cooperation Group, in cooperation with the Commission and ENISA, may "
            "carry out coordinated security risk assessments of specific critical ICT "
            "services, systems or products supply chains, taking into account technical "
            "and, where relevant, non-technical risk factors."
        ),
        entity_types=[ENTITY_ESSENTIAL],
    ),
    # CATEGORY_VULNERABILITY (1 control)
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art21-2-e",
        category=CATEGORY_VULNERABILITY,
        title="Vulnerability Handling and Disclosure Policies",
        obligation_text=(
            "Entities shall implement policies and procedures to assess the effectiveness "
            "of cybersecurity risk-management measures, including vulnerability handling "
            "and disclosure."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    # CATEGORY_AI_OVERSIGHT (1 control)
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art21-2-j",
        category=CATEGORY_AI_OVERSIGHT,
        title="Use of Secure and Trustworthy ICT Solutions",
        obligation_text=(
            "Entities shall implement the use of multi-factor authentication or "
            "continuous authentication solutions, secured voice, video and text "
            "communications, and secured emergency communication systems within the "
            "entity where appropriate, including oversight of automated and AI-assisted "
            "decision-making in security-relevant contexts."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    # CATEGORY_OPERATIONAL_RESILIENCE (2 controls)
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art21-2-b",
        category=CATEGORY_OPERATIONAL_RESILIENCE,
        title="Incident Handling and Business Continuity",
        obligation_text=(
            "Entities shall implement incident handling measures and business continuity "
            "plans, including backup management and disaster recovery, and crisis "
            "management."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    ControlInput(
        framework=FRAMEWORK_NIS2,
        control_ref="NIS2-Art21-2-c",
        category=CATEGORY_OPERATIONAL_RESILIENCE,
        title="Backup, Recovery and Crisis Management",
        obligation_text=(
            "Entities shall implement business continuity measures such as backup "
            "management and disaster recovery, and crisis management, to ensure that "
            "the continuity of services is maintained in the event of a significant "
            "cybersecurity incident."
        ),
        entity_types=[ENTITY_ESSENTIAL],
    ),
]

# ---------------------------------------------------------------------------
# Target controls — controls from other frameworks that NIS2 maps onto
# ---------------------------------------------------------------------------

_TARGET_CONTROLS: list[ControlInput] = [
    ControlInput(
        framework=FRAMEWORK_DORA,
        control_ref="DORA-Art9",
        category=CATEGORY_RISK_MANAGEMENT,
        title="DORA ICT Risk Management Framework",
        obligation_text=(
            "Financial entities shall have in place a comprehensive ICT risk management "
            "framework as part of their overall risk management system that enables them "
            "to address ICT risk quickly, efficiently and comprehensively."
        ),
        entity_types=[ENTITY_ESSENTIAL],
    ),
    ControlInput(
        framework=FRAMEWORK_DORA,
        control_ref="DORA-Art17",
        category=CATEGORY_INCIDENT_HANDLING,
        title="DORA ICT-Related Incident Reporting",
        obligation_text=(
            "Financial entities shall report major ICT-related incidents to the relevant "
            "competent authority within the timeframes specified and shall submit an "
            "initial notification, intermediate report, and final report."
        ),
        entity_types=[ENTITY_ESSENTIAL],
    ),
    ControlInput(
        framework=FRAMEWORK_CRA,
        control_ref="CRA-Art13",
        category=CATEGORY_VULNERABILITY,
        title="CRA Vulnerability Handling Requirements",
        obligation_text=(
            "Manufacturers shall identify and document vulnerabilities and components "
            "contained in the product with digital elements, including by drawing up a "
            "software bill of materials, and shall put in place a coordinated vulnerability "
            "disclosure policy."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
    ControlInput(
        framework=FRAMEWORK_AI_ACT,
        control_ref="AI-Act-Art9",
        category=CATEGORY_AI_OVERSIGHT,
        title="AI Act Risk Management System",
        obligation_text=(
            "A risk management system shall be established, implemented, documented and "
            "maintained in relation to high-risk AI systems. The risk management system "
            "shall be a continuous iterative process run throughout the entire lifecycle "
            "of a high-risk AI system."
        ),
        entity_types=[ENTITY_ESSENTIAL, ENTITY_IMPORTANT],
    ),
]

# ---------------------------------------------------------------------------
# Crosswalk definitions — (source_ref, target_ref, relationship, note)
# ---------------------------------------------------------------------------

_CROSSWALKS = [
    (
        "NIS2-Art21-1",
        FRAMEWORK_NIS2,
        "DORA-Art9",
        FRAMEWORK_DORA,
        RELATIONSHIP_EQUIVALENT,
        "Both require a comprehensive organisational risk-management framework; "
        "DORA specialises this for financial ICT risk.",
    ),
    (
        "NIS2-Art21-2-e",
        FRAMEWORK_NIS2,
        "CRA-Art13",
        FRAMEWORK_CRA,
        RELATIONSHIP_PARTIAL,
        "NIS2 requires vulnerability handling policies; CRA additionally mandates "
        "a software bill of materials and coordinated disclosure policy.",
    ),
    (
        "NIS2-Art21-2-j",
        FRAMEWORK_NIS2,
        "AI-Act-Art9",
        FRAMEWORK_AI_ACT,
        RELATIONSHIP_EQUIVALENT,
        "Both address risk management for automated and AI-assisted systems in "
        "security-sensitive contexts.",
    ),
    (
        "NIS2-Art21-2-b",
        FRAMEWORK_NIS2,
        "DORA-Art17",
        FRAMEWORK_DORA,
        RELATIONSHIP_RELATED,
        "NIS2 covers incident handling and business continuity broadly; DORA "
        "focuses the incident reporting obligation on financial entities.",
    ),
    (
        "NIS2-Art21-2-d",
        FRAMEWORK_NIS2,
        "CRA-Art13",
        FRAMEWORK_CRA,
        RELATIONSHIP_RELATED,
        "NIS2 supply-chain security includes software components; CRA SBOM "
        "requirements are a concrete implementation of that obligation.",
    ),
]


# ---------------------------------------------------------------------------
# Database connection wrapper
# ---------------------------------------------------------------------------


class _SeedConn:
    """Wraps a psycopg2 connection and converts :name params to %(name)s format.

    control_service uses SQLAlchemy-style :name placeholders. psycopg2 uses
    %(name)s. This wrapper converts them at execute() time so that control_service
    functions work without modification in the seed script context.
    """

    def __init__(self, pg_conn) -> None:
        """Store the underlying psycopg2 connection."""
        self._conn = pg_conn

    def execute(self, sql: str, params=None):
        """Convert :name placeholders to %(name)s, then execute via psycopg2 cursor."""
        converted = _PARAM_RE.sub(r"%(\1)s", sql)
        cur = self._conn.cursor()
        cur.execute(converted, params)
        return cur

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    def rollback(self) -> None:
        """Roll back the current transaction."""
        self._conn.rollback()


# ---------------------------------------------------------------------------
# Seed function
# ---------------------------------------------------------------------------


def seed(conn) -> None:
    """Populate compliance_controls and control_crosswalks with NIS2 seed data.

    Idempotent: existing (framework, control_ref) pairs are skipped silently.
    Existing crosswalk pairs are also skipped. Safe to run more than once.
    """
    load_controls(conn, _NIS2_CONTROLS)
    load_controls(conn, _TARGET_CONTROLS)
    _seed_crosswalks(conn)


def _seed_crosswalks(conn) -> None:
    """Look up control IDs by ref and insert the five crosswalk rows.

    Uses list_controls() per framework to build lookup dicts, then calls
    add_crosswalk() for each defined pair. add_crosswalk() skips duplicates.
    """
    nis2_by_ref = _build_ref_index(conn, FRAMEWORK_NIS2)
    dora_by_ref = _build_ref_index(conn, FRAMEWORK_DORA)
    cra_by_ref = _build_ref_index(conn, FRAMEWORK_CRA)
    ai_act_by_ref = _build_ref_index(conn, FRAMEWORK_AI_ACT)

    framework_index = {
        FRAMEWORK_NIS2: nis2_by_ref,
        FRAMEWORK_DORA: dora_by_ref,
        FRAMEWORK_CRA: cra_by_ref,
        FRAMEWORK_AI_ACT: ai_act_by_ref,
    }

    for src_ref, src_fw, tgt_ref, tgt_fw, relationship, note in _CROSSWALKS:
        source_id = framework_index[src_fw].get(src_ref)
        target_id = framework_index[tgt_fw].get(tgt_ref)
        if source_id is None or target_id is None:
            print(f"WARNING: skipping crosswalk {src_ref}→{tgt_ref}: ID not found")
            continue
        add_crosswalk(conn, str(source_id), str(target_id), relationship, note)


def _build_ref_index(conn, framework: str) -> dict:
    """Return a dict mapping control_ref to control_id for the given framework.

    Queries list_controls() filtered by framework (which always filters
    is_active=True). Inactive controls are excluded from crosswalk resolution.
    """
    controls = list_controls(conn, framework=framework)
    return {c["control_ref"]: c["control_id"] for c in controls}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    pg_conn = psycopg2.connect(database_url)
    try:
        wrapped = _SeedConn(pg_conn)
        seed(wrapped)
        wrapped.commit()
        print("Seed completed successfully.")
    except Exception as exc:
        pg_conn.rollback()
        print(f"ERROR: seed failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        pg_conn.close()
