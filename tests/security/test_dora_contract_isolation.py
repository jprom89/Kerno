"""DORA-V2-002A — tenant isolation and relational integrity of the contract tables.

What:  proves, against a live PostgreSQL database and under the role that owns
       the tables, that Tenant A cannot see Tenant B's contracts or signing
       parties in either direction; that ENABLE + FORCE ROW LEVEL SECURITY is
       in effect rather than merely written into migration 026; and that a
       party row in Tenant A cannot reference Tenant B's contract or Tenant B's
       organisation even when the service layer is bypassed and RLS is
       switched off.
Why:   DORA_MODEL_V2.md §32 and CLAUDE.md §3. The two composite foreign keys
       exist so the last of those holds in the database with no help from
       application code; only a real database can show that.
How:   pytest tests/security/test_dora_contract_isolation.py -m integration -v
"""

from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

from src.exceptions import EntryNotFoundError
from src.services.audit_log import get_entries_by_actor
from src.services.dora_contract_service import (
    ContractInput,
    ContractUpdate,
    add_contract_party,
    create_contract,
    get_contract,
    list_contract_parties,
    list_contracts,
    list_contracts_for_organization,
    update_contract,
)
from src.services.dora_organization_service import OrganizationInput, create_organization

_ACTOR_A = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_ACTOR_B = uuid.UUID("d0000000-0000-4000-d000-000000000005")
_ROLE = "compliance_lead"
_TABLES = ("dora_contracts", "dora_contract_parties")


def _organization(conn, tenant_id, name: str, actor) -> str:
    """Create one organisation for the tenant, committed, and return its id."""
    with conn.transaction():
        return create_organization(
            conn, tenant_id, OrganizationInput(legal_name=name), actor_id=actor, actor_role=_ROLE
        ).organization_id


def _contract(conn, tenant_id, reference: str, actor) -> str:
    """Create one contract for the tenant, committed, and return its id."""
    with conn.transaction():
        return create_contract(
            conn, tenant_id, ContractInput(contract_reference=reference), actor_id=actor, actor_role=_ROLE
        ).contract_id


def _signed(conn, tenant_id, reference: str, org_name: str, actor) -> tuple[str, str]:
    """Create a contract and an organisation that signs it as recipient; return (contract, organisation)."""
    contract_id = _contract(conn, tenant_id, reference, actor)
    organization_id = _organization(conn, tenant_id, org_name, actor)
    with conn.transaction():
        add_contract_party(
            conn, tenant_id, contract_id, organization_id, "recipient_signatory",
            actor_id=actor, actor_role=_ROLE,
        )
    return contract_id, organization_id


def _count_as(conn, tenant_id, table: str, other_tenant_id) -> int:
    """Count the OTHER tenant's rows in a table while THIS tenant's context is set."""
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return conn.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s", [str(other_tenant_id)]
        ).fetchone()[0]


# ── A. Contract isolation, both directions ──────────────────────────────────


@pytest.mark.integration
def test_contracts_are_invisible_across_tenants_in_both_directions(db_connection, tenant_a_id, tenant_b_id):
    contract_a = _contract(db_connection, tenant_a_id, "A-MSA-1", _ACTOR_A)
    contract_b = _contract(db_connection, tenant_b_id, "B-MSA-1", _ACTOR_B)

    with db_connection.transaction():
        seen_by_a = {c.contract_reference for c in list_contracts(db_connection, tenant_a_id)}
        seen_by_b = {c.contract_reference for c in list_contracts(db_connection, tenant_b_id)}
        a_reads_b = get_contract(db_connection, tenant_a_id, contract_b)
        b_reads_a = get_contract(db_connection, tenant_b_id, contract_a)

    assert seen_by_a == {"A-MSA-1"} and seen_by_b == {"B-MSA-1"}
    assert a_reads_b is None and b_reads_a is None
    assert _count_as(db_connection, tenant_a_id, "dora_contracts", tenant_b_id) == 0
    assert _count_as(db_connection, tenant_b_id, "dora_contracts", tenant_a_id) == 0


@pytest.mark.integration
def test_another_tenants_contract_cannot_be_amended_and_nothing_is_ledgered(
    db_connection, tenant_a_id, tenant_b_id
):
    contract_b = _contract(db_connection, tenant_b_id, "B-MSA-1", _ACTOR_B)
    with db_connection.transaction():
        result = update_contract(
            db_connection, tenant_a_id, contract_b,
            ContractUpdate(display_name="Hijacked", contract_start_date=None,
                           contract_end_date=None, is_active=False),
            actor_id=_ACTOR_A, actor_role=_ROLE,
        )
    assert result is None
    with db_connection.transaction():
        untouched = get_contract(db_connection, tenant_b_id, contract_b)
        a_entries = get_entries_by_actor(db_connection, tenant_a_id, _ACTOR_A)
    assert untouched.display_name is None and untouched.is_active is True
    assert [e for e in a_entries if e.object_id == contract_b] == []


