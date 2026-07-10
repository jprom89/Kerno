"""Kerno Compliance Copilot — application source root.

Subpackages:
    api/           HTTP surface: FastAPI app factory, routers, schemas, dependencies.
    models/        Database models (the shapes of stored records).
    services/      Business logic: retrieval, overrides, bias recalculation.
    db/            Connection handling and Row-Level Security helpers.
    scheduler/     Background batch jobs (nightly recalculation, log prune, DORA deadlines).

How:   run the API with: uvicorn src.api.app:app --reload --port 8000
       run all tests with: pytest
"""
