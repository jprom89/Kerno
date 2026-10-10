"""Unit tests for the SEC-REMED-005 review binding: migration 028 and the models it touches.

Verifies, without a database, that migration 028 sits directly on 027 inside a
single-headed chain, that its upgrade adds exactly the candidate key, the
nullable binding column, the composite foreign key and its index and writes no
data, that its downgrade drops all four, and that the Override and
Recommendation models describe the shape migrations 003, 010 and 028 create.
Live behaviour is proven in tests/integration/test_sec_remed_005_review_binding.py.

Run: pytest tests/unit/models/test_review_binding_models.py -v
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import ForeignKeyConstraint, Index, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID

from src.models.override import Override
from src.models.recommendation import Recommendation

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_MIGRATION_028 = _REPO_ROOT / "migrations" / "versions" / "028_bind_overrides_to_reviewed_recommendations.py"
_OVERRIDES = Override.__table__
_RECOMMENDATIONS = Recommendation.__table__

_UPGRADE_STATEMENTS = [
    "ALTER TABLE recommendations ADD CONSTRAINT uq_recommendations_tenant_recommendation_control "
    "UNIQUE (tenant_id, recommendation_id, control_id)",
    "ALTER TABLE overrides ADD COLUMN recommendation_id UUID",
    "ALTER TABLE overrides ADD CONSTRAINT fk_overrides_reviewed_recommendation "
    "FOREIGN KEY (tenant_id, recommendation_id, original_control_id) "
    "REFERENCES recommendations (tenant_id, recommendation_id, control_id)",
    "CREATE INDEX ix_overrides_tenant_recommendation ON overrides (tenant_id, recommendation_id, created_at)",
]

_DOWNGRADE_STATEMENTS = [
    "DROP INDEX IF EXISTS ix_overrides_tenant_recommendation",
    "ALTER TABLE overrides DROP CONSTRAINT IF EXISTS fk_overrides_reviewed_recommendation",
    "ALTER TABLE overrides DROP COLUMN IF EXISTS recommendation_id",
    "ALTER TABLE recommendations DROP CONSTRAINT IF EXISTS uq_recommendations_tenant_recommendation_control",
]


class _RecordingOp:
    """Stands in for alembic's op proxy and keeps every SQL statement a migration step executes.

    It offers execute() only, so a step that reached for any other operation
    (add_column, bulk_insert, get_bind) fails the test instead of passing unseen.
    """

    def __init__(self) -> None:
        """Start with no recorded statements."""
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        """Record one statement with its whitespace collapsed to single spaces."""
        self.statements.append(" ".join(sql.split()))


def _load_migration_028():
    """Import migration 028 by path; its file name starts with digits and is not importable by name."""
    spec = importlib.util.spec_from_file_location("migration_028", _MIGRATION_028)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recorded_statements(step_name: str) -> list[str]:
    """Run migration 028's upgrade or downgrade against a recording op and return the SQL it executed."""
    migration = _load_migration_028()
    recorder = _RecordingOp()
    migration.op = recorder
    getattr(migration, step_name)()
    return recorder.statements


def _foreign_keys(table) -> dict[str, ForeignKeyConstraint]:
    """Return the table's named foreign keys by name."""
    return {c.name: c for c in table.constraints if isinstance(c, ForeignKeyConstraint)}


def _indexes(table) -> dict[str, Index]:
    """Return the table's indexes by name."""
    return {ix.name: ix for ix in table.indexes}


def test_migration_028_sits_directly_on_027_inside_a_single_headed_chain():
    migration = _load_migration_028()
    assert (migration.revision, migration.down_revision) == ("c4d5e6f7", "b3c4d5e6")
    script = ScriptDirectory.from_config(Config(str(_REPO_ROOT / "alembic.ini")))
    heads = script.get_heads()
    assert len(heads) == 1
    assert "c4d5e6f7" in [s.revision for s in script.walk_revisions(head=heads[0])]


def test_migration_028_upgrade_adds_exactly_the_key_the_column_the_foreign_key_and_the_index():
    assert _recorded_statements("upgrade") == _UPGRADE_STATEMENTS


def test_migration_028_adds_the_binding_column_nullable_and_without_a_default():
    added = [s for s in _recorded_statements("upgrade") if "ADD COLUMN" in s]
    assert added == ["ALTER TABLE overrides ADD COLUMN recommendation_id UUID"]
    assert "NOT NULL" not in added[0]
    assert "DEFAULT" not in added[0]


