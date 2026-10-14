"""FastAPI application factory for the Kerno API.

What:  registers every router and exception handler, runs a startup environment
       check, serves the legacy static dashboard from /dashboard/ in development
       only, and serves FastAPI's interactive API documentation in development
       or wherever KERNO_ENABLE_DOCS=1.
Why:   one factory builds the whole app so tests can construct isolated
       instances with their own dependency overrides. Two surfaces are gated
       rather than always present: the legacy dashboard (it authenticates by
       holding a JWT in localStorage) and the API docs (/openapi.json publishes
       the full route inventory, every schema, and the endpoint docstrings
       describing our own security design). Both are useful locally and are
       liabilities on a reachable host. Their switches are deliberately
       SEPARATE — the docs have their own flag so that wanting the schema on a
       deployed host never becomes a reason to set KERNO_ENV=development there,
       which would also remount the dashboard and unlock the seed scripts.
       Startup additionally refuses a shipped .env.example CORS placeholder
       outside development, and refuses an owner, elevated or member database
       login, or the owner's KERNO_MIGRATION_DATABASE_URL, outside development
       (src/api/runtime_identity.py; a warning in development).
       Serving profile (PILOT-READY-003, src/api/serving_profile.py): "full"
       registers every router; "pilot" registers only the supported pilot
       workflow, never the docs or the legacy dashboard, and requires the
       database's runtime assignment to say "pilot" before it serves anything.
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

from config.constants import EXAMPLE_ALLOWED_ORIGINS
from src.api import runtime_identity, trust_center, webhooks
from src.api.serving_profile import FULL_PROFILE, SERVING_PROFILE_VARIABLE, resolve_serving_profile
from src.api.rate_limit import limiter
from src.api.routers import ai_decisions, coverage, evidence, export, overrides, panel, recommendations, register, remediation, scheduler, submissions
from src.api.routers import auth as auth_router
from src.exceptions import EntryNotFoundError, TenantContextMissingError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Refuse to start without the required settings, a safe CORS list and a matching runtime login and profile.

    The profile checked is the one create_app() resolved and stored on
    app.state, never a fresh read of the environment.
    """
    if not os.environ.get("KERNO_JWT_SECRET"):
        raise RuntimeError("KERNO_JWT_SECRET environment variable is not set")
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL environment variable is not set")
    _reject_example_origins()
    runtime_identity.enforce_runtime_identity(
        development=_is_development(), serving_profile=app.state.serving_profile
    )
    yield


def _reject_example_origins() -> None:
    """Refuse to start outside development if ALLOWED_ORIGINS still holds a placeholder.

    CORS runs with allow_credentials=True, so every entry in that list is an
    origin the browser will hand this API's session cookie to. `.env.example`
    ships placeholders, and `cp .env.example .env` is the documented local
    path, so the copied list reaching a real host is the expected accident
    rather than an unlikely one. Refusing at startup is the only point where
    that is still cheap to fix.

    Development is exempt on purpose: the example values are what the local
    path is supposed to produce, and failing there would break the documented
    setup instead of protecting anything.
    """
    if _is_development():
        return
    offenders = sorted(EXAMPLE_ALLOWED_ORIGINS.intersection(_allowed_origins()))
    if offenders:
        raise RuntimeError(
            "ALLOWED_ORIGINS still contains the .env.example placeholder(s) "
            f"{', '.join(offenders)}. CORS runs with credentials enabled, so these "
            "would grant browser access to an origin this deployment does not own. "
            "Set ALLOWED_ORIGINS to the real frontend origin(s), or leave it unset "
            "for no cross-origin access at all."
        )


def _allowed_origins() -> list[str]:
    """Return the cross-origin allow-list from the ALLOWED_ORIGINS env var (KER-301).

    Comma-separated origins (the Next.js dev server plus the Vercel preview and
    production domains — see .env.example). An unset or empty variable yields an
    empty list, which means no cross-origin browser access at all: CORS fails
    closed rather than open.
    """
    raw_origins = os.environ.get("ALLOWED_ORIGINS", "")
    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]


def _is_development() -> bool:
    """Return True when this process is running in the local development environment.

    Read at call time rather than import time so tests can set KERNO_ENV and
    build an app for either environment. Unset or misspelled means not
    development: the gate fails closed, matching how the seed scripts read the
    same variable (SEC-02).
    """
    return os.environ.get("KERNO_ENV", "") == "development"


