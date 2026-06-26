"""FastAPI application factory for the Kerno DORA RoI API.
Registers all routers, exception handlers, and a startup check for required env vars."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.api.routers import register, submissions
from src.exceptions import EntryNotFoundError, TenantContextMissingError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if not os.environ.get("KERNO_JWT_SECRET"):
        raise RuntimeError("KERNO_JWT_SECRET environment variable is not set")
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL environment variable is not set")
    yield


def create_app() -> FastAPI:
    """Return a fully configured FastAPI application instance."""
    app = FastAPI(lifespan=_lifespan)

    app.include_router(register.router, prefix="/api/v1/register", tags=["register"])
    app.include_router(submissions.router, prefix="/api/v1/submissions", tags=["submissions"])

    @app.exception_handler(TenantContextMissingError)
    async def handle_tenant_context_missing(request: Request, exc: TenantContextMissingError):
        return JSONResponse(status_code=403, content={"detail": "tenant context required"})

    @app.exception_handler(EntryNotFoundError)
    async def handle_entry_not_found(request: Request, exc: EntryNotFoundError):
        return JSONResponse(status_code=404, content={"detail": "entry not found"})

    @app.exception_handler(RuntimeError)
    async def handle_runtime_error(request: Request, exc: RuntimeError):
        return JSONResponse(
            status_code=500,
            content={"detail": "submission run error", "message": str(exc)},
        )

    @app.exception_handler(Exception)
    async def handle_unhandled(request: Request, exc: Exception):
        logger.exception("Unhandled exception")
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app


app = create_app()
