"""FastAPI router for the authentication endpoint mounted at /api/v1/auth.

What:  Provides a single POST /login endpoint that accepts email + password,
       delegates credential verification to auth_service, and returns a signed JWT.
Why:   The dashboard login page needs an issuance endpoint; the existing API only
       validates JWTs. This router is the entry point for all new sessions.
How:   Registered in src/api/app.py. Run tests with:
       pytest tests/unit/api/test_auth.py -v
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_conn
from src.api.schemas.auth import LoginRequest, TokenResponse
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
