"""Unit tests for the DORA-V2-002B contract relationship model and migration 027.

Verifies, without a database, that the ORM model describes the columns,
defaults, constraints and indexes migration 027 creates — the partial unique
index's predicate included — that the relationship-type vocabulary is exactly
'overarching', that migration 027 sits directly on 026 inside a single-headed
chain, and that dora_contracts gained no hierarchy column. Live behaviour is proven in
tests/integration and tests/security.

Run: pytest tests/unit/models/test_dora_contract_relationship_model.py -v
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import re

from sqlalchemy import CheckConstraint, ForeignKeyConstraint

from src.models import Base
from src.models.dora_contract import DORAContract
from src.models.dora_contract_relationship import (
    ALLOWED_RELATIONSHIP_TYPES,
    RELATIONSHIP_TYPE_OVERARCHING,
    DORAContractRelationship,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_MIGRATION_027 = _REPO_ROOT / "migrations" / "versions" / "027_create_dora_contract_relationships.py"
_TABLE = DORAContractRelationship.__table__


def _load_migration_027():
    """Import migration 027 by path; its file name starts with digits and is not importable by name."""
    spec = importlib.util.spec_from_file_location("migration_027", _MIGRATION_027)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _foreign_keys() -> dict[str, ForeignKeyConstraint]:
    """Return the model's named foreign keys by name."""
    return {c.name: c for c in _TABLE.constraints if isinstance(c, ForeignKeyConstraint)}


def test_the_table_registers_on_the_shared_base():
    assert Base.metadata.tables["dora_contract_relationships"] is _TABLE


def test_columns_types_and_nullability_match_migration_027():
    columns = {c.name: (type(c.type).__name__, c.nullable) for c in _TABLE.columns}
    assert columns == {
        "contract_relationship_id": ("UUID", False),
        "tenant_id": ("UUID", False),
        "child_contract_id": ("UUID", False),
        "parent_contract_id": ("UUID", False),
        "relationship_type": ("Text", False),
        "is_active": ("Boolean", False),
        "created_at": ("DateTime", False),
        "updated_at": ("DateTime", False),
    }
    assert [c.name for c in _TABLE.primary_key.columns] == ["contract_relationship_id"]
    assert _TABLE.c.created_at.type.timezone and _TABLE.c.updated_at.type.timezone


def test_server_defaults_match_migration_027():
    defaults = {c.name: str(c.server_default.arg) for c in _TABLE.columns if c.server_default is not None}
    assert defaults == {
        "contract_relationship_id": "gen_random_uuid()",
        "is_active": "true",
        "created_at": "now()",
        "updated_at": "now()",
    }


def test_both_endpoints_carry_a_composite_foreign_key_and_the_tenant_has_its_own():
    keys = _foreign_keys()
    assert set(keys) == {
        "dora_contract_relationships_tenant_id_fkey",
        "fk_dora_contract_relationships_child",
        "fk_dora_contract_relationships_parent",
    }
    for name, column in (("fk_dora_contract_relationships_child", "child_contract_id"),
                         ("fk_dora_contract_relationships_parent", "parent_contract_id")):
        constraint = keys[name]
        assert [c.name for c in constraint.columns] == ["tenant_id", column]
        assert [e.target_fullname for e in constraint.elements] == [
            "dora_contracts.tenant_id", "dora_contracts.contract_id",
        ]
    assert [e.target_fullname for e in keys["dora_contract_relationships_tenant_id_fkey"].elements] == [
        "tenants.tenant_id"
    ]


def test_the_checks_forbid_a_self_link_and_admit_only_the_overarching_type():
    checks = {c.name: str(c.sqltext) for c in _TABLE.constraints if isinstance(c, CheckConstraint)}
    assert checks == {
        "ck_dora_contract_relationships_not_self": "child_contract_id <> parent_contract_id",
        "ck_dora_contract_relationships_type": "relationship_type = 'overarching'",
    }
    assert ALLOWED_RELATIONSHIP_TYPES == frozenset({RELATIONSHIP_TYPE_OVERARCHING}) == frozenset({"overarching"})


def test_one_active_parent_is_a_partial_unique_index_and_the_other_two_are_plain_lookups():
    indexes = {ix.name: ix for ix in _TABLE.indexes}
    assert set(indexes) == {
        "uq_dora_contract_relationships_one_active_parent",
        "ix_dora_contract_relationships_tenant_child",
        "ix_dora_contract_relationships_tenant_parent",
    }
    one_active = indexes["uq_dora_contract_relationships_one_active_parent"]
    assert one_active.unique is True
    assert [c.name for c in one_active.columns] == ["tenant_id", "child_contract_id", "relationship_type"]
    assert str(one_active.dialect_options["postgresql"]["where"]) == "is_active"
    for name, columns in (("ix_dora_contract_relationships_tenant_child", ["tenant_id", "child_contract_id"]),
                          ("ix_dora_contract_relationships_tenant_parent", ["tenant_id", "parent_contract_id"])):
        assert indexes[name].unique is False
        assert [c.name for c in indexes[name].columns] == columns
        assert indexes[name].dialect_options["postgresql"]["where"] is None


def test_no_unique_constraint_competes_with_the_partial_index():
    assert {c.name for c in _TABLE.constraints if c.__class__.__name__ == "UniqueConstraint"} == set()


def test_dora_contracts_gained_no_hierarchy_column():
    names = {c.name for c in DORAContract.__table__.columns}
    assert not names & {"parent_contract_id", "overarching_contract_id", "contract_type", "is_standalone"}


def test_migration_027_sits_directly_on_026_inside_a_single_headed_chain():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    migration = _load_migration_027()
    assert (migration.revision, migration.down_revision) == ("b3c4d5e6", "a2b3c4d5")
    script = ScriptDirectory.from_config(Config(str(_REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1
    assert "b3c4d5e6" in [s.revision for s in script.walk_revisions(head=heads[0])]


def test_migration_027_downgrade_drops_only_its_own_table():
    source = inspect.getsource(_load_migration_027().downgrade)
    assert re.findall(r"DROP \w+", source) == ["DROP TABLE"]
    assert "_TABLE" in source and _load_migration_027()._TABLE == "dora_contract_relationships"


def test_migration_027_writes_the_same_index_predicate_and_checks_as_the_model():
    source = _MIGRATION_027.read_text(encoding="utf-8")
    assert "(tenant_id, child_contract_id, relationship_type) WHERE is_active" in source
    assert "CHECK (child_contract_id <> parent_contract_id)" in source
    assert "CHECK (relationship_type = 'overarching')" in source
    assert 'op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")' in source
    assert 'op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")' in source


def test_env_py_registers_the_relationship_model_for_autogenerate():
    env_py = (_REPO_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
    registered = set(re.findall(r"^import (src\.models\.\w+)", env_py, re.MULTILINE))
    assert "src.models.dora_contract_relationship" in registered
