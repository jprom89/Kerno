"""auth_service.py — per-user credential verification and JWT issuance (KER-202).

What:  Verifies an email/password pair against the users table and, on success,
       issues a signed HS256 JWT carrying user_id (as sub), email, role, and
       tenant_id — the claims consumed by get_tenant_id()/require_role() in
       src/api/dependencies.py.
Why:   Sprint 1 authenticated one credential per tenant; KER-202 makes login
       per-user so overrides and the audit ledger attribute to a real person and
       RBAC can gate by role. This is the one place JWTs are minted.
How:   Call authenticate_and_issue_token(conn, email, password). Returns a JWT string
       on success, None on invalid credentials. Use hash_password(plaintext) when
       provisioning a user row (store the result in users.password_hash).
       The login lookup reads users before any tenant context exists; the users
       table is deliberately not FORCE-RLS'd for exactly this reason (migration 019).
       Run tests with: pytest tests/unit/services/test_user_auth.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

import jwt

from config.constants import (
    JWT_EXPIRY_SECONDS,
    SCRYPT_BLOCK_SIZE,
    SCRYPT_COST_FACTOR,
    SCRYPT_KEY_LENGTH,
    SCRYPT_PARALLELISM,
    SCRYPT_SALT_LENGTH,
)

# Reads users before any tenant context exists (login bootstrap). Email is unique
# per tenant, so LIMIT 1 (oldest) makes the lookup deterministic if the same
# address were ever provisioned in two tenants. (Migration 019 documents why the
# users table is not FORCE-RLS'd, which is what lets this pre-context read run.)
_SELECT_USER_BY_EMAIL = """
SELECT user_id, tenant_id, password_hash, role, is_active
FROM users
WHERE email = :email
ORDER BY created_at ASC
LIMIT 1
"""

# Dummy hash used when the email is not found, so that the verification path
# runs at the same cost regardless of whether the email exists. This prevents
# timing-based email enumeration attacks.
_DUMMY_HASH: str = ""


def _build_dummy_hash() -> str:
    """Return a dummy hash to use when an email is not found in the users table.

    Called once at module load time so the dummy hash is available for
    _dummy_verify() without recomputing it on every failed login attempt.
    """
    return hash_password("kerno_dummy_password_for_timing_consistency")


def hash_password(plaintext: str) -> str:
    """Hash a plaintext password with scrypt and a random salt. Returns a storable string.

    Format: 'scrypt:{salt_hex}:{key_hex}'. Use this function when seeding a
    tenant row; never store plaintext passwords. The stored string is verified
    by _verify_password().
    """
    salt = os.urandom(SCRYPT_SALT_LENGTH)
    key = hashlib.scrypt(
        plaintext.encode("utf-8"),
        salt=salt,
        n=SCRYPT_COST_FACTOR,
        r=SCRYPT_BLOCK_SIZE,
        p=SCRYPT_PARALLELISM,
        dklen=SCRYPT_KEY_LENGTH,
    )
    return f"scrypt:{salt.hex()}:{key.hex()}"


def _verify_password(plaintext: str, stored_hash: str) -> bool:
    """Return True if plaintext matches stored_hash; False otherwise.

    Uses hmac.compare_digest for the final comparison to guard against
    timing side-channels. Returns False (not an exception) on any format mismatch.
    """
    parts = stored_hash.split(":")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        salt = bytes.fromhex(parts[1])
        expected_key = bytes.fromhex(parts[2])
    except ValueError:
        return False
    actual_key = hashlib.scrypt(
        plaintext.encode("utf-8"),
        salt=salt,
        n=SCRYPT_COST_FACTOR,
        r=SCRYPT_BLOCK_SIZE,
        p=SCRYPT_PARALLELISM,
        dklen=SCRYPT_KEY_LENGTH,
    )
    return hmac.compare_digest(actual_key, expected_key)


def _dummy_verify() -> None:
    """Run a full password verification against a dummy hash.

    Called when the email is not found so the total time is indistinguishable
    from the case where the email exists but the password is wrong.
    """
    _verify_password("invalid_input", _DUMMY_HASH)


def _issue_jwt(user_id: str, email: str, role: str, tenant_id: str) -> str:
    """Return a signed HS256 JWT carrying the user's identity, role, and tenant.

    sub is the user_id (the verified actor), and the token also carries email,
    role (an RbacRole value consumed by require_role), and tenant_id (consumed by
    get_tenant_id). Reads KERNO_JWT_SECRET from the environment; raises RuntimeError
    if it is absent (the lifespan check in app.py normally prevents this).
    """
    secret = os.environ.get("KERNO_JWT_SECRET")
    if not secret:
        raise RuntimeError("KERNO_JWT_SECRET environment variable is not set")
    now = int(time.time())
    payload = {
        "sub": user_id,
        "user_id": user_id,
        "email": email,
        "role": role,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def authenticate_and_issue_token(conn, email: str, password: str) -> str | None:
    """Verify credentials against the users table. Return a per-user JWT or None.

    Returns None (not an exception) on any failure — unknown email, wrong password,
    or inactive user — so the caller returns a uniform 401 with no detail that
    reveals which field is wrong. The dummy verify call keeps timing consistent
    between 'email not found' and 'password wrong' paths. The users lookup runs
    before tenant context exists (login bootstrap); it is safe because the caller
    verifies the password before this function returns any token.
    """
    normalised_email = email.lower().strip()
    row = conn.execute(_SELECT_USER_BY_EMAIL, {"email": normalised_email}).fetchone()
    if row is None:
        _dummy_verify()
        return None
    user_id, tenant_id, stored_hash, role, is_active = (
        str(row[0]), str(row[1]), row[2], row[3], row[4],
    )
    if not is_active or stored_hash is None:
        _dummy_verify()
        return None
    if not _verify_password(password, stored_hash):
        return None
    return _issue_jwt(user_id, email=normalised_email, role=role, tenant_id=tenant_id)


# Build the dummy hash at module load time — not at call time — so it is
# available immediately and does not block the first failed login attempt.
_DUMMY_HASH = _build_dummy_hash()
