"""DORA-V2-001 — tenant isolation and relational integrity of the organisation tables.

What:  proves, against a live PostgreSQL database and under the role that owns
       the tables, that Tenant A cannot see Tenant B's organisations,
       identifiers or roles; that FORCE ROW LEVEL SECURITY is actually in
       effect rather than merely written into a migration; and that a child
       row in Tenant A cannot reference an organisation in Tenant B even when
       the service layer is bypassed entirely.
Why:   DORA_MODEL_V2.md §32 and CLAUDE.md §3. The composite foreign key on
       (tenant_id, organization_id) exists precisely so the last of those holds
       at the database level with no help from application code. A test that
       inspected the migration text for the words FORCE ROW LEVEL SECURITY
       would pass whether or not the flag was ever applied; these tests drive
       the real database instead.
How:   pytest tests/security/test_dora_organization_isolation.py -m integration -v
"""

from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

from src.services.dora_organization_service import (
    OrganizationInput,
    add_organization_identifier,
    add_organization_role,
    create_organization,
    list_organization_identifiers,
    list_organization_roles,
    list_organizations,
)

_ACTOR_A = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_ACTOR_B = uuid.UUID("d0000000-0000-4000-d000-000000000005")
_ROLE = "compliance_lead"


def _create(conn, tenant_id, name: str, actor) -> str:
    """Create one organisation for the tenant and return its id."""
    with conn.transaction():
        created = create_organization(
            conn, tenant_id, OrganizationInput(legal_name=name),
            actor_id=actor, actor_role=_ROLE,
        )
    return created.organization_id


def _count_as(conn, tenant_id, table: str, other_tenant_id) -> int:
    """Count the OTHER tenant's rows in a table while THIS tenant's context is set.

    The WHERE clause asks for the other tenant explicitly. Under a working
    FORCE RLS policy the answer must be zero even though the rows exist and
    the connecting role owns the table.
    """
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return conn.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s", [str(other_tenant_id)]
        ).fetchone()[0]


# ── A. Organisation isolation ────────────────────────────────────────────────


@pytest.mark.integration
def test_tenant_a_cannot_see_tenant_b_organizations(db_connection, tenant_a_id, tenant_b_id):
    _create(db_connection, tenant_a_id, "Alpha Bank AG", _ACTOR_A)
    _create(db_connection, tenant_b_id, "Beta Bank SA", _ACTOR_B)

    # Both directions: the policy is symmetric, and asserting only one way
    # would leave a policy that hard-codes one tenant looking correct.
    with db_connection.transaction():
        seen_by_a = {o.legal_name for o in list_organizations(db_connection, tenant_a_id)}
    with db_connection.transaction():
        seen_by_b = {o.legal_name for o in list_organizations(db_connection, tenant_b_id)}

    assert "Alpha Bank AG" in seen_by_a and "Beta Bank SA" not in seen_by_a
    assert "Beta Bank SA" in seen_by_b and "Alpha Bank AG" not in seen_by_b
    assert _count_as(db_connection, tenant_a_id, "dora_organizations", tenant_b_id) == 0


# ── B. Identifier isolation ──────────────────────────────────────────────────


@pytest.mark.integration
def test_tenant_a_cannot_see_tenant_b_identifiers(db_connection, tenant_a_id, tenant_b_id):
    org_b = _create(db_connection, tenant_b_id, "Beta Bank SA", _ACTOR_B)
    with db_connection.transaction():
        add_organization_identifier(
            db_connection, tenant_b_id, org_b, "LEI", "5493001KJTIIGC8Y1R12",
            actor_id=_ACTOR_B, actor_role=_ROLE,
        )

    # Through the service, under Tenant A: the organisation id is real, the
    # identifier is real, and Tenant A must still get nothing.
    with db_connection.transaction():
        seen_by_a = list_organization_identifiers(db_connection, tenant_a_id, org_b)
    assert seen_by_a == []
    assert _count_as(db_connection, tenant_a_id, "dora_organization_identifiers", tenant_b_id) == 0


# ── C. Role isolation ────────────────────────────────────────────────────────