def _docs_enabled() -> bool:
    """Return True when the interactive docs and the raw schema should be served.

    Development serves them, and KERNO_ENABLE_DOCS=1 opts a deployed host in
    WITHOUT setting KERNO_ENV=development — which would also remount the legacy
    localStorage-JWT dashboard and unlock both seed scripts against that
    database. One convenience request should not disarm three unrelated
    controls, which is what a single shared flag forced.

    Exactly "1" and nothing else: "true", "yes" and "TRUE" are all off, so a
    half-remembered value fails closed rather than publishing the schema.
    """
    return _is_development() or os.environ.get("KERNO_ENABLE_DOCS", "") == "1"


def _frontend_url() -> str | None:
    """Return the absolute URL of the frontend application, or None if unconfigured.

    Reads FRONTEND_URL, falling back to the first ALLOWED_ORIGINS entry. The
    value comes from the environment only — never from a query parameter, a
    Host header, or anything else in the request, because deriving it from
    request input is what would turn the root redirect into an open redirect.
    A value that is blank or is not an absolute http(s) URL yields None, so the
    caller declines to redirect rather than emitting a Location that is empty
    (a redirect loop back to the same URL) or protocol-relative (an off-origin
    redirect that merely looks relative).
    """
    target = os.environ.get("FRONTEND_URL", "").strip()
    if not target:
        origins = _allowed_origins()
        target = origins[0] if origins else ""
        if target:
            logger.warning(
                "FRONTEND_URL is not set; the root redirect is falling back to the first "
                "ALLOWED_ORIGINS entry (%s). Set FRONTEND_URL explicitly — ALLOWED_ORIGINS "
                "is an unordered allow-list, so reordering it silently repoints this redirect.",
                target,
            )
    if not target.startswith(("http://", "https://")):
        return None
    return target.rstrip("/")


def create_app(serving_profile: str | None = None) -> FastAPI:
    """Return a configured FastAPI application serving one profile, "full" or "pilot".

    The profile is resolved once, here — from the argument when given,
    otherwise from KERNO_SERVING_PROFILE — and stored on app.state; route
    registration, the docs, the legacy dashboard and the lifespan's database
    check all use that one value. Raises ServingProfileError for an unknown
    value, or for no value outside development, so the process refuses to
    start. The pilot never serves the docs or the legacy dashboard, whatever
    KERNO_ENV or KERNO_ENABLE_DOCS say.
    """
    profile = _resolve_profile(serving_profile)
    # These must be constructor arguments: FastAPI registers the documentation
    # routes at the end of __init__, so assigning the attributes afterwards is
    # a silent no-op. openapi_url is the one that matters — nulling docs_url
    # and redoc_url alone removes the two HTML viewers but leaves the schema
    # itself served at /openapi.json, which is the actual disclosure.
    serve_docs = profile == FULL_PROFILE and _docs_enabled()
    app = FastAPI(
        lifespan=_lifespan,
        openapi_url="/openapi.json" if serve_docs else None,
        docs_url="/docs" if serve_docs else None,
        redoc_url="/redoc" if serve_docs else None,
    )
    app.state.serving_profile = profile
    _configure_rate_limiting_and_cors(app)
    _include_pilot_workflow_routers(app)
    if profile == FULL_PROFILE:
        _include_full_only_routers(app)
    _register_root(app)
    _register_exception_handlers(app)
    if profile == FULL_PROFILE:
        _mount_legacy_dashboard(app)
    return app


def _configure_rate_limiting_and_cors(app: FastAPI) -> None:
    """Register the shared rate limiter (SEC-05) and the dashboard-origin CORS policy (KER-301)."""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    # Credentials are enabled for the httpOnly session cookie; an empty
    # allow-list disables CORS entirely.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def _resolve_profile(serving_profile: str | None) -> str:
    """Resolve the explicit argument, or KERNO_SERVING_PROFILE when there is none; raises ServingProfileError."""
    value = os.environ.get(SERVING_PROFILE_VARIABLE) if serving_profile is None else serving_profile
    return resolve_serving_profile(value, development=_is_development())


