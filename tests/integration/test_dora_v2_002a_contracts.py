"""DORA-V2-002A — contract identity and signing parties against a live database.

What:  proves the contract service round-trips through real PostgreSQL; that
       every write lands its KER-107 ledger entry in the same transaction and
       a rolled-back write leaves neither row nor entry; that a duplicate
       reference or party tuple is a controlled conflict decided by the
       database constraint; and that the constraints themselves — reference
       canonical form under the one whitespace policy, date order, the party
       role vocabulary — hold for raw SQL, so a service that later dropped its
       own checks would still be caught.
Why:   CLAUDE.md §11 live-database rule. A spy proves the SQL that was
       written; only PostgreSQL proves the SQL the driver can run and the
       constraints the database holds. Isolation and the composite FKs are in
       tests/security/test_dora_contract_isolation.py; concurrency in
       tests/integration/test_dora_v2_002a_concurrency.py.
How:   pytest tests/integration/test_dora_v2_002a_contracts.py -m integration -v
"""

from __future__ import annotations

import uuid
from datetime import date

import psycopg2
import pytest

from config.constants import CONTRACT_REFERENCE_MAX_CHARACTERS
from src.exceptions import DORAContractConflictError
from src.models.dora_contract import CONTRACT_TEXT_TRIM_CHARACTERS
from src.services import dora_contract_service
from src.services.audit_log import get_entries_by_actor, verify_audit_chain
from src.services.dora_contract_service import (
    ACTION_CONTRACT_CREATED,
    ACTION_CONTRACT_PARTY_ADDED,
    ACTION_CONTRACT_UPDATED,
    OBJECT_TYPE_CONTRACT,
    OBJECT_TYPE_CONTRACT_PARTY,
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
from src.services.dora_organization_service import (
    OrganizationInput,
    add_organization_role,
    create_organization,
    list_organization_roles,
)

_ACTOR = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_ROLE = "compliance_lead"


class _DeliberateRollback(Exception):
    pass


def _ledger(conn, tenant_id) -> list:
    """Return this actor's ledger entries in this tenant, oldest first."""
    with conn.transaction():
        return get_entries_by_actor(conn, tenant_id, _ACTOR)


def _entries(conn, tenant_id, action: str, object_id: str | None = None) -> list:
    """Return this actor's entries for one action, optionally for one object."""
    return [
        e for e in _ledger(conn, tenant_id)
        if e.action_type == action and (object_id is None or e.object_id == object_id)
    ]


def _contract(conn, tenant_id, reference: str, **fields):
    """Create and commit one contract through the service."""
    with conn.transaction():
        return create_contract(
            conn, tenant_id, ContractInput(contract_reference=reference, **fields),
            actor_id=_ACTOR, actor_role=_ROLE,
        )


def _organization(conn, tenant_id, name: str, *roles: str) -> str:
    """Create and commit one organisation holding exactly the given organisation roles."""
    with conn.transaction():
        organization_id = create_organization(
            conn, tenant_id, OrganizationInput(legal_name=name), actor_id=_ACTOR, actor_role=_ROLE
        ).organization_id
        for role in roles:
            add_organization_role(conn, tenant_id, organization_id, role, actor_id=_ACTOR, actor_role=_ROLE)
    return organization_id


def _party(conn, tenant_id, contract_id: str, organization_id: str, party_role: str):
    """Add and commit one signing party through the service."""
    with conn.transaction():
        return add_contract_party(
            conn, tenant_id, contract_id, organization_id, party_role, actor_id=_ACTOR, actor_role=_ROLE
        )


def _raw_contract(conn, tenant_id, reference: str, display_name=None, start=None, end=None) -> None:
    """Insert a contract with raw SQL, bypassing every service check."""
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
    conn.execute(
        "INSERT INTO dora_contracts (tenant_id, contract_reference, display_name, "
        "contract_start_date, contract_end_date) VALUES (%s, %s, %s, %s, %s)",
        [str(tenant_id), reference, display_name, start, end],
    )


def _stored_references(conn, tenant_id) -> list[str]:
    """Return every contract reference this tenant holds, as stored."""
    with conn.transaction():
        return [c.contract_reference for c in list_contracts(conn, tenant_id)]


# ── A. Contract identity ────────────────────────────────────────────────────


@pytest.mark.integration
def test_create_persists_canonical_values_and_ledgers_in_one_transaction(db_connection, tenant_a_id):
    created = _contract(
        db_connection, tenant_a_id, "\u00a0\tMSA-2024/01 v2\n",
        display_name=" Cloud hosting ", contract_start_date=date(2024, 1, 1),
    )
    with db_connection.transaction():
        fetched = get_contract(db_connection, tenant_a_id, created.contract_id)
    assert fetched == created
    assert fetched.contract_reference == "MSA-2024/01 v2"
    assert fetched.display_name == "Cloud hosting"
    assert (fetched.contract_start_date, fetched.contract_end_date) == (date(2024, 1, 1), None)

    entries = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_CREATED, created.contract_id)
    assert len(entries) == 1
    assert entries[0].object_type == OBJECT_TYPE_CONTRACT
    assert entries[0].actor_id == str(_ACTOR)
    assert entries[0].before_state is None
    assert entries[0].after_state["contract_reference"] == "MSA-2024/01 v2"
    assert entries[0].after_state["contract_start_date"] == "2024-01-01"


