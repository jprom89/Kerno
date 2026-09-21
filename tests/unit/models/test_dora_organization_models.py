"""Unit tests for the three DORA-V2-001 organisation models.

Plain-English summary
---------------------
Verifies, without a database, that the ORM models describe the same tables,
columns, constraints and role vocabulary that migration 025 creates. The live
behaviour of those constraints is proven in tests/integration and
tests/security; this file catches the model and the migration drifting apart.

How to run
----------
    pytest tests/unit/models/test_dora_organization_models.py -v
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from src.models import Base
from src.models.dora_organization import DORAOrganization
from src.models.dora_organization_identifier import DORAOrganizationIdentifier
from src.models.dora_organization_role import (
    ALLOWED_ROLE_TYPES,
    ROLE_TYPE_FINANCIAL_ENTITY,
    ROLE_TYPE_ICT_PROVIDER,
    DORAOrganizationRole,
)

_TIMESTAMPS = {"created_at", "updated_at"}


def _constraint_names(model, kind) -> set[str]:
    return {c.name for c in model.__table__.constraints if isinstance(c, kind)}


def _foreign_key(model, name: str) -> ForeignKeyConstraint:
    for constraint in model.__table__.constraints:
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name:
            return constraint
    raise AssertionError(f"{model.__tablename__} declares no foreign key named {name!r}")


def test_all_three_tables_register_on_the_shared_base() -> None:
    for name in ("dora_organizations", "dora_organization_identifiers", "dora_organization_roles"):
        assert name in Base.metadata.tables


def test_organization_columns_match_migration_025() -> None:
    columns = {c.name for c in DORAOrganization.__table__.columns}
    assert columns == {
        "organization_id", "tenant_id", "legal_name", "country_code", "is_active",
    } | _TIMESTAMPS
    assert DORAOrganization.__table__.c.country_code.nullable is True
    assert DORAOrganization.__table__.c.legal_name.nullable is False


def test_organization_declares_the_composite_candidate_key_and_checks() -> None:
    assert "uq_dora_organizations_tenant_org" in _constraint_names(DORAOrganization, UniqueConstraint)
    assert _constraint_names(DORAOrganization, CheckConstraint) == {
        "ck_dora_organizations_legal_name_canonical",
        "ck_dora_organizations_country_code_iso2",
    }


def test_identifier_columns_and_constraints_match_migration_025() -> None:
    columns = {c.name for c in DORAOrganizationIdentifier.__table__.columns}
    assert columns == {
        "organization_identifier_id", "tenant_id", "organization_id",
        "identifier_type", "identifier_value", "is_active",
    } | _TIMESTAMPS
    composite = _foreign_key(DORAOrganizationIdentifier, "fk_dora_organization_identifiers_organization")
    assert [col.name for col in composite.columns] == ["tenant_id", "organization_id"]
    assert [el.target_fullname for el in composite.elements] == [
        "dora_organizations.tenant_id", "dora_organizations.organization_id",
    ]
    assert "uq_dora_organization_identifiers_tenant_type_value" in _constraint_names(
        DORAOrganizationIdentifier, UniqueConstraint
    )


def test_role_columns_and_constraints_match_migration_025() -> None:
    columns = {c.name for c in DORAOrganizationRole.__table__.columns}
    assert columns == {
        "organization_role_id", "tenant_id", "organization_id", "role_type", "is_active",
    } | _TIMESTAMPS
    composite = _foreign_key(DORAOrganizationRole, "fk_dora_organization_roles_organization")
    assert [col.name for col in composite.columns] == ["tenant_id", "organization_id"]
    assert "uq_dora_organization_roles_tenant_org_role" in _constraint_names(DORAOrganizationRole, UniqueConstraint)
    assert "ck_dora_organization_roles_role_type" in _constraint_names(DORAOrganizationRole, CheckConstraint)


@pytest.mark.parametrize("model", [DORAOrganization, DORAOrganizationIdentifier, DORAOrganizationRole])
def test_each_table_declares_the_tenants_foreign_key_migration_025_created(model) -> None:
    # Migration 025 wrote an inline REFERENCES tenants(tenant_id), which
    # PostgreSQL names <table>_tenant_id_fkey. The model carries the same
    # name so Alembic's comparison matches it by name, not only by shape.
    tenants_fk = _foreign_key(model, f"{model.__tablename__}_tenant_id_fkey")
    assert [col.name for col in tenants_fk.columns] == ["tenant_id"]
    assert [el.target_fullname for el in tenants_fk.elements] == ["tenants.tenant_id"]


@pytest.mark.parametrize("model", [DORAOrganization, DORAOrganizationIdentifier, DORAOrganizationRole])
def test_server_defaults_match_migration_025(model) -> None:
    # The database, not the application, supplies these when a raw INSERT
    # omits them. A model without them is what makes autogenerate propose
    # dropping them from the live table.
    primary_key = list(model.__table__.primary_key.columns)[0]
    assert str(primary_key.server_default.arg) == "gen_random_uuid()"
    assert str(model.__table__.c.is_active.server_default.arg) == "true"
    assert str(model.__table__.c.created_at.server_default.arg) == "now()"
    assert str(model.__table__.c.updated_at.server_default.arg) == "now()"


def test_only_the_identifier_table_carries_a_plain_index_and_it_is_tenant_org() -> None:
    assert {ix.name for ix in DORAOrganization.__table__.indexes} == set()
    assert {ix.name for ix in DORAOrganizationRole.__table__.indexes} == set()
    identifier_indexes = {ix.name: [c.name for c in ix.columns]
                          for ix in DORAOrganizationIdentifier.__table__.indexes}
    assert identifier_indexes == {
        "ix_dora_organization_identifiers_tenant_org": ["tenant_id", "organization_id"],
    }


def test_role_vocabulary_is_exactly_the_two_initial_roles() -> None:
    assert ALLOWED_ROLE_TYPES == frozenset({"financial_entity", "ict_provider"})
    assert ROLE_TYPE_FINANCIAL_ENTITY == "financial_entity"
    assert ROLE_TYPE_ICT_PROVIDER == "ict_provider"


def test_no_provenance_or_template_columns_were_added() -> None:
    forbidden = {
        "source_system", "source_record_id", "source_file", "source_row", "evidence_id",
        "register_scope_id", "ict_service_type", "rank", "substitutability",
        "criticality", "annual_expense", "reference_date", "package_version",
    }
    for model in (DORAOrganization, DORAOrganizationIdentifier, DORAOrganizationRole):
        assert not forbidden & {c.name for c in model.__table__.columns}, model.__tablename__