# ── B. Party isolation, both directions ─────────────────────────────────────


@pytest.mark.integration
def test_parties_are_invisible_across_tenants_in_both_directions(db_connection, tenant_a_id, tenant_b_id):
    contract_a, org_a = _signed(db_connection, tenant_a_id, "A-MSA-1", "Alpha Bank AG", _ACTOR_A)
    contract_b, org_b = _signed(db_connection, tenant_b_id, "B-MSA-1", "Beta Bank SA", _ACTOR_B)

    with db_connection.transaction():
        a_sees_b_parties = list_contract_parties(db_connection, tenant_a_id, contract_b)
        b_sees_a_parties = list_contract_parties(db_connection, tenant_b_id, contract_a)
        a_sees_b_org_contracts = list_contracts_for_organization(db_connection, tenant_a_id, org_b)
        b_sees_a_org_contracts = list_contracts_for_organization(db_connection, tenant_b_id, org_a)
        a_own = list_contract_parties(db_connection, tenant_a_id, contract_a)

    assert a_sees_b_parties == [] and b_sees_a_parties == []
    assert a_sees_b_org_contracts == [] and b_sees_a_org_contracts == []
    assert [p.organization_id for p in a_own] == [org_a]
    assert _count_as(db_connection, tenant_a_id, "dora_contract_parties", tenant_b_id) == 0
    assert _count_as(db_connection, tenant_b_id, "dora_contract_parties", tenant_a_id) == 0


@pytest.mark.integration
def test_the_service_treats_another_tenants_contract_or_organisation_as_not_found(
    db_connection, tenant_a_id, tenant_b_id
):
    contract_a = _contract(db_connection, tenant_a_id, "A-MSA-1", _ACTOR_A)
    org_a = _organization(db_connection, tenant_a_id, "Alpha Bank AG", _ACTOR_A)
    contract_b, org_b = _signed(db_connection, tenant_b_id, "B-MSA-1", "Beta Bank SA", _ACTOR_B)

    for contract_id, organization_id in ((contract_b, org_a), (contract_a, org_b)):
        with pytest.raises(EntryNotFoundError):
            with db_connection.transaction():
                add_contract_party(
                    db_connection, tenant_a_id, contract_id, organization_id, "recipient_signatory",
                    actor_id=_ACTOR_A, actor_role=_ROLE,
                )
    # A missing id and another tenant's id produce the same outcome.
    with pytest.raises(EntryNotFoundError):
        with db_connection.transaction():
            add_contract_party(
                db_connection, tenant_a_id, str(uuid.uuid4()), org_a, "recipient_signatory",
                actor_id=_ACTOR_A, actor_role=_ROLE,
            )
    with db_connection.transaction():
        assert list_contract_parties(db_connection, tenant_a_id, contract_a) == []


# ── C. RLS state and behaviour, proven under the owning role ────────────────


@pytest.mark.integration
@pytest.mark.parametrize("table", _TABLES)
def test_rls_is_enabled_and_forced_with_the_standard_policy(db_connection, table):
    with db_connection.transaction():
        row = db_connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", [table]
        ).fetchone()
        policies = db_connection.execute(
            "SELECT polname, polcmd, pg_get_expr(polqual, polrelid), polwithcheck IS NULL "
            "FROM pg_policy WHERE polrelid = %s::regclass",
            [table],
        ).fetchall()
    assert row is not None, f"{table} does not exist — is migration 026 applied?"
    assert row[0] is True, f"{table}: ROW LEVEL SECURITY is not enabled"
    assert row[1] is True, f"{table}: ROW LEVEL SECURITY is not FORCED"
    assert policies == [(
        "tenant_isolation_policy", "*",
        "(tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)", True,
    )]


@pytest.mark.integration
@pytest.mark.parametrize("table", _TABLES)
def test_the_owner_role_is_bound_by_the_policy(db_connection, tenant_a_id, tenant_b_id, table):
    _signed(db_connection, tenant_b_id, "B-MSA-1", "Beta Bank SA", _ACTOR_B)
    with db_connection.transaction():
        owner = db_connection.execute(
            "SELECT pg_get_userbyid(relowner) = current_user FROM pg_class WHERE relname = %s", [table]
        ).fetchone()[0]
    assert owner is True, "this test only proves FORCE if the connecting role owns the table"
    assert _count_as(db_connection, tenant_a_id, table, tenant_b_id) == 0


