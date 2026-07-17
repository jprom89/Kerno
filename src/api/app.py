"""FastAPI application factory for the Kerno DORA RoI API.
Registers all routers, exception handlers, a startup env check, and serves the static dashboard from /dashboard/.

Why:   one factory builds the whole app so tests can construct isolated instances
       with their own dependency overrides.
How:   run locally with: uvicorn src.api.app:app --reload --port 8000
       exercised by every test under tests/unit/api/.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.api import trust_center, webhooks
from src.api.rate_limit import limiter
from src.api.routers import ai_decisions, coverage, export, overrides, panel, register, remediation, scheduler, submissions
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


def _allowed_origins() -> list[str]:
    """Return the cross-origin allow-list from the ALLOWED_ORIGINS env var (KER-301).

    Comma-separated origins (the Next.js dev server plus the Vercel preview and
    production domains — see .env.example). An unset or empty variable yields an
    empty list, which means no cross-origin browser access at all: CORS fails
    closed rather than open.
    """
    raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]


def create_app() -> FastAPI:
    """Return a fully configured FastAPI application instance."""
    app = FastAPI(lifespan=_lifespan)

    # SEC-05: register the shared rate limiter and its 429 handler.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # KER-301: allow the dashboard's origins only. Credentials are enabled for
    # the httpOnly session cookie; an empty allow-list disables CORS entirely.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router.router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(register.router, prefix="/api/v1/register", tags=["register"])
    app.include_router(submissions.router, prefix="/api/v1/submissions", tags=["submissions"])
    app.include_router(overrides.router, prefix="/api/v1", tags=["overrides"])
    app.include_router(panel.router, prefix="/api/v1/panel", tags=["panel"])
    app.include_router(coverage.router, prefix="/api/v1/coverage", tags=["coverage"])
    app.include_router(remediation.router, prefix="/api/v1/remediation", tags=["remediation"])
    app.include_router(export.router, prefix="/api/v1/export", tags=["export"])
    app.include_router(scheduler.router, prefix="/api/v1/scheduler", tags=["scheduler"])
    app.include_router(ai_decisions.router, prefix="/api/v1", tags=["ai-decisions"])
    # Trust Center (KER-204): the status page is deliberately public (no /api/v1
    # prefix, no auth); the visibility toggle is authenticated and role-gated.
    app.include_router(trust_center.public_router, tags=["trust-center"])
    app.include_router(
        trust_center.admin_router, prefix="/api/v1/trust-center", tags=["trust-center"]
    )
    # Webhooks (KER-205): /ingest is public (HMAC-authenticated); the
    # registration/rotation management endpoints are JWT + platform_engineer.
    app.include_router(webhooks.router, prefix="/api/v1/webhooks", tags=["webhooks"])

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
        # SEC-03: never return the exception text to the caller. Log it in full
        # server-side, keyed by a correlation id the caller can quote in support.
        correlation_id = str(uuid.uuid4())
        logger.exception("Unhandled RuntimeError [%s]", correlation_id)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred.", "correlation_id": correlation_id},
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