@pytest.mark.integration
def test_tenant_a_cannot_see_tenant_b_roles(db_connection, tenant_a_id, tenant_b_id):
    org_b = _create(db_connection, tenant_b_id, "Beta Bank SA", _ACTOR_B)
    with db_connection.transaction():
        add_organization_role(
            db_connection, tenant_b_id, org_b, "financial_entity",
            actor_id=_ACTOR_B, actor_role=_ROLE,
        )

    with db_connection.transaction():
        seen_by_a = list_organization_roles(db_connection, tenant_a_id, org_b)
    assert seen_by_a == []
    assert _count_as(db_connection, tenant_a_id, "dora_organization_roles", tenant_b_id) == 0


# ── D. FORCE RLS — proven under the owning role, not read from the migration ─


@pytest.mark.integration
@pytest.mark.parametrize(
    "table",
    ["dora_organizations", "dora_organization_identifiers", "dora_organization_roles"],
)
def test_rls_is_enabled_and_forced_on_the_live_table(db_connection, table):
    with db_connection.transaction():
        row = db_connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s",
            [table],
        ).fetchone()
        policies = db_connection.execute(
            "SELECT count(*) FROM pg_policy WHERE polrelid = %s::regclass", [table]
        ).fetchone()[0]
    assert row is not None, f"{table} does not exist — is migration 025 applied?"
    assert row[0] is True, f"{table}: ROW LEVEL SECURITY is not enabled"
    assert row[1] is True, f"{table}: ROW LEVEL SECURITY is not FORCED"
    assert policies == 1, f"{table}: expected exactly one tenant_isolation_policy"


@pytest.mark.integration
def test_owner_role_is_bound_by_the_policy_not_just_ordinary_roles(
    db_connection, tenant_a_id, tenant_b_id
):
    # The connecting role owns these tables. Without FORCE, an owner bypasses
    # every policy on its own tables and this count would be 1. The test
    # therefore proves FORCE is in effect, under the exact role the
    # application and every other integration test uses.
    _create(db_connection, tenant_b_id, "Beta Bank SA", _ACTOR_B)
    with db_connection.transaction():
        owner = db_connection.execute(
            "SELECT pg_get_userbyid(relowner) = current_user FROM pg_class "
            "WHERE relname = 'dora_organizations'"
        ).fetchone()[0]
    assert owner is True, "this test only proves FORCE if the connecting role owns the table"
    assert _count_as(db_connection, tenant_a_id, "dora_organizations", tenant_b_id) == 0


@pytest.mark.integration
def test_no_tenant_context_yields_no_rows_rather_than_all_rows(db_connection, tenant_a_id):
    # A missing context must fail closed. On a genuinely fresh session
    # current_setting(..., true) returns NULL, the uuid comparison is NULL,
    # and NULL is not true — so the row is invisible. A fresh connection is
    # opened deliberately: a pooled/reused one comes back with the setting as
    # '' rather than unset (the §17 pool-RESET hazard), which errors on the
    # cast instead. Both are fail-closed; only the fresh case is deterministic.
    _create(db_connection, tenant_a_id, "Alpha Bank AG", _ACTOR_A)
    fresh = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cur = fresh.cursor()
        cur.execute(
            "SELECT count(*) FROM dora_organizations WHERE tenant_id = %s", [str(tenant_a_id)]
        )
        count = cur.fetchone()[0]
    finally:
        fresh.rollback()
        fresh.close()
    assert count == 0


# ── E. Cross-tenant relational integrity — the composite FK, service bypassed ─


@pytest.mark.integration
def test_identifier_in_tenant_a_cannot_reference_tenant_b_organization(
    db_connection, tenant_a_id, tenant_b_id
):
    org_b = _create(db_connection, tenant_b_id, "Beta Bank SA", _ACTOR_B)

    # Raw INSERT, no service, Tenant A context. The single-column FK on
    # organization_id alone would accept this — org_b exists. The composite
    # FK on (tenant_id, organization_id) must reject it, because
    # (tenant_a, org_b) is not a row in dora_organizations.
    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(
                """
                INSERT INTO dora_organization_identifiers
                    (organization_identifier_id, tenant_id, organization_id,
                     identifier_type, identifier_value)
                VALUES (%s, %s, %s, 'LEI', 'CROSS-TENANT-ATTEMPT')
                """,
                [str(uuid.uuid4()), str(tenant_a_id), org_b],
            )
    assert "fk_dora_organization_identifiers_organization" in str(excinfo.value)


