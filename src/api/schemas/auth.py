"""Pydantic request and response models for the authentication endpoints.

What:  Defines LoginRequest (email + password) and TokenResponse (JWT string) for
       POST /api/v1/auth/login, and MeResponse (email + role) for
       GET /api/v1/auth/me (KER-301).
Why:   Provides validated input parsing and typed response serialisation for the
       endpoints that issue and introspect JWTs.
How:   Import these in src/api/routers/auth.py. Run related tests with:
       pytest tests/unit/api/test_auth.py -v
"""

from __future__ import annotations

from pydantic import BaseModel


class LoginRequest(BaseModel):
    """Credentials submitted to the login endpoint."""

    email: str
    password: str


class TokenResponse(BaseModel):
    """JWT bearer token returned on successful login."""

    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    """The logged-in identity as displayed by the dashboard (KER-301).

    Deliberately carries only the two non-sensitive display strings the UI
    needs — never the tenant_id, user_id, or the token itself.
    """

    email: str
    role: str
