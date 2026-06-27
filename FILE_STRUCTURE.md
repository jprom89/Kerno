# FILE_STRUCTURE.md — Canonical Directory Tree

**Status:** Baseline v1.1 (Gap-Fixed)
**Rule:** Every file has exactly one correct home. Update this document before creating any new top-level directory.

---

## Root

```
kerno/
├── CLAUDE.md                          ← Constitution — read first, every session
├── FILE_STRUCTURE.md                  ← This file — canonical directory map
├── LEARNING_PIPELINE_SPEC.md          ← Document #8: Core learning pipeline architecture
├── PROMPT_doc8_learning_pipeline.md   ← Build instructions for Document #8 implementation
├── CLAUDE_STARTER_PROMPT.md           ← Ready-to-paste Claude session opener
├── README.md                          ← Public-facing project overview
├── .env.example                       ← Environment variable template (no secrets)
├── pytest.ini                         ← Test runner configuration
├── pyproject.toml                     ← Project metadata and dependencies
└── docker-compose.yml                 ← Local development environment
```

---

## Source Code

```
src/
├── __init__.py
│
├── models/                            ← Database models (SQLAlchemy / Pydantic)
│   ├── __init__.py
│   ├── tenant.py                      ← Tenant record, UUIDv4 registration (KER-101)
│   ├── embedding.py                   ← Vector embedding storage schema
│   ├── control.py                     ← Compliance control record
│   ├── override.py                    ← Human override capture record (KER-106)
│   ├── audit_log.py                   ← Immutable override audit log (KER-107)
│   └── retrieval_bias.py              ← Per-tenant retrieval bias vector record (KER-114)
│
├── services/                          ← Business logic — no framework dependencies
│   ├── __init__.py
│   ├── anonymisation.py               ← Anonymisation pipeline — strips PII before telemetry (KER-102)
│   ├── embedding_service.py           ← Embedding generation and pgvector storage (KER-103)
│   ├── retrieval_service.py           ← RAG query execution with tenant bias injection (KER-104)
│   ├── recommendation_service.py      ← AI recommendation engine stub (KER-105)
│   ├── override_service.py            ← Override capture, weighting, audit emission (KER-106/107)
│   ├── bias_recalculation_service.py  ← Nightly batch: recalculates retrieval_bias_vector (KER-114)
│   └── tenant_context.py              ← set_tenant_context() — must be called before every query
│
├── api/                               ← HTTP layer (FastAPI routers)
│   ├── __init__.py
│   ├── routes/
│   │   ├── controls.py                ← Control mapping endpoints
│   │   ├── overrides.py               ← Override submission endpoints
│   │   ├── evidence.py                ← Evidence pack export (KER-111)
│   │   └── health.py                  ← Health check endpoint
│   ├── dependencies.py                ← Auth session resolution, tenant_id extraction
│   └── middleware.py                  ← Request logging, error handling
│
├── integrations/                      ← Third-party integrations
│   ├── __init__.py
│   └── jira/
│       ├── __init__.py
│       ├── panel.py                   ← Jira side-panel rendering (KER-108)
│       └── webhook.py                 ← Inbound Jira webhook handler
│
├── db/                                ← Database connection and transaction management
│   ├── __init__.py
│   ├── connection.py                  ← Connection pool, transaction context manager
│   └── rls.py                         ← RLS policy helpers, set_tenant_context() implementation
│
└── scheduler/                         ← Background jobs
    ├── __init__.py
    └── nightly_bias_recalculation.py  ← Cron job: triggers bias_recalculation_service (KER-114)
```

---

## Configuration

```
config/
├── __init__.py
├── constants.py                       ← All magic numbers with documented origins
├── settings.py                        ← Environment-driven settings (Pydantic BaseSettings)
└── logging.py                         ← Structured logging configuration
```

---

## Database Migrations

```
migrations/
├── env.py                             ← Alembic environment
├── script.py.mako
└── versions/
    ├── 001_create_tenant_table.py
    ├── 002_create_embedding_table_with_rls.py
    ├── 003_create_override_table.py
    ├── 004_create_audit_log_table.py
    └── 005_create_retrieval_bias_table.py
```

---

## Scripts

```
scripts/
├── seed_nis2_controls.py              ← One-time seed: NIS2 control catalogue
└── seed_dev_tenant.py                 ← Dev-only seed: admin@kerno.local credentials
```

---

## Tests

Mirror the `src/` structure. Every service has a matching test file.

```
tests/
├── conftest.py                        ← Shared fixtures: test DB, tenant factory, session mocks
├── security/
│   └── test_tenant_isolation.py       ← KER-113: Cross-tenant isolation tests (MUST-HAVE)
├── unit/
│   ├── services/
│   │   ├── test_anonymisation.py
│   │   ├── test_embedding_service.py
│   │   ├── test_retrieval_service.py
│   │   ├── test_recommendation_service.py
│   │   ├── test_override_service.py
│   │   ├── test_bias_recalculation_service.py
│   │   └── test_tenant_context.py
│   └── models/
│       ├── test_tenant.py
│       ├── test_override.py
│       └── test_retrieval_bias.py
└── integration/
    ├── test_rag_pipeline_end_to_end.py
    ├── test_override_to_bias_pipeline.py
    └── test_evidence_pack_export.py
```

---

## Data Classification Quick Reference

| Directory / File | Data Class | Crosses Tenant Boundary? |
|---|---|---|
| `src/models/embedding.py` | Tenant-Specific Context | Never |
| `src/models/override.py` | Tenant-Specific Context | Never |
| `src/models/audit_log.py` | Tenant-Specific Context | Never |
| `src/services/anonymisation.py` | Processes both; output is Low-Sensitivity | Output only, after stripping |
| Central analytics table | Cross-Tenant Telemetry | Yes, after anonymisation |