def test_migration_028_downgrade_drops_all_four_objects_foreign_key_before_its_target_key():
    assert _recorded_statements("downgrade") == _DOWNGRADE_STATEMENTS


def test_migration_028_writes_no_data():
    source = _MIGRATION_028.read_text(encoding="utf-8")
    assert re.findall(r"\b(INSERT|UPDATE|DELETE)\b", source) == []
    for step_name in ("upgrade", "downgrade"):
        for statement in _recorded_statements(step_name):
            assert statement.split()[0].upper() in {"ALTER", "CREATE", "DROP"}


def test_override_binding_column_is_a_nullable_uuid_with_no_default():
    column = _OVERRIDES.c.recommendation_id
    assert isinstance(column.type, PostgresUUID)
    assert column.type.as_uuid is True
    assert column.nullable is True
    assert column.default is None
    assert column.server_default is None


def test_override_foreign_keys_are_the_composite_binding_and_the_named_tenant_key():
    keys = _foreign_keys(_OVERRIDES)
    assert set(keys) == {"fk_overrides_reviewed_recommendation", "overrides_tenant_id_fkey"}

    binding = keys["fk_overrides_reviewed_recommendation"]
    assert [c.name for c in binding.columns] == ["tenant_id", "recommendation_id", "original_control_id"]
    assert [e.target_fullname for e in binding.elements] == [
        "recommendations.tenant_id",
        "recommendations.recommendation_id",
        "recommendations.control_id",
    ]
    assert binding.referred_table is _RECOMMENDATIONS

    tenant_key = keys["overrides_tenant_id_fkey"]
    assert [c.name for c in tenant_key.columns] == ["tenant_id"]
    assert [e.target_fullname for e in tenant_key.elements] == ["tenants.tenant_id"]


def test_override_indexes_are_the_tenant_lookup_and_the_binding_lookup():
    indexes = _indexes(_OVERRIDES)
    assert set(indexes) == {"overrides_tenant_id_idx", "ix_overrides_tenant_recommendation"}
    assert [c.name for c in indexes["overrides_tenant_id_idx"].columns] == ["tenant_id"]
    assert [c.name for c in indexes["ix_overrides_tenant_recommendation"].columns] == [
        "tenant_id",
        "recommendation_id",
        "created_at",
    ]
    assert indexes["overrides_tenant_id_idx"].unique is False
    assert indexes["ix_overrides_tenant_recommendation"].unique is False


def test_override_control_and_justification_columns_are_text_like_migration_003():
    nullability = {}
    for name in ("original_control_id", "corrected_control_id", "justification_text"):
        column = _OVERRIDES.c[name]
        assert type(column.type) is Text, name
        nullability[name] = column.nullable
    assert nullability == {
        "original_control_id": False,
        "corrected_control_id": True,
        "justification_text": True,
    }


def test_recommendation_candidate_key_is_the_named_three_column_unique_constraint():
    uniques = {c.name: c for c in _RECOMMENDATIONS.constraints if isinstance(c, UniqueConstraint)}
    assert set(uniques) == {"uq_recommendations_tenant_recommendation_control"}
    assert [c.name for c in uniques["uq_recommendations_tenant_recommendation_control"].columns] == [
        "tenant_id",
        "recommendation_id",
        "control_id",
    ]


def test_recommendation_current_index_matches_migration_010():
    indexes = _indexes(_RECOMMENDATIONS)
    assert set(indexes) == {"idx_recommendations_current"}
    assert [c.name for c in indexes["idx_recommendations_current"].columns] == [
        "tenant_id",
        "control_id",
        "is_superseded",
    ]
    assert indexes["idx_recommendations_current"].unique is False


def test_recommendation_control_id_is_text_and_is_superseded_defaults_false_in_the_database():
    assert type(_RECOMMENDATIONS.c.control_id.type) is Text
    assert _RECOMMENDATIONS.c.control_id.nullable is False
    is_superseded = _RECOMMENDATIONS.c.is_superseded
    assert is_superseded.nullable is False
    assert str(is_superseded.server_default.arg) == "false"


def test_env_py_imports_both_model_modules_for_autogenerate():
    env_py = (_REPO_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
    registered = set(re.findall(r"^import (src\.models\.\w+)", env_py, re.MULTILINE))
    assert {"src.models.override", "src.models.recommendation"} <= registered
