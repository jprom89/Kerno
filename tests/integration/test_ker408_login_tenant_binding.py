"""KER-408 / Ticket C1 integration tests — login binds to the right organisation.

Reproduces the exact collision the audit found, against a live database: two
users sharing one email address in two different organisations, which the
schema permits because uq_users_tenant_email is UNIQUE (tenant_id, email) and
NOT globally unique. Before this fix the login query selected the oldest
matching row across all tenants, so the newer organisation's user either could
not log in at all or — with a matching password — authenticated into the wrong
organisation's data entirely.

A live database is required, not spies: the whole bug lives in what SQL the
driver actually returns when two rows match, which no mock can demonstrate.

Run: pytest tests/integration/test_ker408_login_tenant_binding.py -m integration -v
"""

from __future__ import annotations

import uuid

import jwt
import pytest

from src.services.auth_service import authenticate_and_issue_token, hash_password

_SHARED_EMAIL = "consultant@vciso.example"
_PASSWORD_ONE = "correct horse battery staple"
_PASSWORD_TWO = "a completely different password"

_TENANT_ONE = str(uuid.UUID("c4080000-0000-4000-c000-000000000001"))
_TENANT_TWO = str(uuid.UUID("c4080000-0000-4000-c000-000000000002"))
_SLUG_ONE = "ker408-older-org"
_SLUG_TWO = "ker408-newer-org"


@pytest.fixture
def ker408_two_orgs(db_connection):
    """Seed two organisations whose users share one email address.

    tenant_one is deliberately created with the OLDER created_at, because the
    pre-fix query ordered by it — this is what made the newer organisation
    unreachable. Passwords differ so the two accounts are genuinely distinct.
    """
    with db_connection.transaction():
        for tenant_id, slug, name in (
            (_TENANT_ONE, _SLUG_ONE, "Older Org"),
            (_TENANT_TWO, _SLUG_TWO, "Newer Org"),
        ):
            db_connection.execute(
                """INSERT INTO tenants (tenant_id, display_name, tenant_slug, is_active)
                   VALUES (%s, %s, %s, true) ON CONFLICT (tenant_id) DO NOTHING""",
                [tenant_id, name, slug],
            )
        for tenant_id, password, created in (
            (_TENANT_ONE, _PASSWORD_ONE, "2020-01-01"),
            (_TENANT_TWO, _PASSWORD_TWO, "2026-01-01"),
        ):
            db_connection.execute(
                """INSERT INTO users
                   (user_id, tenant_id, email, password_hash, role, is_active, created_at)
                   VALUES (%s, %s, %s, %s, 'vciso', true, %s)""",
                [str(uuid.uuid4()), tenant_id, _SHARED_EMAIL, hash_password(password), created],
            )

    yield

    with db_connection.transaction():
        db_connection.execute(
            "DELETE FROM users WHERE tenant_id IN (%s, %s)", [_TENANT_ONE, _TENANT_TWO]
        )
        db_connection.execute(
            "DELETE FROM tenants WHERE tenant_id IN (%s, %s)", [_TENANT_ONE, _TENANT_TWO]
        )


def _tenant_of(token: str) -> str:
    import os

    return jwt.decode(token, os.environ["KERNO_JWT_SECRET"], algorithms=["HS256"])["tenant_id"]


@pytest.mark.integration
def test_each_organisation_authenticates_into_its_own_tenant(db_connection, ker408_two_orgs):
    older = authenticate_and_issue_token(
        db_connection, _SHARED_EMAIL, _PASSWORD_ONE, _SLUG_ONE
    )
    newer = authenticate_and_issue_token(
        db_connection, _SHARED_EMAIL, _PASSWORD_TWO, _SLUG_TWO
    )

    assert older is not None and newer is not None
    assert _tenant_of(older) == _TENANT_ONE
    # THE BUG: before KER-408 this was impossible — the newer organisation's
    # user was always checked against the older row and could never log in.
    assert _tenant_of(newer) == _TENANT_TWO


@pytest.mark.integration
def test_credentials_do_not_work_against_another_organisation(db_connection, ker408_two_orgs):
    # The older org's real password, presented against the newer org's slug,
    # must fail — this is the cross-tenant access case when passwords collide.
    crossed = authenticate_and_issue_token(
        db_connection, _SHARED_EMAIL, _PASSWORD_ONE, _SLUG_TWO
    )
    assert crossed is None


@pytest.mark.integration
def test_identical_passwords_still_bind_to_the_named_organisation(db_connection, ker408_two_orgs):
    # The breach case from the audit: same email AND same password in two orgs.
    # The slug is what disambiguates, so each login lands in its own tenant.
    with db_connection.transaction():
        db_connection.execute(
            "UPDATE users SET password_hash = %s WHERE tenant_id = %s",
            [hash_password(_PASSWORD_ONE), _TENANT_TWO],
        )
    token = authenticate_and_issue_token(
        db_connection, _SHARED_EMAIL, _PASSWORD_ONE, _SLUG_TWO
    )
    assert token is not None
    assert _tenant_of(token) == _TENANT_TWO, "must never bind to the older organisation"


@pytest.mark.integration
def test_unknown_organisation_is_rejected(db_connection, ker408_two_orgs):
    assert authenticate_and_issue_token(
        db_connection, _SHARED_EMAIL, _PASSWORD_ONE, "no-such-org"
    ) is None