@pytest.mark.integration
@pytest.mark.parametrize("table", _TABLES)
def test_no_tenant_context_yields_no_rows_rather_than_all_rows(db_connection, tenant_a_id, table):
    # A genuinely fresh session: current_setting(..., true) is NULL, the
    # comparison is NULL, the row is invisible. A reused pooled connection
    # comes back with '' instead and errors on the cast (the §17 pool-RESET
    # hazard) — also fail-closed, but not deterministic, so not used here.
    _signed(db_connection, tenant_a_id, "A-MSA-1", "Alpha Bank AG", _ACTOR_A)
    fresh = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cursor = fresh.cursor()
        cursor.execute(f"SELECT count(*) FROM {table} WHERE tenant_id = %s", [str(tenant_a_id)])
        count = cursor.fetchone()[0]
    finally:
        fresh.rollback()
        fresh.close()
    assert count == 0


@pytest.mark.integration
@pytest.mark.parametrize("table", _TABLES)
def test_the_policy_refuses_a_write_carrying_another_tenants_id(db_connection, tenant_a_id, tenant_b_id, table):
    contract_b, org_b = _signed(db_connection, tenant_b_id, "B-MSA-1", "Beta Bank SA", _ACTOR_B)
    statements = {
        "dora_contracts": (
            "INSERT INTO dora_contracts (tenant_id, contract_reference) VALUES (%s, 'SMUGGLED')",
            [str(tenant_b_id)],
        ),
        "dora_contract_parties": (
            "INSERT INTO dora_contract_parties (tenant_id, contract_id, organization_id, party_role) "
            "VALUES (%s, %s, %s, 'provider_signatory')",
            [str(tenant_b_id), contract_b, org_b],
        ),
    }
    sql, params = statements[table]
    with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="violates row-level security policy"):
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(sql, params)
    assert _count_as(db_connection, tenant_b_id, table, tenant_b_id) == 1


# ── D. Cross-tenant references — the composite FKs, service bypassed ────────


def _raw_party_insert(conn, tenant_id, contract_id: str, organization_id: str) -> None:
    """Insert one party row with raw SQL under the given tenant's context."""
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
    conn.execute(
        "INSERT INTO dora_contract_parties (tenant_id, contract_id, organization_id, party_role) "
        "VALUES (%s, %s, %s, 'recipient_signatory')",
        [str(tenant_id), contract_id, organization_id],
    )


@pytest.mark.integration
def test_a_tenant_a_party_cannot_reference_a_tenant_b_contract(db_connection, tenant_a_id, tenant_b_id):
    org_a = _organization(db_connection, tenant_a_id, "Alpha Bank AG", _ACTOR_A)
    contract_b = _contract(db_connection, tenant_b_id, "B-MSA-1", _ACTOR_B)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation, match="fk_dora_contract_parties_contract"):
        with db_connection.transaction():
            _raw_party_insert(db_connection, tenant_a_id, contract_b, org_a)


@pytest.mark.integration
def test_a_tenant_a_party_cannot_reference_a_tenant_b_organisation(db_connection, tenant_a_id, tenant_b_id):
    contract_a = _contract(db_connection, tenant_a_id, "A-MSA-1", _ACTOR_A)
    org_b = _organization(db_connection, tenant_b_id, "Beta Bank SA", _ACTOR_B)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation, match="fk_dora_contract_parties_organization"):
        with db_connection.transaction():
            _raw_party_insert(db_connection, tenant_a_id, contract_a, org_b)


@pytest.mark.integration
@pytest.mark.parametrize("foreign", ["contract", "organization"])
def test_the_composite_fks_reject_cross_tenant_references_with_rls_disabled(
    db_connection, tenant_a_id, tenant_b_id, foreign
):
    # Isolates each foreign key as the mechanism: with RLS off on the party
    # table the policy cannot be what refuses the row. The rollback is
    # unconditional, so a regression that let the INSERT through can never
    # commit — or leave RLS disabled on a live table.
    contract_a = _contract(db_connection, tenant_a_id, "A-MSA-1", _ACTOR_A)
    org_a = _organization(db_connection, tenant_a_id, "Alpha Bank AG", _ACTOR_A)
    contract_b, org_b = _signed(db_connection, tenant_b_id, "B-MSA-1", "Beta Bank SA", _ACTOR_B)
    targets = {"contract": (contract_b, org_a), "organization": (contract_a, org_b)}
    contract_id, organization_id = targets[foreign]
    raised: Exception | None = None
    try:
        db_connection.execute("ALTER TABLE dora_contract_parties DISABLE ROW LEVEL SECURITY")
        _raw_party_insert(db_connection, tenant_a_id, contract_id, organization_id)
    except Exception as exc:  # noqa: BLE001 — the assertions below name the expected one
        raised = exc
    finally:
        db_connection.rollback()

    assert raised is not None, f"the cross-tenant {foreign} reference was ACCEPTED"
    assert isinstance(raised, psycopg2.errors.ForeignKeyViolation)
    assert f"fk_dora_contract_parties_{foreign}" in str(raised)
    with db_connection.transaction():
        enabled, forced = db_connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'dora_contract_parties'"
        ).fetchone()
    assert enabled is True, "RLS was left DISABLED — the DDL did not roll back"
    assert forced is True
