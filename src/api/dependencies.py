"""FastAPI dependencies for JWT authentication and psycopg2 connection pooling.
Both are designed to be overridden in tests via app.dependency_overrides."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator

import jwt
import psycopg2
import psycopg2.pool
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _jwt_secret() -> str:
    secret = os.environ.get("KERNO_JWT_SECRET")
    if not secret:
        raise RuntimeError("KERNO_JWT_SECRET environment variable is not set")
    return secret


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable is not set")
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=database_url)
    return _pool


def get_tenant_id(token: str | None = Depends(_oauth2_scheme)) -> str:
    """Decode a Bearer JWT and return the tenant_id claim.
    Raises HTTP 401 if the token is missing, expired, invalid, or carries a non-UUID tenant_id."""
    if token is None:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    tenant_id = payload.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=401, detail="tenant_id claim missing")
    try:
        uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=401, detail="tenant_id is not a valid UUID")
    return tenant_id


def get_conn() -> Generator:
    """Yield a psycopg2 connection from the pool; commit on success, rollback on exception."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)
