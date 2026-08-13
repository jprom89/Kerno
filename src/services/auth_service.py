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

# Reads users before any tenant context exists (login bootstrap; migration 019
# documents why the users table is not FORCE-RLS'd, which is what lets this
# pre-context read run).
#
# The organisation is part of the credential (KER-408 / Ticket C1). Email is
# unique only PER TENANT — uq_users_tenant_email is UNIQUE (tenant_id, email) —
# so an email alone does not identify a user. The previous query selected the
# OLDEST matching row across all tenants, which meant a person provisioned in
# two organisations could authenticate against the wrong tenant's row and
# receive a JWT for an organisation that was not theirs. Joining on
# tenants.tenant_slug (UNIQUE, migration 021) makes the lookup exact: the pair
# (tenant_slug, email) identifies at most one user, so no ordering is needed
# and no ambiguity exists.
#
# Email is deliberately NOT globally unique: one vCISO or consultant legitimately
# holds accounts across several client organisations, which is the core persona.
_SELECT_USER_BY_SLUG_AND_EMAIL = """
SELECT u.user_id, u.tenant_id, u.password_hash, u.role, u.is_active
FROM users u
JOIN tenants t ON t.tenant_id = u.tenant_id
WHERE t.tenant_slug = :tenant_slug AND u.email = :email
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


def authenticate_and_issue_token(
    conn, email: str, password: str, tenant_slug: str
) -> str | None:
    """Verify credentials against the users table. Return a per-user JWT or None.

    The organisation slug is part of the credential, not a hint: (tenant_slug,
    email) identifies exactly one user, so authentication can never bind to a
    different organisation's account (KER-408 / Ticket C1).

    Returns None (not an exception) on any failure — unknown organisation,
    unknown email, wrong password, or inactive user — so the caller returns a
    uniform 401 that reveals which field was wrong for none of them. The dummy
    verify keeps timing consistent across every not-found path, so an attacker
    cannot enumerate valid organisation slugs any more than valid emails. The
    users lookup runs before tenant context exists (login bootstrap); it is safe
    because the password is verified before any token is returned.
    """
    normalised_email = email.lower().strip()
    normalised_slug = tenant_slug.lower().strip()
    if not normalised_slug:
        _dummy_verify()
        return None
    row = conn.execute(
        _SELECT_USER_BY_SLUG_AND_EMAIL,
        {"tenant_slug": normalised_slug, "email": normalised_email},
    ).fetchone()
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
