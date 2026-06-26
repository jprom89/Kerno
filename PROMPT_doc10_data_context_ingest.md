# PROMPT_doc10_data_context_ingest.md
# Document 10 — Data Context: Jira + CMDB Connector Ingest
# Spec version: 1.0 | Status: Authoritative
# Covers: KER-102
# Supersedes: any inline description of KER-102 in Claude prompts

---

## 1. Purpose

This document specifies Document 10 of the Kerno compliance copilot codebase.

Document 10 implements the Data Context layer — the first of the four architectural
layers in Kerno's pipeline. It provides the ingest path that populates the context
store with raw material the Decision layer (Documents 11–13) will reason over.

Without this layer, the Decision layer has nothing to classify, and the learning
pipeline (Documents 7–9) has no source embeddings to calibrate against.

The four-layer architecture is:
  1. Data Context (ingest)        ← Document 10
  2. Decision (classify, score)   ← Documents 11–13
  3. Feedback (approve, override) ← Documents 7–9 (already built)
  4. Interface (Trust Center)     ← Future

---

## 2. Scope — KER-102 acceptance criteria (authoritative)

Source: Kerno Sprint 1 Backlog, KER-102.

The implementation is complete when all four acceptance criteria pass:

  AC-1: Given valid connector credentials, when a sync runs, then Jira issues and
        CMDB assets are normalised into the canonical context schema.

  AC-2: Given a second sync, when records are unchanged, then they are deduplicated
        and not re-written (idempotent, watermark-based).

  AC-3: Connector failures are retried with backoff and surfaced as an
        ingest-health status.

  AC-4: Each ingested record stores provenance (source system, external ID,
        fetch timestamp).

---

## 3. Canonical context schema

Every ingested record — whether from Jira or CMDB — is normalised into a single
canonical shape before it enters the context store. This is the authoritative
definition of that shape.

### 3.1 ContextRecord (the canonical unit)

| Field         | Type           | Required | Notes |
|---------------|----------------|----------|-------|
| record_id     | UUID (v4)      | Yes      | Generated in Python (uuid.uuid4()) |
| tenant_id     | UUID (v4)      | Yes      | FK → tenants |
| source_system | str            | Yes      | "jira" or "cmdb" |
| external_id   | str            | Yes      | ID in the source system |
| record_type   | str            | Yes      | "issue", "asset", or "document" |
| title         | str            | Yes      | Short human-readable label |
| body          | str or None    | No       | Full text content; nullable |
| metadata      | dict[str, Any] | No       | Source-specific fields; stored as JSONB |
| fetched_at    | datetime (UTC) | Yes      | Timestamp when fetched from source |
| content_hash  | str            | Yes      | SHA-256 of (external_id + body or title); deduplication key |
| is_deleted    | bool           | Yes      | Soft-delete flag |

### 3.2 IngestWatermark (tracks sync position per connector)

| Field          | Type           | Required | Notes |
|----------------|----------------|----------|-------|
| watermark_id   | UUID (v4)      | Yes      | Generated in Python |
| tenant_id      | UUID (v4)      | Yes      | FK → tenants |
| source_system  | str            | Yes      | "jira" or "cmdb" |
| last_synced_at | datetime (UTC) | Yes      | Updated after each successful sync |
| last_cursor    | str or None    | No       | Connector-specific cursor |

### 3.3 ConnectorHealth (tracks ingest health per connector)

| Field         | Type           | Required | Notes |
|---------------|----------------|----------|-------|
| health_id     | UUID (v4)      | Yes      | Generated in Python |
| tenant_id     | UUID (v4)      | Yes      | FK → tenants |
| source_system | str            | Yes      | |
| status        | str            | Yes      | "ok", "degraded", or "error" |
| last_error    | str or None    | No       | Most recent error message; nullable |
| checked_at    | datetime (UTC) | Yes      | |

---

## 4. Files in scope

### 4.1 src/models/context_record.py — NEW

ORM model for ContextRecord (§3.1).
- All fields as defined in §3.1.
- content_hash computed from (external_id + body) using SHA-256; expose a
  @staticmethod compute_hash(external_id, body) helper.
- RLS is enforced at query time via tenant_context; the model itself does not
  issue queries.
- Define SOURCE_JIRA = "jira" and SOURCE_CMDB = "cmdb" as module-level constants.

### 4.2 src/models/ingest_watermark.py — NEW

ORM model for IngestWatermark (§3.2).

### 4.3 src/models/connector_health.py — NEW

ORM model for ConnectorHealth (§3.3).
- Define HEALTH_OK = "ok", HEALTH_DEGRADED = "degraded", HEALTH_ERROR = "error"
  as module-level constants.

### 4.4 src/services/ingest_service.py — NEW

The core ingest orchestrator. This is the service the connectors call.

Responsibilities:
- upsert_record(conn, tenant_id, record: ContextRecordInput) → str
    Accepts a normalised ContextRecordInput, computes content_hash, and either
    inserts a new row or skips if content_hash matches an existing row for the
    same (tenant_id, source_system, external_id) tuple (idempotency per AC-2).
    Returns the record_id (new or existing).
- get_watermark(conn, tenant_id, source_system) → IngestWatermark | None
- set_watermark(conn, tenant_id, source_system, last_synced_at, last_cursor)
- set_health(conn, tenant_id, source_system, status, last_error=None)
- get_health(conn, tenant_id, source_system) → ConnectorHealth | None

