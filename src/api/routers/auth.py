"""FastAPI router for the authentication endpoints mounted at /api/v1/auth.

What:  POST /login accepts email + password, delegates credential verification
       to auth_service, and returns a signed JWT. GET /me (KER-301) validates
       a presented JWT and returns the email + role display strings.
Why:   The dashboard login page needs an issuance endpoint, and every dashboard
       page load re-validates the session and shows who is logged in (EU AI Act
       Article 14 — identified human actors).
How:   Registered in src/api/app.py. Run tests with:
       pytest tests/unit/api/test_auth.py -v
"""

from __future__ import annotations

import jwt
from fastapi import APIRouter, Depends, HTTPException

# _jwt_secret and _oauth2_scheme are imported so /me reads the token with the
# same security scheme and secret every other authenticated endpoint uses.
from src.api.dependencies import _jwt_secret, _oauth2_scheme, get_conn
from src.api.schemas.auth import LoginRequest, MeResponse, TokenResponse
from src.services.auth_service import authenticate_and_issue_token

router = APIRouter()


@router.post("/login")
def login(body: LoginRequest, conn=Depends(get_conn)) -> TokenResponse:
    """Authenticate with email and password, return a signed per-user JWT on success.

    Returns 401 with the detail 'invalid credentials' for any failure — unknown
    email, wrong password, or inactive user — to prevent leaking which field is
    incorrect. The service layer uses timing-consistent verification (KER-202).
    """
    token = authenticate_and_issue_token(conn, body.email, body.password)
    if token is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return TokenResponse(access_token=token, token_type="bearer")


@router.get("/me")
def me(token: str | None = Depends(_oauth2_scheme)) -> MeResponse:
    """Return the logged-in user's email and role from the verified JWT (KER-301).

    No database read: identity lives in the verified token (KER-202 — only the
    login query ever reads the users table). Any missing, expired, or invalid
    token is a uniform 401 so the dashboard's session check fails closed.
    """
    if token is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    email = payload.get("email")
    role = payload.get("role")
    if not email or not role:
        raise HTTPException(status_code=401, detail="token missing identity claims")
    return MeResponse(email=email, role=role)
