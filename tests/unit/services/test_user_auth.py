"""Unit tests for per-user authentication in src/services/auth_service.py (KER-202).

Nine tests cover the scrypt hash round-trip, the users-table lookup query, the four
credential-failure paths (unknown email, wrong password, inactive user, missing hash),
and the claims of the issued JWT (sub=user_id, email, role, tenant_id). A spy connection
serves a canned user row; no database is touched.
"""

from __future__ import annotations

import os
import uuid

import jwt

from src.services.auth_service import (
    authenticate_and_issue_token,
    hash_password,
    _verify_password,
)

_JWT_SECRET = "test-secret-for-unit-tests"
os.environ["KERNO_JWT_SECRET"] = _JWT_SECRET

_USER_ID = str(uuid.UUID("d0000000-0000-4000-d000-000000000004"))
_TENANT_ID = str(uuid.UUID("c0000000-0000-4000-a000-000000000003"))
_PASSWORD = "correct horse battery staple"


# ── Spy connection ─────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class _UserSpyConn:
    """Returns a single canned users row for the login SELECT; records the SQL."""

    def __init__(self, row):
        self.calls = []
        self._row = row

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Result(self._row)


def _user_row(password: str = _PASSWORD, role: str = "vciso", is_active: bool = True):
    return (_USER_ID, _TENANT_ID, hash_password(password), role, is_active)


# ── Password hashing ───────────────────────────────────────────────────────────


def test_hash_password_round_trips():
    stored = hash_password(_PASSWORD)
    assert stored.startswith("scrypt:")
    assert _verify_password(_PASSWORD, stored) is True
    assert _verify_password("wrong", stored) is False


# ── Login lookup + success ─────────────────────────────────────────────────────


def test_login_queries_users_table_by_email():
    spy = _UserSpyConn(_user_row())
    authenticate_and_issue_token(spy, "Admin@Example.com ", _PASSWORD)
    sql, params = spy.calls[0]
    assert "FROM users" in sql
    assert params["email"] == "admin@example.com"  # normalised (lowercased/stripped)


def test_valid_credentials_issue_token_with_user_claims():
    spy = _UserSpyConn(_user_row(role="compliance_lead"))
    token = authenticate_and_issue_token(spy, "u@x.io", _PASSWORD)
    assert token is not None
    claims = jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])
    assert claims["sub"] == _USER_ID
    assert claims["user_id"] == _USER_ID
    assert claims["role"] == "compliance_lead"
    assert claims["tenant_id"] == _TENANT_ID
    assert claims["email"] == "u@x.io"


# ── Failure paths (all return None, never raise) ────────────────────────────────


def test_unknown_email_returns_none():
    spy = _UserSpyConn(None)
    assert authenticate_and_issue_token(spy, "nobody@x.io", _PASSWORD) is None


def test_wrong_password_returns_none():
    spy = _UserSpyConn(_user_row())
    assert authenticate_and_issue_token(spy, "u@x.io", "wrong-password") is None


def test_inactive_user_returns_none():
    spy = _UserSpyConn(_user_row(is_active=False))
    assert authenticate_and_issue_token(spy, "u@x.io", _PASSWORD) is None


def test_missing_password_hash_returns_none():
    spy = _UserSpyConn((_USER_ID, _TENANT_ID, None, "vciso", True))
    assert authenticate_and_issue_token(spy, "u@x.io", _PASSWORD) is None


def test_failure_paths_do_not_raise():
    # Each failure path must return None rather than raising, so the router can
    # map every failure to a uniform 401 without leaking which field was wrong.
    for spy in (_UserSpyConn(None), _UserSpyConn(_user_row(is_active=False))):
        assert authenticate_and_issue_token(spy, "u@x.io", "x") is None


def test_role_is_carried_verbatim_from_the_user_row():
    spy = _UserSpyConn(_user_row(role="auditor"))
    token = authenticate_and_issue_token(spy, "u@x.io", _PASSWORD)
    claims = jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])
    assert claims["role"] == "auditor"
