# PROMPT_doc11_nis2_control_mapping.md
# Document 11 — NIS2 Control-Mapping Data Model
# Spec version: 1.0 | Status: Authoritative
# Covers: KER-103
# Supersedes: any inline description of KER-103 in Claude prompts

---

## 1. Purpose

This document specifies Document 11 of the Kerno compliance copilot codebase.

Document 11 implements the NIS2 control catalogue — the library of regulatory
obligations that every other part of the Decision layer reasons over. It is the
schema backbone that connects:

  - ingested context records (Document 10)  →  evidence (Document 12)
  - evidence                                →  recommendations (Document 13)
  - recommendations                         →  human approvals (Documents 7–9)
  - approvals                               →  Trust Center display (future)

Without this document, the Decision layer has no controls to reason over and
evidence has nowhere to attach.

The four-layer architecture reminder:
  1. Data Context (ingest)        ← Document 10 (closed)
  2. Decision (classify, score)   ← Documents 11–13 ← YOU ARE HERE
  3. Feedback (approve, override) ← Documents 7–9 (closed)
  4. Interface (Trust Center)     ← Future

---

## 2. Scope — KER-103 acceptance criteria (authoritative)

Source: Kerno Sprint 1 Backlog, KER-103.

The implementation is complete when all four acceptance criteria pass:

  AC-1: Given the NIS2 control catalogue, when it is loaded, then each obligation
        is stored with ID, text, category and applicable entity type.

  AC-2: Given a control, when it overlaps another regime, then a crosswalk link
        to the DORA / CRA / AI Act equivalent is persisted.

  AC-3: Schema supports a many-to-many control-to-evidence relationship
        (for KER-104 / Document 12).

  AC-4: Migrations are reversible and covered by tests.

---

## 3. Data model

### 3.1 ComplianceControl (the canonical control record)

Every NIS2 obligation — and every equivalent from DORA, CRA, or the AI Act
surfaced via a crosswalk — is stored as one ComplianceControl row.

| Field            | Type           | Required | Notes |
|------------------|----------------|----------|-------|
| control_id       | UUID (v4)      | Yes      | Generated in Python (uuid.uuid4()) |
| framework        | str            | Yes      | FRAMEWORK_NIS2 / FRAMEWORK_DORA / FRAMEWORK_CRA / FRAMEWORK_AI_ACT |
| control_ref      | str            | Yes      | Framework-native reference code, e.g. "NIS2-6.1", "DORA-Art9" |
| category         | str            | Yes      | Control domain — see §3.4 for the authoritative list |
| title            | str            | Yes      | Short human-readable label (≤ 120 chars) |
| obligation_text  | str            | Yes      | Full regulatory obligation text |
| entity_types     | list[str]      | Yes      | Which entity types this applies to; stored as ARRAY in Postgres |
| is_active        | bool           | Yes      | False = retired or superseded; excluded from coverage calculations |
| created_at       | datetime (UTC) | Yes      | Set at INSERT; never updated |

### 3.2 ControlCrosswalk (many-to-many between controls across frameworks)

Links a source control in one framework to an equivalent or overlapping control
in another framework. One row per directional pair.

| Field            | Type           | Required | Notes |
|------------------|----------------|----------|-------|
| crosswalk_id     | UUID (v4)      | Yes      | Generated in Python |
| source_control_id| UUID (v4)      | Yes      | FK → compliance_controls |
| target_control_id| UUID (v4)      | Yes      | FK → compliance_controls |
| relationship     | str            | Yes      | RELATIONSHIP_EQUIVALENT / RELATIONSHIP_PARTIAL / RELATIONSHIP_RELATED |
| note             | str or None    | No       | Human-readable explanation of the mapping |
| created_at       | datetime (UTC) | Yes      | Set at INSERT; never updated |

### 3.3 ControlEvidenceLink (many-to-many stub for KER-104)

Document 12 owns the full evidence-linking logic. Document 11 creates this table
and its migration so that the schema is in place and reversible. The table is
populated entirely by Document 12; Document 11 writes no data to it.

| Field            | Type           | Required | Notes |
|------------------|----------------|----------|-------|
| link_id          | UUID (v4)      | Yes      | Generated in Python |
| control_id       | UUID (v4)      | Yes      | FK → compliance_controls |
| record_id        | UUID (v4)      | Yes      | FK → context_records |
| linked_by        | str            | Yes      | Actor who created the link: user ID or "system" |
| linked_at        | datetime (UTC) | Yes      | When the link was created |
| relevance_score  | float or None  | No       | 0.0–1.0 relevance score; set by the Decision layer |
| note             | str or None    | No       | Optional human annotation |

