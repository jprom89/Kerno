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
    fks = [c for c in DORAOrganizationIdentifier.__table__.constraints if isinstance(c, ForeignKeyConstraint)]
    assert len(fks) == 1
    assert [col.name for col in fks[0].columns] == ["tenant_id", "organization_id"]
    assert "uq_dora_organization_identifiers_tenant_type_value" in _constraint_names(
        DORAOrganizationIdentifier, UniqueConstraint
    )


def test_role_columns_and_constraints_match_migration_025() -> None:
    columns = {c.name for c in DORAOrganizationRole.__table__.columns}
    assert columns == {
        "organization_role_id", "tenant_id", "organization_id", "role_type", "is_active",
    } | _TIMESTAMPS
    fks = [c for c in DORAOrganizationRole.__table__.constraints if isinstance(c, ForeignKeyConstraint)]
    assert [col.name for col in fks[0].columns] == ["tenant_id", "organization_id"]
    assert "uq_dora_organization_roles_tenant_org_role" in _constraint_names(DORAOrganizationRole, UniqueConstraint)
    assert "ck_dora_organization_roles_role_type" in _constraint_names(DORAOrganizationRole, CheckConstraint)


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
