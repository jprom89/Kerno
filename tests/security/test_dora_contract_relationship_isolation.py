"""DORA-V2-002B — tenant isolation and relational integrity of dora_contract_relationships.

What:  proves, against a live PostgreSQL database and under the role that owns
       the table, that Tenant A cannot see Tenant B's contract links in either
       direction through any read; that the write paths treat another tenant's
       contract or link exactly like a missing one and never name anything of
       the other tenant's in an error; that ENABLE + FORCE ROW LEVEL SECURITY
       is in effect rather than merely written into migration 027; and that a
       Tenant A link cannot reference a Tenant B contract at either end even
       with the service bypassed and RLS switched off.
Why:   DORA_MODEL_V2.md §32 and CLAUDE.md §3. Only a real database can show
       that the composite foreign keys and the policy hold on their own.
How:   pytest tests/security/test_dora_contract_relationship_isolation.py -m integration -v
"""

from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

from src.exceptions import DORAContractConflictError, EntryNotFoundError
from src.services.audit_log import get_entries_by_actor
from src.services.dora_contract_hierarchy_service import (
    OBJECT_TYPE_CONTRACT_RELATIONSHIP,
    add_contract_relationship,
    deactivate_contract_relationship,
    get_contract_relationship,
    get_recorded_parent,
    list_contract_relationship_history,
    list_recorded_children,
)
from src.services.dora_contract_service import ContractInput, create_contract

_ACTOR_A = uuid.UUID("d0000000-0000-4000-d000-000000000008")
_ACTOR_B = uuid.UUID("d0000000-0000-4000-d000-000000000009")
_ROLE = "compliance_lead"
_TABLE = "dora_contract_relationships"


def _contracts(conn, tenant_id, actor, *references: str) -> list[str]:
    """Create and commit one contract per reference for the tenant; return their ids."""
    with conn.transaction():
        return [
            create_contract(conn, tenant_id, ContractInput(contract_reference=reference), actor_id=actor, actor_role=_ROLE).contract_id
            for reference in references
        ]


def _hierarchy(conn, tenant_id, actor, prefix: str) -> tuple[str, str, str]:
    """Create an order under an MSA for the tenant; return (order, msa, link id)."""
    order, msa = _contracts(conn, tenant_id, actor, f"{prefix}-ORDER", f"{prefix}-MSA")
    with conn.transaction():
        link = add_contract_relationship(conn, tenant_id, order, msa, actor_id=actor, actor_role=_ROLE)
    return order, msa, link.contract_relationship_id


def _count_as(conn, tenant_id, other_tenant_id) -> int:
    """Count the OTHER tenant's links while THIS tenant's context is set."""
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        return conn.execute(f"SELECT count(*) FROM {_TABLE} WHERE tenant_id = %s", [str(other_tenant_id)]).fetchone()[0]


def _raw_link(conn, tenant_id, child: str, parent: str) -> None:
    """Insert one link with raw SQL under the given tenant's context."""
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
    conn.execute(
        f"INSERT INTO {_TABLE} (tenant_id, child_contract_id, parent_contract_id, relationship_type) "
        "VALUES (%s, %s, %s, 'overarching')",
        [str(tenant_id), child, parent],
    )


# ── A. Reads are isolated in both directions ────────────────────────────────


@pytest.mark.integration
def test_links_are_invisible_across_tenants_through_every_read(db_connection, tenant_a_id, tenant_b_id):
    order_a, msa_a, link_a = _hierarchy(db_connection, tenant_a_id, _ACTOR_A, "A")
    order_b, msa_b, link_b = _hierarchy(db_connection, tenant_b_id, _ACTOR_B, "B")
    for reader, (order, msa, link) in ((tenant_a_id, (order_b, msa_b, link_b)), (tenant_b_id, (order_a, msa_a, link_a))):
        with db_connection.transaction():
            assert get_contract_relationship(db_connection, reader, link) is None
            assert get_recorded_parent(db_connection, reader, order) is None
            assert list_recorded_children(db_connection, reader, msa) == []
            assert list_contract_relationship_history(db_connection, reader, order) == []
    with db_connection.transaction():
        assert get_recorded_parent(db_connection, tenant_a_id, order_a).parent_contract_id == msa_a
    assert _count_as(db_connection, tenant_a_id, tenant_b_id) == 0
    assert _count_as(db_connection, tenant_b_id, tenant_a_id) == 0