def _include_pilot_workflow_routers(app: FastAPI) -> None:
    """Register the routers of the supported pilot workflow (kickoff §4.1); both profiles serve them.

    The register router carries only the entry operations; its reporting
    windows live on a separate router that only the full profile registers.
    Submission windows stay: the submission-run workflow reads them.
    """
    app.include_router(auth_router.router, prefix="/api/v1/auth", tags=["auth"])
    app.include_router(register.router, prefix="/api/v1/register", tags=["register"])
    app.include_router(submissions.router, prefix="/api/v1/submissions", tags=["submissions"])
    app.include_router(overrides.router, prefix="/api/v1", tags=["overrides"])
    app.include_router(coverage.router, prefix="/api/v1/coverage", tags=["coverage"])
    app.include_router(export.router, prefix="/api/v1/export", tags=["export"])
    app.include_router(
        recommendations.router, prefix="/api/v1/recommendations", tags=["recommendations"]
    )
    app.include_router(evidence.router, prefix="/api/v1/evidence", tags=["evidence"])


def _include_full_only_routers(app: FastAPI) -> None:
    """Register the surfaces the pilot excludes (kickoff §4.3); only the full profile serves them.

    Left unregistered in the pilot, each answers 404 before any dependency
    runs: no pool lease, no business service, no external call, no ledger write.
    """
    app.include_router(register.windows_router, prefix="/api/v1/register", tags=["register"])
    app.include_router(panel.router, prefix="/api/v1/panel", tags=["panel"])
    app.include_router(remediation.router, prefix="/api/v1/remediation", tags=["remediation"])
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


def _register_root(app: FastAPI) -> None:
    """Serve GET / in both profiles: a redirect to the frontend, or a minimal service descriptor."""

    @app.get("/", include_in_schema=False)
    def root():
        """Send a browser that hit the bare API root on to the frontend application.

        The destination is resolved per request from the environment and never
        from the request itself — do not add a ?next= parameter or derive the
        target from the Host header, as either would make this an open
        redirect. When no frontend is configured the API identifies itself
        instead of redirecting, because redirecting to an empty target sends
        the browser back to this same URL forever. 302 and never 301/308: a
        permanent redirect to a mistyped FRONTEND_URL is cached by the browser
        and cannot be corrected server-side.
        """
        target = _frontend_url()
        if target is None:
            return JSONResponse(status_code=200, content={"service": "kerno-api", "status": "ok"})
        return RedirectResponse(url=target, status_code=302)


def _register_exception_handlers(app: FastAPI) -> None:
    """Map the domain errors to their status codes and keep every unexpected error's text out of responses."""

    @app.exception_handler(TenantContextMissingError)
    async def handle_tenant_context_missing(request: Request, exc: TenantContextMissingError):
        """A request reached the database layer without a tenant context: 403."""
        return JSONResponse(status_code=403, content={"detail": "tenant context required"})

    @app.exception_handler(EntryNotFoundError)
    async def handle_entry_not_found(request: Request, exc: EntryNotFoundError):
        """An id that is not this tenant's, or not well formed: the same 404 either way."""
        return JSONResponse(status_code=404, content={"detail": "entry not found"})

    @app.exception_handler(RuntimeError)
    async def handle_runtime_error(request: Request, exc: RuntimeError):
        """SEC-03: log the error in full under a correlation id; return only the id."""
        correlation_id = str(uuid.uuid4())
        logger.exception("Unhandled RuntimeError [%s]", correlation_id)
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred.", "correlation_id": correlation_id},
        )

    @app.exception_handler(Exception)
    async def handle_unhandled(request: Request, exc: Exception):
        """Any other error: a generic 500 with no detail."""
        logger.exception("Unhandled exception")
        return JSONResponse(status_code=500, content={"detail": "internal server error"})


def _mount_legacy_dashboard(app: FastAPI) -> None:
    """Serve the legacy static dashboard in development only (full profile only; the caller checks that).

    It keeps its session JWT in localStorage, where any script on the page can
    read it, and it is the only remaining consumer of that pattern — the
    Next.js dashboard uses an httpOnly cookie (KER-301). The isdir guard stays
    because src/dashboard/ has no __init__.py and is therefore not packaged by
    an installed build.
    """
    dashboard_dir = os.path.join(os.path.dirname(__file__), "..", "dashboard")
    if _is_development() and os.path.isdir(dashboard_dir):
        app.mount(
            "/dashboard",
            StaticFiles(directory=dashboard_dir, html=True),
            name="dashboard",
        )


app = create_app()
