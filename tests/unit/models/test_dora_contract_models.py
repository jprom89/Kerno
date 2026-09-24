"""Unit tests for the two DORA-V2-002A contract models.

Plain-English summary
---------------------
Verifies, without a database, that the ORM models describe the columns,
constraints, defaults, index and role vocabulary migration 026 creates, and
that the one contract whitespace policy is the same set of characters in the
service constant, the model CHECK text and the migration's CHECK text. Live
behaviour is proven in tests/integration and tests/security.

How to run
----------
    pytest tests/unit/models/test_dora_contract_models.py -v
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from src.models import Base
from src.models.dora_contract import CONTRACT_TEXT_TRIM_CHARACTERS, DORAContract
from src.models.dora_contract_party import (
    ALLOWED_PARTY_ROLES,
    PARTY_ROLE_INTRAGROUP_PROVIDER_SIGNATORY,
    PARTY_ROLE_PROVIDER_SIGNATORY,
    PARTY_ROLE_RECIPIENT_SIGNATORY,
    PROVIDER_PARTY_ROLES,
    DORAContractParty,
)

_TIMESTAMPS = {"created_at", "updated_at"}
_MIGRATION_026 = (
    pathlib.Path(__file__).resolve().parents[3]
    / "migrations" / "versions" / "026_create_dora_contract_foundation.py"
)
_ESCAPED_LITERAL = re.compile(r"E'((?:\\u[0-9a-f]{4})+)'")


def _constraint_names(model, kind) -> set[str]:
    """Return the names of every constraint of one SQLAlchemy kind on the model's table."""
    return {c.name for c in model.__table__.constraints if isinstance(c, kind)}


def _foreign_key(model, name: str) -> ForeignKeyConstraint:
    """Return the model's foreign key with this exact name, or fail naming the model."""
    for constraint in model.__table__.constraints:
        if isinstance(constraint, ForeignKeyConstraint) and constraint.name == name:
            return constraint
    raise AssertionError(f"{model.__tablename__} declares no foreign key named {name!r}")


def _decode_escaped_literals(sql: str) -> list[str]:
    """Return every E'\\uXXXX…' literal in the SQL text, decoded to the characters it names."""
    decoded = []
    for escapes in _ESCAPED_LITERAL.findall(sql):
        code_points = escapes.split("\\u")[1:]
        decoded.append("".join(chr(int(point, 16)) for point in code_points))
    return decoded


def _load_migration_026():
    """Import migration 026 by path; its file name starts with digits and is not importable by name."""
    spec = importlib.util.spec_from_file_location("migration_026", _MIGRATION_026)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Tables and columns ──────────────────────────────────────────────────────


def test_both_tables_register_on_the_shared_base() -> None:
    for name in ("dora_contracts", "dora_contract_parties"):
        assert name in Base.metadata.tables


def test_contract_columns_and_nullability_match_migration_026() -> None:
    table = DORAContract.__table__
    assert {c.name for c in table.columns} == {
        "contract_id", "tenant_id", "contract_reference", "display_name",
        "contract_start_date", "contract_end_date", "is_active",
    } | _TIMESTAMPS
    nullable = {c.name for c in table.columns if c.nullable}
    assert nullable == {"display_name", "contract_start_date", "contract_end_date"}


def test_party_columns_and_nullability_match_migration_026() -> None:
    table = DORAContractParty.__table__
    assert {c.name for c in table.columns} == {
        "contract_party_id", "tenant_id", "contract_id", "organization_id", "party_role", "is_active",
    } | _TIMESTAMPS
    assert {c.name for c in table.columns if c.nullable} == set()
    assert table.c.party_role.type.length == 32


@pytest.mark.parametrize("model", [DORAContract, DORAContractParty])
def test_server_defaults_match_migration_026(model) -> None:
    primary_key = list(model.__table__.primary_key.columns)[0]
    assert str(primary_key.server_default.arg) == "gen_random_uuid()"
    assert str(model.__table__.c.is_active.server_default.arg) == "true"
    assert str(model.__table__.c.created_at.server_default.arg) == "now()"
    assert str(model.__table__.c.updated_at.server_default.arg) == "now()"
    assert model.__table__.c.updated_at.onupdate is None


# ── Constraints and index ───────────────────────────────────────────────────


def test_contract_constraints_are_named_as_migration_026_names_them() -> None:
    assert _constraint_names(DORAContract, UniqueConstraint) == {
        "uq_dora_contracts_tenant_contract", "uq_dora_contracts_tenant_reference",
    }
    assert _constraint_names(DORAContract, CheckConstraint) == {
        "ck_dora_contracts_reference_canonical",
        "ck_dora_contracts_display_name_canonical",
        "ck_dora_contracts_date_order",
    }
    tenants_fk = _foreign_key(DORAContract, "dora_contracts_tenant_id_fkey")
    assert [el.target_fullname for el in tenants_fk.elements] == ["tenants.tenant_id"]