### 3.4 Authoritative category list

These are the only permitted values for ComplianceControl.category.
Store as plain strings. Define each as a module-level constant in
src/models/compliance_control.py.

| Constant name              | String value              |
|----------------------------|---------------------------|
| CATEGORY_GOVERNANCE        | "governance"              |
| CATEGORY_RISK_MANAGEMENT   | "risk_management"         |
| CATEGORY_INCIDENT_HANDLING | "incident_handling"       |
| CATEGORY_SUPPLY_CHAIN      | "supply_chain"            |
| CATEGORY_VULNERABILITY     | "vulnerability"           |
| CATEGORY_AI_OVERSIGHT      | "ai_oversight"            |
| CATEGORY_OPERATIONAL_RESILIENCE | "operational_resilience" |

These map directly to the control domains displayed in the Trust Center
Framework Coverage Matrix (Trust Center Spec §4).

### 3.5 Authoritative framework list

| Constant name         | String value  |
|-----------------------|---------------|
| FRAMEWORK_NIS2        | "nis2"        |
| FRAMEWORK_DORA        | "dora"        |
| FRAMEWORK_CRA         | "cra"         |
| FRAMEWORK_AI_ACT      | "ai_act"      |

### 3.6 Authoritative crosswalk relationship list

| Constant name              | String value  |
|----------------------------|---------------|
| RELATIONSHIP_EQUIVALENT    | "equivalent"  |
| RELATIONSHIP_PARTIAL       | "partial"     |
| RELATIONSHIP_RELATED       | "related"     |

---

## 4. Files in scope

### 4.1 src/models/compliance_control.py — NEW

ORM model for ComplianceControl (§3.1).
- All fields from §3.1.
- All category, framework, and entity_type constants defined as module-level
  constants (see §3.4, §3.5).
- entity_types stored using SQLAlchemy's ARRAY(String) with
  postgresql dialect.
- created_at set via server_default=func.now(); never updated.
- No queries issued in this file.

### 4.2 src/models/control_crosswalk.py — NEW

ORM model for ControlCrosswalk (§3.2).
- All fields from §3.2.
- Relationship constants defined as module-level constants (§3.6).
- created_at set via server_default=func.now().

### 4.3 src/models/control_evidence_link.py — NEW

ORM model for ControlEvidenceLink (§3.3).
- All fields from §3.3.
- This is a schema stub only. Document 11 creates the table and ORM model;
  Document 12 writes all data and implements all service methods that use it.
- Module docstring must make this explicit: "Populated by Document 12."

### 4.4 src/services/control_service.py — NEW

Service for reading and loading the control catalogue. All methods use
conn.execute(sql, dict) with :name-style parameters. No SQLAlchemy Session API.

Responsibilities:
- load_controls(conn, controls: list[ControlInput]) -> list[str]
    Bulk-inserts a list of ComplianceControl rows. Skips (ON CONFLICT DO NOTHING)
    rows where control_ref + framework already exist. Returns list of inserted
    control_ids. Used by the seed script (§4.6).

- get_control(conn, control_id: str) -> dict | None
    Returns a single control row as a dict, or None if not found.
    Does NOT require tenant context (controls are global, not per-tenant).

- list_controls(conn, framework: str | None = None,
               category: str | None = None,
               entity_type: str | None = None) -> list[dict]
    Returns controls filtered by any combination of framework, category,
    and entity_type. Returns all active controls if no filters are given.
    is_active = True filter is always applied.

- add_crosswalk(conn, source_control_id: str, target_control_id: str,
               relationship: str, note: str | None = None) -> str
    Inserts one ControlCrosswalk row. Returns crosswalk_id.
    Skips duplicates ON CONFLICT DO NOTHING on (source_control_id,
    target_control_id).

- get_crosswalks(conn, control_id: str) -> list[dict]
    Returns all crosswalk rows where source_control_id = control_id.

Rules:
- Controls are global (not per-tenant). resolve_and_set_tenant_context
  is NOT required in this service — controls belong to the platform, not
  to any one tenant.
- All functions under 40 lines; factor helpers if needed.
- ControlInput: a frozen dataclass with all required fields for one control row.

### 4.5 migrations/versions/008_create_control_tables.py — NEW

Creates three tables: compliance_controls, control_crosswalks,
control_evidence_links.