# ── B. Writes treat another tenant's records as missing, and say nothing about them ─


@pytest.mark.integration
def test_another_tenants_contract_is_not_found_at_either_end_like_a_missing_one(db_connection, tenant_a_id, tenant_b_id):
    [contract_a] = _contracts(db_connection, tenant_a_id, _ACTOR_A, "A-ORDER")
    order_b, msa_b, link_b = _hierarchy(db_connection, tenant_b_id, _ACTOR_B, "B")
    missing = str(uuid.uuid4())
    outcomes = {}
    for label, child, parent in (
        ("other-parent", contract_a, msa_b), ("other-child", order_b, contract_a),
        ("missing-parent", contract_a, missing), ("missing-child", missing, contract_a),
    ):
        with pytest.raises(EntryNotFoundError) as caught:
            with db_connection.transaction():
                add_contract_relationship(db_connection, tenant_a_id, child, parent, actor_id=_ACTOR_A, actor_role=_ROLE)
        outcomes[label] = str(caught.value)
    assert outcomes["other-parent"] == outcomes["missing-parent"].replace(missing, msa_b)
    assert outcomes["other-child"] == outcomes["missing-child"].replace(missing, order_b)
    for message in outcomes.values():
        assert link_b not in message and "B-" not in message
    assert outcomes["other-parent"].count(msa_b) == 1 and order_b not in outcomes["other-parent"]
    with db_connection.transaction():
        a_entries = get_entries_by_actor(db_connection, tenant_a_id, _ACTOR_A)
    assert [e for e in a_entries if e.object_type == OBJECT_TYPE_CONTRACT_RELATIONSHIP] == []


@pytest.mark.integration
def test_another_tenants_link_cannot_be_deactivated_and_is_untouched(db_connection, tenant_a_id, tenant_b_id):
    order_b, msa_b, link_b = _hierarchy(db_connection, tenant_b_id, _ACTOR_B, "B")
    with db_connection.transaction():
        assert deactivate_contract_relationship(
            db_connection, tenant_a_id, link_b, actor_id=_ACTOR_A, actor_role=_ROLE
        ) is None
    with db_connection.transaction():
        assert get_contract_relationship(db_connection, tenant_b_id, link_b).is_active is True
        a_entries = get_entries_by_actor(db_connection, tenant_a_id, _ACTOR_A)
    assert [e for e in a_entries if e.object_id == link_b] == []


@pytest.mark.integration
def test_a_tenant_a_conflict_names_only_tenant_a_contracts(db_connection, tenant_a_id, tenant_b_id):
    order_a, msa_a, _ = _hierarchy(db_connection, tenant_a_id, _ACTOR_A, "A")
    order_b, msa_b, link_b = _hierarchy(db_connection, tenant_b_id, _ACTOR_B, "B")
    [other_a] = _contracts(db_connection, tenant_a_id, _ACTOR_A, "A-OTHER")
    with pytest.raises(DORAContractConflictError) as caught:
        with db_connection.transaction():
            add_contract_relationship(db_connection, tenant_a_id, order_a, other_a, actor_id=_ACTOR_A, actor_role=_ROLE)
    message = str(caught.value)
    assert msa_a in message
    assert not any(identifier in message for identifier in (order_b, msa_b, link_b))


# ── C. RLS state and behaviour, proven under the owning role ────────────────


