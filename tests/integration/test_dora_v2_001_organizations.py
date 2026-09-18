"""DORA-V2-001 — organisation, identifier and role behaviour against a live database.

What:  proves the service round-trips through real PostgreSQL, that every
       write lands its KER-107 ledger entry in the same transaction, and that
       the database constraints — not only the service's pre-checks — enforce
       canonical form, per-tenant identifier uniqueness, the exact role
       vocabulary, and one-role-once. The constraint tests use raw SQL on
       purpose, so a service that later dropped its own check would still be
       caught here.
Why:   CLAUDE.md §11 live-database rule. A spy proves the SQL that was
       written; only a real connection proves the SQL the driver can run and
       the constraints the database actually holds. Tenant isolation and the
       composite FK are proven separately in
       tests/security/test_dora_organization_isolation.py.
How:   pytest tests/integration/test_dora_v2_001_organizations.py -m integration -v
"""

from __future__ import annotations

import uuid

import pytest

from src.exceptions import EntryNotFoundError
from src.services.audit_log import get_entries_by_actor
from src.services.dora_organization_service import (
    ACTION_ORGANIZATION_CREATED,
    ACTION_ORGANIZATION_IDENTIFIER_ADDED,
    ACTION_ORGANIZATION_ROLE_ADDED,
    ACTION_ORGANIZATION_UPDATED,
    OBJECT_TYPE_ORGANIZATION,
    OBJECT_TYPE_ORGANIZATION_IDENTIFIER,
    OBJECT_TYPE_ORGANIZATION_ROLE,
    OrganizationInput,
    add_organization_identifier,
    add_organization_role,
    create_organization,
    get_organization,
    list_organization_identifiers,
    list_organization_roles,
    list_organizations,
    update_organization,
)

_ACTOR = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_ROLE = "compliance_lead"


class _DeliberateRollback(Exception):
    pass


def _ledger(conn, tenant_id) -> list:
    """Return this actor's ledger entries (these events carry control_id=None)."""
    with conn.transaction():
        return get_entries_by_actor(conn, tenant_id, _ACTOR)


def _raw_insert_org(conn, tenant_id, legal_name: str, country_code=None) -> str:
    """Insert an organisation with raw SQL, bypassing the service's normalisation."""
    org_id = str(uuid.uuid4())
    conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
    conn.execute(
        "INSERT INTO dora_organizations (organization_id, tenant_id, legal_name, country_code) "
        "VALUES (%s, %s, %s, %s)",
        [org_id, str(tenant_id), legal_name, country_code],
    )
    return org_id


# ── Create / read / update through the service ──────────────────────────────