@pytest.mark.integration
def test_a_duplicate_reference_in_one_tenant_is_a_controlled_conflict(db_connection, tenant_a_id):
    first = _contract(db_connection, tenant_a_id, "MSA-1")
    with db_connection.transaction():
        with pytest.raises(DORAContractConflictError):
            create_contract(
                db_connection, tenant_a_id, ContractInput(contract_reference="  MSA-1\t", display_name="Retry"),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
        # The conflict aborted nothing: the same transaction goes on to write.
        create_contract(
            db_connection, tenant_a_id, ContractInput(contract_reference="MSA-2"),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert _stored_references(db_connection, tenant_a_id) == ["MSA-1", "MSA-2"]
    creations = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_CREATED)
    assert [e.after_state["contract_reference"] for e in creations] == ["MSA-1", "MSA-2"]
    with db_connection.transaction():
        assert get_contract(db_connection, tenant_a_id, first.contract_id).display_name is None


@pytest.mark.integration
def test_the_same_reference_in_two_tenants_is_two_contracts(db_connection, tenant_a_id, tenant_b_id):
    in_a = _contract(db_connection, tenant_a_id, "MSA-SHARED")
    in_b = _contract(db_connection, tenant_b_id, "MSA-SHARED")
    assert in_a.contract_id != in_b.contract_id
    assert _stored_references(db_connection, tenant_a_id) == ["MSA-SHARED"]
    assert _stored_references(db_connection, tenant_b_id) == ["MSA-SHARED"]


@pytest.mark.integration
def test_case_is_identity_and_a_shared_display_name_merges_nothing(db_connection, tenant_a_id):
    upper = _contract(db_connection, tenant_a_id, "MSA-1", display_name="Hosting")
    lower = _contract(db_connection, tenant_a_id, "msa-1", display_name="Hosting")
    assert upper.contract_id != lower.contract_id
    assert sorted(_stored_references(db_connection, tenant_a_id)) == ["MSA-1", "msa-1"]


@pytest.mark.integration
@pytest.mark.parametrize("missing", ["", "   ", "\t\n", "\u2028"])
def test_a_missing_reference_is_refused_and_nothing_is_written(db_connection, tenant_a_id, missing):
    with pytest.raises(ValueError, match="contract_reference is required"):
        with db_connection.transaction():
            create_contract(
                db_connection, tenant_a_id, ContractInput(contract_reference=missing),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
    assert _stored_references(db_connection, tenant_a_id) == []
    assert _entries(db_connection, tenant_a_id, ACTION_CONTRACT_CREATED) == []


@pytest.mark.integration
@pytest.mark.parametrize("space", list(CONTRACT_TEXT_TRIM_CHARACTERS), ids=lambda c: f"U+{ord(c):04X}")
@pytest.mark.parametrize("where", ["leading", "trailing"])
def test_the_database_refuses_a_reference_padded_with_any_policy_character(
    db_connection, tenant_a_id, space, where
):
    # The same 29 characters the service strips are the ones the CHECK
    # refuses at either end — one policy, not two that happen to overlap.
    padded = f"{space}MSA-1" if where == "leading" else f"MSA-1{space}"
    with pytest.raises(psycopg2.errors.CheckViolation, match="ck_dora_contracts_reference_canonical"):
        with db_connection.transaction():
            _raw_contract(db_connection, tenant_a_id, padded)


@pytest.mark.integration
@pytest.mark.parametrize("space", list(CONTRACT_TEXT_TRIM_CHARACTERS), ids=lambda c: f"U+{ord(c):04X}")
def test_the_service_stores_what_the_database_accepts_for_every_policy_character(
    db_connection, tenant_a_id, space
):
    created = _contract(db_connection, tenant_a_id, f"{space}{space}MSA{space}1{space}")
    assert created.contract_reference == f"MSA{space}1"
    assert _stored_references(db_connection, tenant_a_id) == [f"MSA{space}1"]


@pytest.mark.integration
@pytest.mark.parametrize("reference", ["MSA 1", "MSA\t1", "-MSA-1.", "(MSA/1)", "\u200bMSA"])
def test_interior_whitespace_and_boundary_punctuation_are_accepted_by_the_database(
    db_connection, tenant_a_id, reference
):
    # U+200B ZERO WIDTH SPACE is not whitespace to Python and not in the
    # policy, so both sides leave it alone — agreement, not an accident.
    with db_connection.transaction():
        _raw_contract(db_connection, tenant_a_id, reference)
    assert _stored_references(db_connection, tenant_a_id) == [reference]


@pytest.mark.integration
def test_the_longest_reference_is_stored_even_in_four_byte_characters(db_connection, tenant_a_id):
    # 255 characters of four-byte UTF-8 (1020 bytes) must fit the unique
    # index; the bound exists so no accepted reference can overflow it.
    longest = chr(0x1F4C4) * CONTRACT_REFERENCE_MAX_CHARACTERS
    created = _contract(db_connection, tenant_a_id, longest)
    assert _stored_references(db_connection, tenant_a_id) == [created.contract_reference]
    assert len(created.contract_reference) == CONTRACT_REFERENCE_MAX_CHARACTERS


@pytest.mark.integration
def test_an_over_long_reference_is_refused_and_leaves_the_transaction_usable(db_connection, tenant_a_id):
    # Before the bound, 4000 characters reached the INSERT and failed as
    # ProgramLimitExceeded on the unique index, aborting the transaction.
    with db_connection.transaction():
        with pytest.raises(ValueError, match="at most"):
            create_contract(
                db_connection, tenant_a_id, ContractInput(contract_reference="R" * 4000),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
        create_contract(
            db_connection, tenant_a_id, ContractInput(contract_reference="MSA-AFTER"),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert _stored_references(db_connection, tenant_a_id) == ["MSA-AFTER"]
    with pytest.raises(psycopg2.errors.CheckViolation, match="ck_dora_contracts_reference_length"):
        with db_connection.transaction():
            _raw_contract(db_connection, tenant_a_id, "R" * (CONTRACT_REFERENCE_MAX_CHARACTERS + 1))


@pytest.mark.integration
@pytest.mark.parametrize("actor_id, actor_role", [(None, _ROLE), ("not-a-uuid", _ROLE), (_ACTOR, "")])
def test_a_bad_actor_writes_nothing_and_the_transaction_commits_nothing(
    db_connection, tenant_a_id, actor_id, actor_role
):
    # The actor is checked before the INSERT, so even a caller that catches
    # the error and commits cannot persist an unledgered contract.
    with db_connection.transaction():
        with pytest.raises(ValueError, match="actor_"):
            create_contract(
                db_connection, tenant_a_id, ContractInput(contract_reference="MSA-UNLEDGERED"),
                actor_id=actor_id, actor_role=actor_role,
            )
    assert _stored_references(db_connection, tenant_a_id) == []


@pytest.mark.integration
def test_the_database_refuses_an_empty_reference(db_connection, tenant_a_id):
    with pytest.raises(psycopg2.errors.CheckViolation, match="ck_dora_contracts_reference_canonical"):
        with db_connection.transaction():
            _raw_contract(db_connection, tenant_a_id, "")


@pytest.mark.integration
@pytest.mark.parametrize("display_name", ["", " Hosting", "Hosting\n"])
def test_the_database_refuses_a_blank_or_padded_display_name(db_connection, tenant_a_id, display_name):
    with pytest.raises(psycopg2.errors.CheckViolation, match="ck_dora_contracts_display_name_canonical"):
        with db_connection.transaction():
            _raw_contract(db_connection, tenant_a_id, "MSA-1", display_name=display_name)


@pytest.mark.integration
def test_date_order_is_enforced_by_service_and_database_and_nulls_mean_unknown(db_connection, tenant_a_id):
    with pytest.raises(ValueError, match="before contract_start_date"):
        _contract(db_connection, tenant_a_id, "MSA-BAD",
                  contract_start_date=date(2025, 2, 1), contract_end_date=date(2025, 1, 31))
    with pytest.raises(psycopg2.errors.CheckViolation, match="ck_dora_contracts_date_order"):
        with db_connection.transaction():
            _raw_contract(db_connection, tenant_a_id, "MSA-RAW", start=date(2025, 2, 1), end=date(2025, 1, 31))
    open_ended = _contract(db_connection, tenant_a_id, "MSA-OPEN", contract_start_date=date(2025, 1, 1))
    end_only = _contract(db_connection, tenant_a_id, "MSA-END", contract_end_date=date(2025, 1, 1))
    undated = _contract(db_connection, tenant_a_id, "MSA-UNDATED")
    same_day = _contract(db_connection, tenant_a_id, "MSA-DAY",
                         contract_start_date=date(2025, 1, 1), contract_end_date=date(2025, 1, 1))
    assert open_ended.contract_end_date is None and end_only.contract_start_date is None
    assert undated.contract_start_date is None and undated.contract_end_date is None
    assert same_day.contract_start_date == same_day.contract_end_date
    assert sorted(_stored_references(db_connection, tenant_a_id)) == [
        "MSA-DAY", "MSA-END", "MSA-OPEN", "MSA-UNDATED",
    ]


@pytest.mark.integration
def test_update_changes_only_the_amendable_fields_and_ledgers_before_and_after(db_connection, tenant_a_id):
    created = _contract(db_connection, tenant_a_id, "MSA-1", display_name="Before",
                        contract_start_date=date(2024, 1, 1))
    with db_connection.transaction():
        updated = update_contract(
            db_connection, tenant_a_id, created.contract_id.upper(),
            ContractUpdate(display_name=" After ", contract_start_date=date(2024, 1, 1),
                           contract_end_date=date(2026, 12, 31), is_active=False),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert updated.contract_id == created.contract_id
    assert updated.contract_reference == "MSA-1"
    assert (updated.display_name, updated.contract_end_date, updated.is_active) == (
        "After", date(2026, 12, 31), False,
    )
    assert updated.created_at == created.created_at
    # No trigger maintains updated_at; the writer must, and did.
    assert updated.updated_at > created.updated_at
    with db_connection.transaction():
        assert get_contract(db_connection, tenant_a_id, created.contract_id) == updated

    entries = _entries(db_connection, tenant_a_id, ACTION_CONTRACT_UPDATED, created.contract_id)
    assert len(entries) == 1
    assert entries[0].before_state["display_name"] == "Before"
    assert entries[0].after_state["display_name"] == "After"
    assert entries[0].before_state["contract_reference"] == entries[0].after_state["contract_reference"] == "MSA-1"
    assert entries[0].after_state["contract_end_date"] == "2026-12-31"


@pytest.mark.integration
def test_the_reference_cannot_change_through_update(db_connection, tenant_a_id):
    created = _contract(db_connection, tenant_a_id, "MSA-IMMUTABLE")
    with pytest.raises(TypeError):
        with db_connection.transaction():
            update_contract(
                db_connection, tenant_a_id, created.contract_id,
                ContractUpdate(display_name=None, contract_start_date=None, contract_end_date=None,
                               is_active=True, contract_reference="MSA-RENAMED"),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
    with db_connection.transaction():
        update_contract(
            db_connection, tenant_a_id, created.contract_id,
            ContractUpdate(display_name="MSA-RENAMED", contract_start_date=None, contract_end_date=None,
                           is_active=True),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert _stored_references(db_connection, tenant_a_id) == ["MSA-IMMUTABLE"]


@pytest.mark.integration
def test_update_of_a_contract_this_tenant_lacks_writes_nothing(db_connection, tenant_a_id):
    before = len(_ledger(db_connection, tenant_a_id))
    with db_connection.transaction():
        result = update_contract(
            db_connection, tenant_a_id, str(uuid.uuid4()),
            ContractUpdate(display_name="Ghost", contract_start_date=None, contract_end_date=None, is_active=True),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert result is None
    assert len(_ledger(db_connection, tenant_a_id)) == before


# ── B. Signing parties ──────────────────────────────────────────────────────


@pytest.mark.integration
def test_one_contract_has_several_signatories_and_one_organisation_signs_several_contracts(
    db_connection, tenant_a_id
):
    bank = _organization(db_connection, tenant_a_id, "Alpha Bank AG", "financial_entity")
    cloud = _organization(db_connection, tenant_a_id, "Cloud Provider SARL", "ict_provider")
    group_it = _organization(db_connection, tenant_a_id, "Alpha Group IT GmbH", "ict_provider")
    hosting = _contract(db_connection, tenant_a_id, "MSA-HOSTING")
    support = _contract(db_connection, tenant_a_id, "MSA-SUPPORT")

    _party(db_connection, tenant_a_id, hosting.contract_id, bank, "recipient_signatory")
    _party(db_connection, tenant_a_id, hosting.contract_id, cloud, "provider_signatory")
    _party(db_connection, tenant_a_id, hosting.contract_id, group_it, "intragroup_provider_signatory")
    _party(db_connection, tenant_a_id, support.contract_id, bank, "recipient_signatory")
    _party(db_connection, tenant_a_id, support.contract_id, cloud, "provider_signatory")

    with db_connection.transaction():
        hosting_parties = list_contract_parties(db_connection, tenant_a_id, hosting.contract_id)
        bank_contracts = list_contracts_for_organization(db_connection, tenant_a_id, bank)
        group_contracts = list_contracts_for_organization(db_connection, tenant_a_id, group_it)
    # A list, not a set: MSA-SUPPORT repeats two of these pairs, so a listing
    # that leaked the other contract's parties would collapse to the same set.
    assert sorted((p.party_role, p.organization_id) for p in hosting_parties) == sorted([
        ("recipient_signatory", bank), ("provider_signatory", cloud),
        ("intragroup_provider_signatory", group_it),
    ])
    assert {p.contract_id for p in hosting_parties} == {hosting.contract_id}
    assert [c.contract_reference for c in bank_contracts] == ["MSA-HOSTING", "MSA-SUPPORT"]
    assert [c.contract_reference for c in group_contracts] == ["MSA-HOSTING"]
    assert len(_entries(db_connection, tenant_a_id, ACTION_CONTRACT_PARTY_ADDED)) == 5


@pytest.mark.integration
def test_one_organisation_may_hold_two_explicit_roles_and_is_listed_once(db_connection, tenant_a_id):
    group = _organization(db_connection, tenant_a_id, "Alpha Group AG", "ict_provider")
    contract = _contract(db_connection, tenant_a_id, "MSA-INTRAGROUP")
    _party(db_connection, tenant_a_id, contract.contract_id, group, "recipient_signatory")
    _party(db_connection, tenant_a_id, contract.contract_id, group, "intragroup_provider_signatory")
    with db_connection.transaction():
        parties = list_contract_parties(db_connection, tenant_a_id, contract.contract_id)
        contracts = list_contracts_for_organization(db_connection, tenant_a_id, group)
    assert sorted(p.party_role for p in parties) == ["intragroup_provider_signatory", "recipient_signatory"]
    assert [c.contract_id for c in contracts] == [contract.contract_id]


@pytest.mark.integration
def test_a_duplicate_party_is_a_controlled_conflict(db_connection, tenant_a_id):
    bank = _organization(db_connection, tenant_a_id, "Alpha Bank AG")
    contract = _contract(db_connection, tenant_a_id, "MSA-1")
    first = _party(db_connection, tenant_a_id, contract.contract_id, bank, "recipient_signatory")
    with pytest.raises(DORAContractConflictError):
        _party(db_connection, tenant_a_id, contract.contract_id.upper(), bank.upper(), "recipient_signatory")
    with db_connection.transaction():
        parties = list_contract_parties(db_connection, tenant_a_id, contract.contract_id)
    assert [p.contract_party_id for p in parties] == [first.contract_party_id]
    assert len(_entries(db_connection, tenant_a_id, ACTION_CONTRACT_PARTY_ADDED)) == 1


@pytest.mark.integration
def test_a_recipient_signatory_needs_no_financial_entity_role(db_connection, tenant_a_id):
    # A group company signing on behalf of another entity: no organisation
    # role at all, and none is required or created.
    holding = _organization(db_connection, tenant_a_id, "Alpha Holding SE")
    contract = _contract(db_connection, tenant_a_id, "MSA-GROUP")
    added = _party(db_connection, tenant_a_id, contract.contract_id, holding, "recipient_signatory")
    assert added.party_role == "recipient_signatory"
    with db_connection.transaction():
        assert list_organization_roles(db_connection, tenant_a_id, holding) == []


@pytest.mark.integration
@pytest.mark.parametrize("party_role", ["provider_signatory", "intragroup_provider_signatory"])
def test_a_provider_signatory_needs_the_ict_provider_role_and_it_is_never_assigned(
    db_connection, tenant_a_id, party_role
):
    # Another organisation in the SAME tenant does hold ict_provider, so a
    # check that asked "does anyone here hold it?" would wrongly let the
    # vendor through. The role must be the signing organisation's own.
    _organization(db_connection, tenant_a_id, "Cloud Provider SARL", "ict_provider")
    vendor = _organization(db_connection, tenant_a_id, "Unclassified Vendor Ltd", "financial_entity")
    contract = _contract(db_connection, tenant_a_id, "MSA-VENDOR")
    with pytest.raises(ValueError, match="ict_provider"):
        _party(db_connection, tenant_a_id, contract.contract_id, vendor, party_role)
    with db_connection.transaction():
        roles = [r.role_type for r in list_organization_roles(db_connection, tenant_a_id, vendor)]
        parties = list_contract_parties(db_connection, tenant_a_id, contract.contract_id)
    assert roles == ["financial_entity"]
    assert parties == []
    assert _entries(db_connection, tenant_a_id, ACTION_CONTRACT_PARTY_ADDED) == []


@pytest.mark.integration
def test_adding_a_party_infers_no_consumer_and_writes_nothing_else(db_connection, tenant_a_id):
    bank = _organization(db_connection, tenant_a_id, "Alpha Bank AG", "financial_entity")
    contract = _contract(db_connection, tenant_a_id, "MSA-1")
    with db_connection.transaction():
        consumer_tables = db_connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
            "AND (table_name LIKE '%%usage%%' OR table_name LIKE '%%consum%%')"
        ).fetchall()
        before = _row_counts(db_connection, tenant_a_id)
    _party(db_connection, tenant_a_id, contract.contract_id, bank, "recipient_signatory")
    with db_connection.transaction():
        after = _row_counts(db_connection, tenant_a_id)
    assert consumer_tables == []
    assert {table: after[table] - before[table] for table in before} == {
        "dora_contract_parties": 1, "dora_contracts": 0, "dora_organizations": 0,
        "dora_organization_roles": 0, "dora_organization_identifiers": 0, "audit_log": 1,
    }


def _row_counts(conn, tenant_id) -> dict[str, int]:
    """Count this tenant's rows in each table a party write could plausibly touch."""
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
    counts = {}
    for table in ("dora_contract_parties", "dora_contracts", "dora_organizations",
                  "dora_organization_roles", "dora_organization_identifiers", "audit_log"):
        counts[table] = conn.execute(
            f"SELECT count(*) FROM {table} WHERE tenant_id = %s", [str(tenant_id)]
        ).fetchone()[0]
    return counts


@pytest.mark.integration
def test_the_database_refuses_a_party_role_outside_the_vocabulary(db_connection, tenant_a_id):
    bank = _organization(db_connection, tenant_a_id, "Alpha Bank AG")
    contract = _contract(db_connection, tenant_a_id, "MSA-1")
    with pytest.raises(psycopg2.errors.CheckViolation, match="ck_dora_contract_parties_party_role"):
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(
                "INSERT INTO dora_contract_parties (tenant_id, contract_id, organization_id, party_role) "
                "VALUES (%s, %s, %s, 'consumer')",
                [str(tenant_a_id), contract.contract_id, bank],
            )


@pytest.mark.integration
def test_a_foreign_key_failure_is_never_relabelled_as_a_conflict(db_connection, tenant_a_id, monkeypatch):
    # With the service's own reference checks switched off, a party naming an
    # organisation that does not exist reaches the INSERT. The ON CONFLICT
    # clause names one unique constraint, so the foreign key's refusal must
    # arrive as the driver's ForeignKeyViolation — not as a conflict.
    contract = _contract(db_connection, tenant_a_id, "MSA-1")
    monkeypatch.setattr(dora_contract_service, "_require_party_references", lambda *args: None)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation, match="fk_dora_contract_parties_organization"):
        _party(db_connection, tenant_a_id, contract.contract_id, str(uuid.uuid4()), "recipient_signatory")
    assert _entries(db_connection, tenant_a_id, ACTION_CONTRACT_PARTY_ADDED) == []


# ── Transactions ────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_a_rolled_back_transaction_leaves_no_contract_no_party_and_no_ledger_entry(db_connection, tenant_a_id):
    bank = _organization(db_connection, tenant_a_id, "Alpha Bank AG")
    before = len(_ledger(db_connection, tenant_a_id))
    captured: dict = {}
    with pytest.raises(_DeliberateRollback):
        with db_connection.transaction():
            created = create_contract(
                db_connection, tenant_a_id, ContractInput(contract_reference="MSA-ROLLBACK"),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
            add_contract_party(
                db_connection, tenant_a_id, created.contract_id, bank, "recipient_signatory",
                actor_id=_ACTOR, actor_role=_ROLE,
            )
            captured["contract_id"] = created.contract_id
            raise _DeliberateRollback()
    with db_connection.transaction():
        assert get_contract(db_connection, tenant_a_id, captured["contract_id"]) is None
        assert list_contracts_for_organization(db_connection, tenant_a_id, bank) == []
    assert len(_ledger(db_connection, tenant_a_id)) == before


@pytest.mark.integration
def test_every_contract_write_keeps_the_hash_chain_valid(db_connection, tenant_a_id):
    bank = _organization(db_connection, tenant_a_id, "Alpha Bank AG")
    contract = _contract(db_connection, tenant_a_id, "MSA-1")
    _party(db_connection, tenant_a_id, contract.contract_id, bank, "recipient_signatory")
    with db_connection.transaction():
        update_contract(
            db_connection, tenant_a_id, contract.contract_id,
            ContractUpdate(display_name="Renamed", contract_start_date=None, contract_end_date=None, is_active=True),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    with db_connection.transaction():
        chain = verify_audit_chain(db_connection, tenant_a_id)
    assert chain.is_valid, chain.failure_reason
    object_types = {e.object_type for e in _ledger(db_connection, tenant_a_id)}
    assert {OBJECT_TYPE_CONTRACT, OBJECT_TYPE_CONTRACT_PARTY} <= object_types