def test_party_carries_both_composite_foreign_keys_and_the_tenants_key() -> None:
    contract_fk = _foreign_key(DORAContractParty, "fk_dora_contract_parties_contract")
    assert [col.name for col in contract_fk.columns] == ["tenant_id", "contract_id"]
    assert [el.target_fullname for el in contract_fk.elements] == [
        "dora_contracts.tenant_id", "dora_contracts.contract_id",
    ]
    organization_fk = _foreign_key(DORAContractParty, "fk_dora_contract_parties_organization")
    assert [col.name for col in organization_fk.columns] == ["tenant_id", "organization_id"]
    assert [el.target_fullname for el in organization_fk.elements] == [
        "dora_organizations.tenant_id", "dora_organizations.organization_id",
    ]
    tenants_fk = _foreign_key(DORAContractParty, "dora_contract_parties_tenant_id_fkey")
    assert [el.target_fullname for el in tenants_fk.elements] == ["tenants.tenant_id"]
    assert len(_constraint_names(DORAContractParty, ForeignKeyConstraint)) == 3


def test_party_uniqueness_is_the_full_tuple_and_the_only_index_is_the_reverse_lookup() -> None:
    unique = next(
        c for c in DORAContractParty.__table__.constraints
        if isinstance(c, UniqueConstraint) and c.name == "uq_dora_contract_parties_tenant_tuple"
    )
    assert [col.name for col in unique.columns] == [
        "tenant_id", "contract_id", "organization_id", "party_role",
    ]
    indexes = {ix.name: [c.name for c in ix.columns] for ix in DORAContractParty.__table__.indexes}
    assert indexes == {
        "ix_dora_contract_parties_tenant_org_contract": ["tenant_id", "organization_id", "contract_id"],
    }
    assert {ix.name for ix in DORAContract.__table__.indexes} == set()


# ── Vocabulary ──────────────────────────────────────────────────────────────


def test_party_role_vocabulary_is_exactly_the_three_initial_roles() -> None:
    assert ALLOWED_PARTY_ROLES == frozenset(
        {"recipient_signatory", "provider_signatory", "intragroup_provider_signatory"}
    )
    assert PARTY_ROLE_RECIPIENT_SIGNATORY == "recipient_signatory"
    assert PARTY_ROLE_PROVIDER_SIGNATORY == "provider_signatory"
    assert PARTY_ROLE_INTRAGROUP_PROVIDER_SIGNATORY == "intragroup_provider_signatory"
    assert PROVIDER_PARTY_ROLES == frozenset({"provider_signatory", "intragroup_provider_signatory"})


def test_the_party_role_check_admits_exactly_the_vocabulary() -> None:
    check = next(
        c for c in DORAContractParty.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_dora_contract_parties_party_role"
    )
    assert set(re.findall(r"'([a-z_]+)'", str(check.sqltext))) == ALLOWED_PARTY_ROLES


# ── One whitespace policy ───────────────────────────────────────────────────


def test_the_trim_set_is_every_character_python_calls_whitespace() -> None:
    # The policy is an explicit list, not a call to str.isspace(), so the
    # database can be given exactly the same list. If a future Python widens
    # isspace(), this fails and the list is extended deliberately — in the
    # constant, the model CHECKs and a new migration together.
    python_whitespace = {chr(point) for point in range(0x110000) if chr(point).isspace()}
    assert set(CONTRACT_TEXT_TRIM_CHARACTERS) == python_whitespace
    assert len(CONTRACT_TEXT_TRIM_CHARACTERS) == len(set(CONTRACT_TEXT_TRIM_CHARACTERS))


@pytest.mark.parametrize(
    "check_name",
    ["ck_dora_contracts_reference_canonical", "ck_dora_contracts_display_name_canonical"],
)
def test_the_model_checks_trim_exactly_the_service_set(check_name) -> None:
    check = next(
        c for c in DORAContract.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == check_name
    )
    assert _decode_escaped_literals(str(check.sqltext)) == [CONTRACT_TEXT_TRIM_CHARACTERS]


def test_migration_026_trims_exactly_the_service_set() -> None:
    migration = _load_migration_026()
    assert _decode_escaped_literals(migration._TRIM_CHARACTERS_SQL) == [CONTRACT_TEXT_TRIM_CHARACTERS]
    assert migration.revision == "a2b3c4d5"
    assert migration.down_revision == "z1a2b3c4"
    assert set(migration._INITIAL_PARTY_ROLES) == ALLOWED_PARTY_ROLES


# ── What this slice must not contain ────────────────────────────────────────


def test_no_hierarchy_cost_provider_identity_template_or_provenance_columns() -> None:
    forbidden = {
        "provider_name", "provider_id", "annual_expense", "current_year_cost",
        "parent_contract_id", "overarching_contract_id", "contract_type",
        "is_standalone", "standalone", "arrangement_type", "template_row_id",
        "b_02_01_row", "source_system", "source_record_id", "source_file",
        "source_row", "evidence_id", "register_profile_id", "register_scope_id",
        "consumer_organization_id", "service_usage_id", "function_id", "ict_service_id",
    }
    for model in (DORAContract, DORAContractParty):
        assert not forbidden & {c.name for c in model.__table__.columns}, model.__tablename__