@pytest.mark.integration
def test_rls_is_enabled_and_forced_and_binds_the_owning_role(db_connection, tenant_a_id, tenant_b_id):
    _hierarchy(db_connection, tenant_b_id, _ACTOR_B, "B")
    with db_connection.transaction():
        enabled, forced, owned = db_connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity, pg_get_userbyid(relowner) = current_user "
            "FROM pg_class WHERE relname = %s",
            [_TABLE],
        ).fetchone()
    assert (enabled, forced) == (True, True)
    assert owned is True, "this test only proves FORCE if the connecting role owns the table"
    assert _count_as(db_connection, tenant_a_id, tenant_b_id) == 0


@pytest.mark.integration
def test_no_tenant_context_yields_no_rows_rather_than_all_rows(db_connection, tenant_a_id):
    _hierarchy(db_connection, tenant_a_id, _ACTOR_A, "A")
    fresh = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        cursor = fresh.cursor()
        cursor.execute(f"SELECT count(*) FROM {_TABLE} WHERE tenant_id = %s", [str(tenant_a_id)])
        count = cursor.fetchone()[0]
    finally:
        fresh.rollback()
        fresh.close()
    assert count == 0


@pytest.mark.integration
def test_the_policy_refuses_a_write_carrying_another_tenants_id(db_connection, tenant_a_id, tenant_b_id):
    order_b, msa_b = _contracts(db_connection, tenant_b_id, _ACTOR_B, "B-ORDER", "B-MSA")
    with pytest.raises(psycopg2.errors.InsufficientPrivilege, match="violates row-level security policy"):
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(
                f"INSERT INTO {_TABLE} (tenant_id, child_contract_id, parent_contract_id, relationship_type) "
                "VALUES (%s, %s, %s, 'overarching')",
                [str(tenant_b_id), order_b, msa_b],
            )
    assert _count_as(db_connection, tenant_b_id, tenant_b_id) == 0


# ── D. Cross-tenant endpoints — the composite FKs, service bypassed ─────────


@pytest.mark.integration
@pytest.mark.parametrize("end", ["child", "parent"])
def test_a_tenant_a_link_cannot_reference_a_tenant_b_contract_at_either_end(db_connection, tenant_a_id, tenant_b_id, end):
    [contract_a] = _contracts(db_connection, tenant_a_id, _ACTOR_A, "A-ORDER")
    [contract_b] = _contracts(db_connection, tenant_b_id, _ACTOR_B, "B-MSA")
    child, parent = (contract_b, contract_a) if end == "child" else (contract_a, contract_b)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation, match=f"fk_dora_contract_relationships_{end}"):
        with db_connection.transaction():
            _raw_link(db_connection, tenant_a_id, child, parent)


@pytest.mark.integration
@pytest.mark.parametrize("end", ["child", "parent"])
def test_the_composite_fks_reject_cross_tenant_endpoints_with_rls_disabled(db_connection, tenant_a_id, tenant_b_id, end):
    # With RLS off on the link table the policy cannot be what refuses the
    # row. The rollback is unconditional, so a regression that let the INSERT
    # through can never commit — or leave RLS disabled on a live table.
    [contract_a] = _contracts(db_connection, tenant_a_id, _ACTOR_A, "A-ORDER")
    [contract_b] = _contracts(db_connection, tenant_b_id, _ACTOR_B, "B-MSA")
    child, parent = (contract_b, contract_a) if end == "child" else (contract_a, contract_b)
    raised: Exception | None = None
    try:
        db_connection.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")
        _raw_link(db_connection, tenant_a_id, child, parent)
    except Exception as exc:  # noqa: BLE001 — the assertions below name the expected one
        raised = exc
    finally:
        db_connection.rollback()

    assert raised is not None, f"the cross-tenant {end} reference was ACCEPTED"
    assert isinstance(raised, psycopg2.errors.ForeignKeyViolation)
    assert f"fk_dora_contract_relationships_{end}" in str(raised)
    with db_connection.transaction():
        enabled, forced = db_connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", [_TABLE]
        ).fetchone()
    assert (enabled, forced) == (True, True), "RLS was left DISABLED — the DDL did not roll back"