Rules:
- conn is always a raw connection using conn.execute(sql, dict) with :name-style params.
- resolve_and_set_tenant_context(conn, tenant_id) must be called before any DB operation.
- Raise TenantContextMissingError (from src.exceptions) if tenant_id is None or "".
- UUIDs generated via uuid.uuid4() in Python.
- All functions under 40 lines; factor helpers if needed.

### 4.5 src/connectors/jira_connector.py — NEW

The Jira ingest connector.

Responsibilities:
- fetch_and_ingest(conn, tenant_id, credentials: JiraCredentials,
                  ingest_svc: IngestService) → IngestResult
    1. Reads watermark via ingest_svc.get_watermark().
    2. Fetches Jira issues updated since last_cursor using JQL `updated >= "<cursor>"`.
       Endpoint: GET /rest/api/3/search. Pagination: startAt + maxResults = JIRA_PAGE_SIZE.
    3. Normalises each issue into ContextRecordInput and calls ingest_svc.upsert_record().
    4. On completion, calls ingest_svc.set_watermark().
    5. On failure, calls ingest_svc.set_health() with status "error" then re-raises.
    6. On success, calls ingest_svc.set_health() with status "ok".
    7. Returns IngestResult (records_fetched, records_upserted, skipped).

- Retry policy: transient HTTP errors retried up to MAX_CONNECTOR_RETRIES times
  with exponential backoff starting at CONNECTOR_RETRY_BACKOFF_SECONDS.
- JiraCredentials: dataclass with base_url, api_token, project_key.

### 4.6 src/connectors/cmdb_connector.py — NEW

The CMDB ingest connector. Models a generic CMDB HTTP API.

Same pattern as Jira connector but:
- Endpoint: GET /api/v1/assets with query param updated_since=<cursor>.
- Pagination: cursor-based using a next_cursor field in the response.
- CmdbCredentials: dataclass with base_url, api_token.

### 4.7 migrations/versions/007_create_context_tables.py — NEW

Creates context_records, ingest_watermarks, connector_health tables.

- revision chains after migration 006.
- upgrade():
    - Creates context_records. Unique constraint on (tenant_id, source_system, external_id).
    - Creates ingest_watermarks. Unique constraint on (tenant_id, source_system).
    - Creates connector_health. Unique constraint on (tenant_id, source_system).
    - Enables RLS on context_records (same current_setting pattern as prior migrations).
    - ingest_watermarks and connector_health: RLS not required (internal ops tables).
- downgrade(): drops all three tables in reverse dependency order.
- Full module docstring: What / Why / How.
- All functions under 40 lines.

### 4.8 config/constants.py — extend only

Add if not already present:
- JIRA_PAGE_SIZE = 50
- MAX_CONNECTOR_RETRIES = 3
- CONNECTOR_RETRY_BACKOFF_SECONDS = 2

Do not remove or rename existing constants.

### 4.9 tests/unit/services/test_ingest_service.py — NEW

Unit tests for ingest_service.py. No live database.

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_upsert_new_record_inserts_row | New record inserted; record_id returned |
| 2 | test_upsert_duplicate_record_skipped | Same hash → no second INSERT; same record_id |
| 3 | test_content_hash_change_triggers_upsert | New body → new row inserted |
| 4 | test_tenant_context_set_before_upsert | SET LOCAL before INSERT in SQL log |
| 5 | test_none_tenant_raises | TenantContextMissingError on tenant_id=None |
| 6 | test_empty_tenant_raises | TenantContextMissingError on tenant_id="" |
| 7 | test_watermark_round_trip | set_watermark then get_watermark returns correct values |
| 8 | test_health_round_trip | set_health then get_health returns correct status |
| 9 | test_no_sqlalchemy_session_api | conn.add() and conn.flush() never called |
| 10 | test_provenance_fields_persisted | source_system, external_id, fetched_at in INSERT SQL |

### 4.10 tests/unit/connectors/test_jira_connector.py — NEW

Unit tests for jira_connector.py. Mock the HTTP layer and ingest_service.

| # | Test name | What it asserts |
|---|---|---|
| 1 | test_issues_fetched_and_upserted | N issues → N calls to upsert_record |
| 2 | test_watermark_updated_after_sync | set_watermark called with most-recent timestamp |
| 3 | test_pagination_fetches_all_pages | Two pages → both fetched; total count correct |
| 4 | test_http_error_sets_health_error | 500 response → set_health called with "error" |
| 5 | test_success_sets_health_ok | Clean run → set_health called with "ok" |
| 6 | test_retry_on_transient_error | 503 then 200 → retried; upsert called; health "ok" |
| 7 | test_max_retries_exhausted_raises | MAX_CONNECTOR_RETRIES failures → exception raised |
| 8 | test_cursor_passed_to_jql | Watermark cursor in JQL updated >= "<cursor>" |
| 9 | test_no_watermark_fetches_all | No watermark → no updated >= filter in JQL |
| 10 | test_ingest_result_counts_correct | records_fetched, upserted, skipped counts match |

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

## 6. Out of scope for Document 10

- Confluence / SharePoint connectors (future).
- ServiceNow connector (future).
- Full-text search indexing of ingested records (Document 12).
- Embedding generation (Document 12).
- Decision layer reasoning (Documents 11–13).
- Any UI or API layer exposing ingest status.

---

## 7. Authoritative references

| Document | Authority |
|---|---|
| CLAUDE.md (current version in working directory) | Highest |
| This file (PROMPT_doc10_data_context_ingest.md) | Authoritative for Document 10 scope |
| Kerno_Sprint1_Backlog.pdf KER-102 | Source of acceptance criteria |
