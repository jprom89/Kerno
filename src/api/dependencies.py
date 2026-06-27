"""FastAPI dependencies for JWT authentication and database connections.
_ExecutableConn wraps raw psycopg2 connections to bridge the :name parameter style used by services to psycopg2's %(name)s cursor API."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Generator

import jwt
import psycopg2
import psycopg2.pool
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer

from config.constants import EMBEDDING_DIMENSION

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)
_pool: psycopg2.pool.ThreadedConnectionPool | None = None

# Converts :name placeholders to %(name)s (psycopg2 style).
# Negative lookbehind avoids matching PostgreSQL ::typename casts.
_NAMED_PARAM_RE = re.compile(r"(?<!:):([A-Za-z_]\w*)")


def _is_vector_value(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= EMBEDDING_DIMENSION
        and all(isinstance(v, (int, float)) for v in value)
    )


def _format_vector(values: list[float]) -> str:
    return "[" + ",".join(str(v) for v in values) + "]"


def _convert_named_params(sql: str, params: dict) -> tuple[str, dict]:
    adapted: dict = {}

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        value = params.get(name)
        if _is_vector_value(value):
            adapted[name] = _format_vector(value)
            return f"%({name})s::vector"
        adapted[name] = value
        return f"%({name})s"

    return _NAMED_PARAM_RE.sub(_replace, sql), adapted


class _CursorResult:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def fetchall(self) -> list:
        try:
            return self._cursor.fetchall()
        except Exception:
            return []

    def fetchone(self):
        try:
            return self._cursor.fetchone()
        except Exception:
            return None


class _ExecutableConn:
    """Bridges raw psycopg2 connections to the execute(sql, params) interface used by all services.

    psycopg2 connections have no execute() method — only cursors do. Services also use SQLAlchemy-style
    :name dict params while psycopg2 requires %(name)s. This class adapts both conventions."""

    def __init__(self, raw_conn) -> None:
        self._conn = raw_conn

    def execute(self, sql: str, params=None) -> _CursorResult:
        """Accept a list (%s positional) or dict (:name style) and execute via psycopg2 cursor."""
        cursor = self._conn.cursor()
        if isinstance(params, dict):
            converted_sql, adapted_params = _convert_named_params(sql, params)
            cursor.execute(converted_sql, adapted_params)
        else:
            cursor.execute(sql, params)
        return _CursorResult(cursor)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()


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
    """Yield an _ExecutableConn wrapping a pooled psycopg2 connection; commit on success, rollback on exception."""
    pool = _get_pool()
    raw_conn = pool.getconn()
    conn = _ExecutableConn(raw_conn)
    try:
        yield conn
        raw_conn.commit()
    except Exception:
        raw_conn.rollback()
        raise
    finally:
        pool.putconn(raw_conn)
