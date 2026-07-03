"""FastAPI application factory for the Kerno DORA RoI API.
Registers all routers, exception handlers, a startup env check, and serves the static dashboard from /dashboard/."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.api.routers import coverage, overrides, panel, register, remediation, submissions
from src.api.routers import auth as auth_router
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

    app.include_router(auth_router.router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(register.router, prefix="/api/v1/register", tags=["register"])
    app.include_router(submissions.router, prefix="/api/v1/submissions", tags=["submissions"])
    app.include_router(overrides.router, prefix="/api/v1", tags=["overrides"])
    app.include_router(panel.router, prefix="/api/v1/panel", tags=["panel"])
    app.include_router(coverage.router, prefix="/api/v1/coverage", tags=["coverage"])
    app.include_router(remediation.router, prefix="/api/v1/remediation", tags=["remediation"])

    @app.get("/", include_in_schema=False)
    def root():
        """Redirect the bare root URL to the dashboard login page."""
        return RedirectResponse(url="/dashboard/login.html")

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

    dashboard_dir = os.path.join(os.path.dirname(__file__), "..", "dashboard")
    if os.path.isdir(dashboard_dir):
        app.mount(
            "/dashboard",
            StaticFiles(directory=dashboard_dir, html=True),
            name="dashboard",
        )

    return app


app = create_app()