@pytest.mark.integration
def test_role_in_tenant_a_cannot_reference_tenant_b_organization(
    db_connection, tenant_a_id, tenant_b_id
):
    org_b = _create(db_connection, tenant_b_id, "Beta Bank SA", _ACTOR_B)

    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(
                """
                INSERT INTO dora_organization_roles
                    (organization_role_id, tenant_id, organization_id, role_type)
                VALUES (%s, %s, %s, 'ict_provider')
                """,
                [str(uuid.uuid4()), str(tenant_a_id), org_b],
            )
    assert "fk_dora_organization_roles_organization" in str(excinfo.value)


@pytest.mark.integration
def test_the_composite_fk_rejects_cross_tenant_reference_even_with_rls_disabled(
    db_connection, tenant_a_id, tenant_b_id
):
    # Isolates the foreign key as the mechanism. RLS is switched off for the
    # duration of one transaction so the policy cannot be what rejects the
    # row; (tenant_a, org_b) is not a row in dora_organizations, so the
    # composite FK alone must refuse it. A single-column FK on organization_id
    # would have accepted it — org_b exists.
    #
    # The rollback is UNCONDITIONAL, in a finally. If the FK ever regressed and
    # the INSERT succeeded, a context-manager transaction would commit — and
    # leave RLS disabled on a live tenant-owned table. That must be impossible
    # for a test to do, whichever way it fails.
    org_b = _create(db_connection, tenant_b_id, "Beta Bank SA", _ACTOR_B)
    raised: Exception | None = None
    try:
        db_connection.execute("ALTER TABLE dora_organization_roles DISABLE ROW LEVEL SECURITY")
        db_connection.execute(
            """
            INSERT INTO dora_organization_roles
                (organization_role_id, tenant_id, organization_id, role_type)
            VALUES (%s, %s, %s, 'financial_entity')
            """,
            [str(uuid.uuid4()), str(tenant_a_id), org_b],
        )
    except Exception as exc:  # noqa: BLE001 — the assertion below names the expected one
        raised = exc
    finally:
        db_connection.rollback()

    assert raised is not None, "the cross-tenant row was ACCEPTED — the composite FK is not enforcing"
    assert "fk_dora_organization_roles_organization" in str(raised)

    # DISABLE ROW LEVEL SECURITY flips relrowsecurity (FORCE is a separate
    # flag it does not touch), so relrowsecurity is the one that proves the
    # DDL was rolled back. Both are asserted so the table is exactly as found.
    with db_connection.transaction():
        enabled, forced = db_connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = 'dora_organization_roles'"
        ).fetchone()
    assert enabled is True, "RLS was left DISABLED — the DDL did not roll back"
    assert forced is True


@pytest.mark.integration
def test_policy_refuses_a_write_carrying_another_tenants_id(db_connection, tenant_a_id, tenant_b_id):
    # The write side of the policy. With USING and no WITH CHECK, PostgreSQL
    # applies USING to INSERT as well, so a row claiming tenant B under tenant
    # A's context is refused outright (42501) rather than stored and hidden.
    # Section E's inserts all carry the context tenant's own id and so exercise
    # the FK; this one carries the OTHER tenant's id and exercises the policy.
    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(
                "INSERT INTO dora_organizations (organization_id, tenant_id, legal_name) "
                "VALUES (%s, %s, 'Smuggled Ltd')",
                [str(uuid.uuid4()), str(tenant_b_id)],
            )
    assert "violates row-level security policy" in str(excinfo.value)
    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_b_id)])
        smuggled = db_connection.execute(
            "SELECT count(*) FROM dora_organizations WHERE legal_name = 'Smuggled Ltd'"
        ).fetchone()[0]
    assert smuggled == 0