@pytest.mark.integration
def test_create_persists_and_ledgers_in_one_transaction(db_connection, tenant_a_id):
    with db_connection.transaction():
        created = create_organization(
            db_connection, tenant_a_id,
            OrganizationInput(legal_name="  Alpha Bank AG  ", country_code=" de "),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert created.legal_name == "Alpha Bank AG"
    assert created.country_code == "DE"

    with db_connection.transaction():
        fetched = get_organization(db_connection, tenant_a_id, created.organization_id)
    assert fetched is not None
    assert fetched.legal_name == "Alpha Bank AG"
    assert fetched.country_code == "DE"
    assert fetched.is_active is True

    entries = [e for e in _ledger(db_connection, tenant_a_id)
               if e.action_type == ACTION_ORGANIZATION_CREATED]
    assert len(entries) == 1
    assert entries[0].object_type == OBJECT_TYPE_ORGANIZATION
    assert entries[0].object_id == created.organization_id
    assert entries[0].actor_id == str(_ACTOR)
    assert entries[0].control_id is None
    assert entries[0].before_state is None
    assert entries[0].after_state["legal_name"] == "Alpha Bank AG"


@pytest.mark.integration
def test_a_rolled_back_create_leaves_neither_row_nor_ledger_entry(db_connection, tenant_a_id):
    captured: dict = {}
    with pytest.raises(_DeliberateRollback):
        with db_connection.transaction():
            created = create_organization(
                db_connection, tenant_a_id, OrganizationInput(legal_name="Rollback Ltd"),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
            captured["id"] = created.organization_id
            raise _DeliberateRollback()

    with db_connection.transaction():
        gone = get_organization(db_connection, tenant_a_id, captured["id"])
    assert gone is None
    assert [e for e in _ledger(db_connection, tenant_a_id) if e.object_id == captured["id"]] == []


@pytest.mark.integration
def test_update_ledgers_the_previous_values(db_connection, tenant_a_id):
    with db_connection.transaction():
        created = create_organization(
            db_connection, tenant_a_id,
            OrganizationInput(legal_name="Before Ltd", country_code="DE"),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    with db_connection.transaction():
        updated = update_organization(
            db_connection, tenant_a_id, created.organization_id,
            OrganizationInput(legal_name="After Limited", country_code="fr", is_active=False),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert updated is not None
    assert updated.legal_name == "After Limited"
    assert updated.country_code == "FR"
    assert updated.is_active is False
    assert updated.created_at == created.created_at

    entries = [e for e in _ledger(db_connection, tenant_a_id)
               if e.action_type == ACTION_ORGANIZATION_UPDATED]
    assert len(entries) == 1
    assert entries[0].before_state["legal_name"] == "Before Ltd"
    assert entries[0].before_state["country_code"] == "DE"
    assert entries[0].after_state["legal_name"] == "After Limited"
    assert entries[0].after_state["country_code"] == "FR"
    assert entries[0].after_state["is_active"] is False


@pytest.mark.integration
def test_update_of_a_missing_organization_returns_none_and_writes_nothing(
    db_connection, tenant_a_id
):
    before = len(_ledger(db_connection, tenant_a_id))
    with db_connection.transaction():
        result = update_organization(
            db_connection, tenant_a_id, str(uuid.uuid4()),
            OrganizationInput(legal_name="Ghost"), actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert result is None
    assert len(_ledger(db_connection, tenant_a_id)) == before


@pytest.mark.integration
def test_two_organizations_may_share_a_legal_name(db_connection, tenant_a_id):
    # Names are not identity. Both rows must exist as distinct organisations.
    with db_connection.transaction():
        first = create_organization(
            db_connection, tenant_a_id, OrganizationInput(legal_name="Example Group Services Ltd"),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
        second = create_organization(
            db_connection, tenant_a_id, OrganizationInput(legal_name="Example Group Services Ltd"),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert first.organization_id != second.organization_id
    with db_connection.transaction():
        names = [o.legal_name for o in list_organizations(db_connection, tenant_a_id)]
    assert names.count("Example Group Services Ltd") == 2


# ── Database-level canonical-form constraints (raw SQL) ─────────────────────


@pytest.mark.integration
@pytest.mark.parametrize("bad_name", ["", "   ", " Padded Ltd", "Trailing Ltd "])
def test_db_rejects_a_non_canonical_legal_name(db_connection, tenant_a_id, bad_name):
    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            _raw_insert_org(db_connection, tenant_a_id, bad_name)
    assert "ck_dora_organizations_legal_name_canonical" in str(excinfo.value)


@pytest.mark.integration
@pytest.mark.parametrize("bad_code", ["de", "D", "DEU", "1A", "D-"])
def test_db_rejects_a_non_canonical_country_code(db_connection, tenant_a_id, bad_code):
    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            _raw_insert_org(db_connection, tenant_a_id, "Some Bank", bad_code)
    # Two-letter-shaped mistakes hit the CHECK; a longer code is refused by the
    # VARCHAR(2) column before the CHECK is even evaluated. Either is the
    # database saying no, which is the point.
    message = str(excinfo.value)
    assert (
        "ck_dora_organizations_country_code_iso2" in message
        or "character varying(2)" in message
    )


# ── Identifiers ─────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_identifier_is_stored_canonical_and_ledgered(db_connection, tenant_a_id):
    with db_connection.transaction():
        org = create_organization(
            db_connection, tenant_a_id, OrganizationInput(legal_name="Alpha Bank AG"),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
        added = add_organization_identifier(
            db_connection, tenant_a_id, org.organization_id, " lei ", "  5493001KJTIIGC8Y1R12 ",
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    assert added.identifier_type == "LEI"
    assert added.identifier_value == "5493001KJTIIGC8Y1R12"

    with db_connection.transaction():
        listed = list_organization_identifiers(db_connection, tenant_a_id, org.organization_id)
    assert [(i.identifier_type, i.identifier_value) for i in listed] == [
        ("LEI", "5493001KJTIIGC8Y1R12")
    ]
    entries = [e for e in _ledger(db_connection, tenant_a_id)
               if e.action_type == ACTION_ORGANIZATION_IDENTIFIER_ADDED]
    assert len(entries) == 1
    assert entries[0].object_type == OBJECT_TYPE_ORGANIZATION_IDENTIFIER
    assert entries[0].object_id == added.organization_identifier_id
    assert entries[0].after_state["identifier_value"] == "5493001KJTIIGC8Y1R12"


@pytest.mark.integration
def test_db_unique_rejects_the_same_identifier_on_two_organizations_in_one_tenant(
    db_connection, tenant_a_id
):
    # Raw SQL, bypassing the service's pre-check: the UNIQUE constraint is the
    # guarantee, and this is what a service bug or a direct writer would hit.
    with db_connection.transaction():
        first = _raw_insert_org(db_connection, tenant_a_id, "First Ltd")
        second = _raw_insert_org(db_connection, tenant_a_id, "Second Ltd")
        db_connection.execute(
            "INSERT INTO dora_organization_identifiers (organization_identifier_id, tenant_id, "
            "organization_id, identifier_type, identifier_value) VALUES (%s, %s, %s, 'LEI', 'X1')",
            [str(uuid.uuid4()), str(tenant_a_id), first],
        )
    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(
                "INSERT INTO dora_organization_identifiers (organization_identifier_id, tenant_id, "
                "organization_id, identifier_type, identifier_value) VALUES (%s, %s, %s, 'LEI', 'X1')",
                [str(uuid.uuid4()), str(tenant_a_id), second],
            )
    assert "uq_dora_organization_identifiers_tenant_type_value" in str(excinfo.value)


@pytest.mark.integration
def test_the_same_identifier_may_exist_in_another_tenant(db_connection, tenant_a_id, tenant_b_id):
    # AWS's LEI is AWS's LEI in every tenant that deals with AWS. Uniqueness is
    # per tenant, never global.
    for tenant in (tenant_a_id, tenant_b_id):
        with db_connection.transaction():
            org = create_organization(
                db_connection, tenant, OrganizationInput(legal_name="AWS EMEA SARL"),
                actor_id=_ACTOR, actor_role=_ROLE,
            )
            add_organization_identifier(
                db_connection, tenant, org.organization_id, "LEI", "SAME-IN-BOTH",
                actor_id=_ACTOR, actor_role=_ROLE,
            )
    for tenant in (tenant_a_id, tenant_b_id):
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant)])
            count = db_connection.execute(
                "SELECT count(*) FROM dora_organization_identifiers "
                "WHERE tenant_id = %s AND identifier_value = 'SAME-IN-BOTH'",
                [str(tenant)],
            ).fetchone()[0]
        assert count == 1


@pytest.mark.integration
@pytest.mark.parametrize(
    ("bad_type", "bad_value", "constraint"),
    [
        ("lei", "OK", "ck_dora_organization_identifiers_type_canonical"),
        (" LEI", "OK", "ck_dora_organization_identifiers_type_canonical"),
        ("", "OK", "ck_dora_organization_identifiers_type_canonical"),
        ("LEI", " padded", "ck_dora_organization_identifiers_value_canonical"),
        ("LEI", "", "ck_dora_organization_identifiers_value_canonical"),
    ],
)
def test_db_rejects_a_non_canonical_identifier(
    db_connection, tenant_a_id, bad_type, bad_value, constraint
):
    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            org = _raw_insert_org(db_connection, tenant_a_id, "Some Bank")
            db_connection.execute(
                "INSERT INTO dora_organization_identifiers (organization_identifier_id, tenant_id, "
                "organization_id, identifier_type, identifier_value) VALUES (%s, %s, %s, %s, %s)",
                [str(uuid.uuid4()), str(tenant_a_id), org, bad_type, bad_value],
            )
    assert constraint in str(excinfo.value)


@pytest.mark.integration
def test_adding_an_identifier_to_an_unknown_organization_is_not_found(db_connection, tenant_a_id):
    with pytest.raises(EntryNotFoundError):
        with db_connection.transaction():
            add_organization_identifier(
                db_connection, tenant_a_id, str(uuid.uuid4()), "LEI", "X",
                actor_id=_ACTOR, actor_role=_ROLE,
            )


# ── Roles ───────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_one_organization_may_hold_both_initial_roles(db_connection, tenant_a_id):
    # The dual-hat group bank: a regulated financial entity that is also the
    # group's ICT provider. One organisation, two roles, no duplicate master.
    with db_connection.transaction():
        org = create_organization(
            db_connection, tenant_a_id, OrganizationInput(legal_name="Group Bank AG"),
            actor_id=_ACTOR, actor_role=_ROLE,
        )
        add_organization_role(
            db_connection, tenant_a_id, org.organization_id, "financial_entity",
            actor_id=_ACTOR, actor_role=_ROLE,
        )
        add_organization_role(
            db_connection, tenant_a_id, org.organization_id, "ict_provider",
            actor_id=_ACTOR, actor_role=_ROLE,
        )
    with db_connection.transaction():
        roles = list_organization_roles(db_connection, tenant_a_id, org.organization_id)
    assert sorted(r.role_type for r in roles) == ["financial_entity", "ict_provider"]
    with db_connection.transaction():
        orgs = list_organizations(db_connection, tenant_a_id)
    assert [o.legal_name for o in orgs].count("Group Bank AG") == 1

    entries = [e for e in _ledger(db_connection, tenant_a_id)
               if e.action_type == ACTION_ORGANIZATION_ROLE_ADDED]
    assert len(entries) == 2
    assert {e.object_type for e in entries} == {OBJECT_TYPE_ORGANIZATION_ROLE}
    assert {e.after_state["role_type"] for e in entries} == {"financial_entity", "ict_provider"}


@pytest.mark.integration
def test_db_unique_rejects_the_same_role_twice(db_connection, tenant_a_id):
    with db_connection.transaction():
        org = _raw_insert_org(db_connection, tenant_a_id, "Some Bank")
        db_connection.execute(
            "INSERT INTO dora_organization_roles (organization_role_id, tenant_id, "
            "organization_id, role_type) VALUES (%s, %s, %s, 'ict_provider')",
            [str(uuid.uuid4()), str(tenant_a_id), org],
        )
    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
            db_connection.execute(
                "INSERT INTO dora_organization_roles (organization_role_id, tenant_id, "
                "organization_id, role_type) VALUES (%s, %s, %s, 'ict_provider')",
                [str(uuid.uuid4()), str(tenant_a_id), org],
            )
    assert "uq_dora_organization_roles_tenant_org_role" in str(excinfo.value)


@pytest.mark.integration
@pytest.mark.parametrize(
    "bad_role",
    ["direct_provider", "subcontractor", "rank_1", "rank_2", "signatory", "consumer", "FINANCIAL_ENTITY"],
)
def test_db_check_rejects_any_role_outside_the_initial_vocabulary(db_connection, tenant_a_id, bad_role):
    # These are relationship-level facts (DORA_MODEL_V2.md §9, §16, §20), not
    # organisation roles, and the CHECK admits exactly two lower-case strings.
    with pytest.raises(Exception) as excinfo:
        with db_connection.transaction():
            org = _raw_insert_org(db_connection, tenant_a_id, "Some Bank")
            db_connection.execute(
                "INSERT INTO dora_organization_roles (organization_role_id, tenant_id, "
                "organization_id, role_type) VALUES (%s, %s, %s, %s)",
                [str(uuid.uuid4()), str(tenant_a_id), org, bad_role],
            )
    assert "ck_dora_organization_roles_role_type" in str(excinfo.value)


@pytest.mark.integration
def test_adding_a_role_to_an_unknown_organization_is_not_found(db_connection, tenant_a_id):
    with pytest.raises(EntryNotFoundError):
        with db_connection.transaction():
            add_organization_role(
                db_connection, tenant_a_id, str(uuid.uuid4()), "ict_provider",
                actor_id=_ACTOR, actor_role=_ROLE,
            )