- revision chains after migration 007 (007_create_context_tables.py).
- upgrade():
    - Creates compliance_controls with all fields from §3.1.
      entity_types as TEXT[] (Postgres array).
      Unique constraint on (framework, control_ref).
    - Creates control_crosswalks. Unique constraint on
      (source_control_id, target_control_id).
      FK constraints to compliance_controls for both source and target.
    - Creates control_evidence_links (§3.3 stub).
      FK to compliance_controls(control_id) and context_records(record_id).
      Unique constraint on (control_id, record_id).
    - No RLS on any of these three tables. Controls are global platform data,
      not per-tenant. Evidence links are tenant-scoped but RLS is applied in
      Document 12 when the table is activated.
- downgrade(): drops all three tables in reverse dependency order.
- Full module docstring: What / Why / How (alembic upgrade / downgrade).
- All functions under 40 lines.

### 4.6 scripts/seed_nis2_controls.py — NEW

A one-time seed script that populates the compliance_controls and
control_crosswalks tables with a representative NIS2 control set and their
DORA / CRA / AI Act crosswalks.

Seed data requirements (implement all):
- Minimum 10 NIS2 controls covering all 7 categories from §3.4.
- Minimum 5 crosswalk rows linking NIS2 controls to at least two other frameworks.
- Controls must use realistic NIS2 Article references for control_ref
  (e.g. "NIS2-Art21-a", "NIS2-Art23-1").
- entity_types must use values from the authoritative list in §3.5 note:
  entity_types refers to NIS2 entity classification:
  ENTITY_ESSENTIAL = "essential" and ENTITY_IMPORTANT = "important"
  — define these two constants in src/models/compliance_control.py.

The script must be idempotent: running it twice must not create duplicate rows.

### 4.7 tests/unit/services/test_control_service.py — NEW

Unit tests for control_service.py. No live database.

Required tests (implement all):

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_load_controls_inserts_new_rows | New controls are inserted; control_ids returned |
| 2 | test_load_controls_skips_duplicates | Same (framework, control_ref) → no second INSERT |
| 3 | test_get_control_returns_dict | Known control_id → dict with expected fields |
| 4 | test_get_control_returns_none_for_unknown | Unknown ID → None |
| 5 | test_list_controls_no_filter | Returns all active controls |
| 6 | test_list_controls_by_framework | framework filter applied in SQL |
| 7 | test_list_controls_by_category | category filter applied in SQL |
| 8 | test_list_controls_inactive_excluded | is_active=False rows never returned |
| 9 | test_add_crosswalk_inserts_row | crosswalk_id returned; INSERT called |
| 10 | test_get_crosswalks_returns_list | Known source_control_id → correct rows |

Note: control_service does NOT call set_tenant_context. Tests must NOT
assert that SET LOCAL appears in conn.execute calls.

### 4.8 tests/unit/models/test_compliance_control.py — NEW

Unit tests that verify the model constants and structure without a live DB.

Required tests:

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_framework_constants_defined | FRAMEWORK_NIS2, DORA, CRA, AI_ACT all importable |
| 2 | test_category_constants_defined | All 7 CATEGORY_ constants importable |
| 3 | test_relationship_constants_defined | All 3 RELATIONSHIP_ constants importable |
| 4 | test_entity_type_constants_defined | ENTITY_ESSENTIAL, ENTITY_IMPORTANT importable |
| 5 | test_no_duplicate_constant_values | All constant values are unique strings |

---

## 5. Gate checks (apply to every file produced)

| Check | Rule |
|---|---|
| Module docstring present | Answers What, Why, and How to run or test |
| All functions have docstrings | No exceptions |
| No spec notation in variable names | No Greek letters, subscripts, raw spec symbols |
| No magic numbers | All numeric literals named in config/constants.py |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule | Controls are global — resolve_and_set_tenant_context NOT called in control_service.py. All other services follow the standard rule. |
| TenantContextMissingError from src.exceptions | N/A for control_service (no tenant context). Required in any future service that does use tenant context. |

---

## 6. What Document 11 does NOT do

- Does not load live NIS2 text from any external source (seed data is
  representative, not exhaustive — the full catalogue is a future data task).
- Does not link evidence to controls (that is Document 12 / KER-104).
- Does not generate recommendations (that is Document 13 / KER-105).
- Does not apply tenant context to control queries (controls are global).
- Does not add RLS to control_evidence_links (Document 12 owns that).

---

## 7. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md (current version in working directory) | Highest |
| This file (PROMPT_doc11_nis2_control_mapping.md) | Authoritative for Document 11 scope |
| Kerno_Sprint1_Backlog.pdf KER-103 | Source of acceptance criteria |
| Kerno_TrustCenter_Spec.pdf §4 (Framework Coverage Matrix) | Source of authoritative category list |
