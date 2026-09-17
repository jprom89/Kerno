# PROMPT_doc12_evidence_linking.md
# Document 12 — Evidence Linking and Retrieval
# Spec version: 1.0 | Status: Authoritative
# Covers: KER-104
# Supersedes: any inline description of KER-104 in Claude prompts

---

## 1. Purpose

This document specifies Document 12 of the Kerno compliance copilot codebase.

Document 12 activates the control_evidence_links table created as a stub in
Document 11 and builds the full evidence-linking and retrieval service on top
of it. After this document, a compliance control can be linked to one or more
ingested context records (evidence), and those links can be queried, filtered,
and scored — giving the Decision layer (Document 13) the evidence it needs to
generate recommendations.

The four-layer architecture reminder:
  1. Data Context (ingest)        <- Document 10 (closed)
  2. Decision (classify, score)   <- Documents 11-13 <- YOU ARE HERE
  3. Feedback (approve, override) <- Documents 7-9 (closed)
  4. Interface (Trust Center)     <- Future

---

## 2. Scope — KER-104 acceptance criteria (authoritative)

Source: Kerno Sprint 1 Backlog, KER-104.

The implementation is complete when all four acceptance criteria pass:

  AC-1: Given a control and an ingested artifact, when I link them, then the
        relationship is persisted with who/when metadata.

  AC-2: Given a control, when I request its evidence, then all linked artifacts
        are returned with source provenance and relevance score.

  AC-3: Retrieval supports full-text and metadata filters and returns within an
        agreed latency budget.

  AC-4: Broken links (deleted source record) are flagged, never silently dropped.

---

## 3. Evidence link model — activating the stub

Document 11 created control_evidence_links as a schema stub with no RLS and
no service methods. Document 12 completes it.

### 3.1 RLS on control_evidence_links

Evidence links are per-tenant data. A tenant's engineer links their ingested
context records to controls. Another tenant must never see those links.

Document 12 adds RLS to control_evidence_links via a new migration that runs
after 008. The policy is identical in pattern to the one on context_records:
  app.current_tenant_id matches the tenant_id of the linked context_record.

Implementation note: control_evidence_links does not have a direct tenant_id
column. RLS must join through context_records to find the tenant. The policy
expression is:

  EXISTS (
    SELECT 1 FROM context_records cr
    WHERE cr.record_id = control_evidence_links.record_id
    AND cr.tenant_id = current_setting('app.current_tenant_id', true)::uuid
  )

### 3.2 EvidenceLinkStatus values

When a link is retrieved, the system checks whether the source context_record
still exists. Define these module-level constants in
src/services/evidence_service.py:

  LINK_STATUS_ACTIVE  = "active"   -- context_record exists and is not deleted
  LINK_STATUS_BROKEN  = "broken"   -- context_record is missing or is_deleted=True

AC-4 requires broken links to be flagged, never silently dropped. The retrieval
method must include broken links in results with status = LINK_STATUS_BROKEN.

---

## 4. Files in scope

### 4.1 src/services/evidence_service.py — NEW

The core evidence-linking and retrieval service.

All methods use conn.execute(sql, dict) with :name-style parameters.
No SQLAlchemy Session API.
resolve_and_set_tenant_context(conn, tenant_id) must be called before any
DB operation. Raise TenantContextMissingError (from src.exceptions) if
tenant_id is None or "".

Responsibilities:

- link_evidence(conn, tenant_id, control_id: str, record_id: str,
               linked_by: str, relevance_score: float | None = None,
               note: str | None = None) -> str
    Creates a ControlEvidenceLink row. Returns link_id.
    ON CONFLICT (control_id, record_id) DO UPDATE sets relevance_score,
    note, linked_by, linked_at to the new values (re-linking is allowed;
    it updates the existing link rather than failing).
    Validates that relevance_score is in [0.0, 1.0] if provided; raises
    ValueError if out of range.

- get_evidence_for_control(conn, tenant_id, control_id: str,
                           source_system: str | None = None,
                           record_type: str | None = None,
                           min_relevance: float | None = None
                           ) -> list[EvidenceResult]
    Returns all evidence linked to this control for this tenant.
    Each EvidenceResult contains the link fields PLUS the context_record
    fields (joined query) PLUS link_status (LINK_STATUS_ACTIVE or
    LINK_STATUS_BROKEN based on cr.is_deleted).
    Applies optional filters: source_system, record_type, min_relevance.
    Broken links (is_deleted=True or record not found) are included with
    link_status = LINK_STATUS_BROKEN — never silently dropped (AC-4).
    Results ordered by relevance_score DESC NULLS LAST, then linked_at DESC.

- get_controls_for_record(conn, tenant_id, record_id: str) -> list[dict]
    Returns all controls linked to a given context_record, as a list of
    dicts with control fields. Supports the reverse lookup.

- remove_link(conn, tenant_id, link_id: str) -> bool
    Soft-removes a link by setting a removed_at timestamp column.
    Returns True if a row was updated, False if link_id not found.
    Note: this requires adding removed_at TIMESTAMPTZ NULL to
    control_evidence_links — see migration §4.3.

- EvidenceResult: a frozen dataclass. Fields:
    link_id, control_id, record_id, linked_by, linked_at,
    relevance_score, note, link_status,
    source_system, external_id, record_type, title, body,
    fetched_at, content_hash.

Rules:
- All functions under 40 lines; factor private helpers if needed.
- LINK_STATUS_ACTIVE and LINK_STATUS_BROKEN defined as module-level constants.

