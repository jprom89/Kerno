"""Pydantic request and response models for the authentication endpoints.

What:  Defines LoginRequest (email + password) and TokenResponse (JWT string) for
       the POST /api/v1/auth/login endpoint.
Why:   Provides validated input parsing and typed response serialisation for the
       one endpoint that issues rather than validates JWTs.
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