### 4.2 migrations/versions/009_add_rls_to_evidence_links.py — NEW

Adds RLS to control_evidence_links and adds the removed_at column.

- revision chains after migration 008 (h3i4j5k6).
- upgrade():
    - ALTER TABLE control_evidence_links ADD COLUMN removed_at
      TIMESTAMPTZ NULL (supports soft-delete from evidence_service.remove_link).
    - ENABLE ROW LEVEL SECURITY on control_evidence_links.
    - CREATE POLICY tenant_isolation_policy using the EXISTS subquery
      from §3.1.
    - get_evidence_for_control must also filter WHERE removed_at IS NULL
      so soft-deleted links are excluded from active results.
- downgrade():
    - DROP POLICY tenant_isolation_policy.
    - DISABLE ROW LEVEL SECURITY.
    - ALTER TABLE control_evidence_links DROP COLUMN removed_at.
- Full module docstring: What / Why / How.
- All functions under 40 lines.

### 4.3 src/services/full_text_search_service.py — NEW

A lightweight full-text search helper over context_records.
Used by evidence_service to support AC-3 (full-text filter).

Responsibilities:
- search_records(conn, tenant_id, query: str,
                source_system: str | None = None,
                record_type: str | None = None,
                limit: int = FULL_TEXT_SEARCH_LIMIT) -> list[dict]
    Searches context_records using PostgreSQL's to_tsvector / plainto_tsquery
    on the title and body columns. Returns matching records ordered by
    ts_rank DESC. Applies optional source_system and record_type filters.
    Only returns records where is_deleted = False.
    resolve_and_set_tenant_context(conn, tenant_id) called first.

Rules:
- FULL_TEXT_SEARCH_LIMIT defined in config/constants.py (value: 20).
- All functions under 40 lines.

### 4.4 config/constants.py — extend only

Add if not already present:
- FULL_TEXT_SEARCH_LIMIT = 20
- RELEVANCE_SCORE_MIN = 0.0
- RELEVANCE_SCORE_MAX = 1.0

Do not remove or rename existing constants.

### 4.5 tests/unit/services/test_evidence_service.py — NEW

Unit tests for evidence_service.py. No live database.

Required tests (implement all):

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_link_evidence_inserts_row | New link inserted; link_id returned |
| 2 | test_relink_updates_existing | Same (control_id, record_id) -> UPDATE not INSERT |
| 3 | test_relevance_score_out_of_range_raises | score=1.1 -> ValueError |
| 4 | test_get_evidence_returns_active_links | Active records returned with LINK_STATUS_ACTIVE |
| 5 | test_get_evidence_includes_broken_links | is_deleted=True record -> LINK_STATUS_BROKEN included |
| 6 | test_get_evidence_filter_source_system | source_system filter appears in SQL |
| 7 | test_get_evidence_filter_min_relevance | min_relevance filter appears in SQL |
| 8 | test_get_controls_for_record_returns_list | reverse lookup returns control rows |
| 9 | test_remove_link_returns_true_on_success | removed_at set; True returned |
| 10 | test_remove_link_returns_false_if_not_found | Unknown link_id -> False |
| 11 | test_tenant_context_set_before_any_query | SET LOCAL is first SQL call |
| 12 | test_none_tenant_raises | TenantContextMissingError on None |
| 13 | test_no_sqlalchemy_session_api | conn.add() and conn.flush() never called |

### 4.6 tests/unit/services/test_full_text_search_service.py — NEW

Unit tests for full_text_search_service.py. No live database.

Required tests (implement all):

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_search_returns_matching_records | Query string appears in SQL as plainto_tsquery |
| 2 | test_search_filter_source_system | source_system filter in SQL when provided |
| 3 | test_search_filter_record_type | record_type filter in SQL when provided |
| 4 | test_search_excludes_deleted | is_deleted = False in SQL |
| 5 | test_search_limit_applied | FULL_TEXT_SEARCH_LIMIT appears in SQL |
| 6 | test_tenant_context_set_before_query | SET LOCAL is first SQL call |
| 7 | test_no_sqlalchemy_session_api | conn.add() and conn.flush() never called |

---

## 5. Gate checks (apply to every file produced)

| Check | Rule |
|---|---|
| Module docstring present | Answers What, Why, and How to run or test |
| All functions have docstrings | No exceptions |
| No spec notation in variable names | No Greek letters, subscripts, raw spec symbols |
| No magic numbers | All numeric literals named in config/constants.py |
| No function longer than 40 lines | Factor helpers if needed |
| Tenant isolation rule followed | resolve_and_set_tenant_context before any DB access |
| TenantContextMissingError from src.exceptions | Not from src.db.rls or any other location |

---

## 6. Out of scope for Document 12

- Embedding-based semantic search over context_records (retrieval_service.py
  already handles this for the learning pipeline; Document 13 will call it).
- Evidence sign-off / human approval of evidence links (Trust Center future).
- Evidence pack export (KER-111, future).
- Remediation task routing (KER-110, future).

---

## 7. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md (current version in working directory) | Highest |
| This file (PROMPT_doc12_evidence_linking.md) | Authoritative for Document 12 scope |
| Kerno_Sprint1_Backlog.pdf KER-104 | Source of acceptance criteria |
| PROMPT_doc11_nis2_control_mapping.md §3.3 | Source of control_evidence_links stub definition |
