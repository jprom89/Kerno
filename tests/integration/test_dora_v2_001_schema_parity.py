"""DORA-V2-001 — the ORM models and migration 025 describe the same three tables.

What:  runs Alembic's own metadata comparison against the live database, scoped
       to dora_organizations, dora_organization_identifiers and
       dora_organization_roles, with type and server-default comparison switched
       on, and asserts it finds nothing. Then verifies separately, from the
       PostgreSQL catalogs, the constraints that comparison cannot see: CHECK
       expressions, the exact foreign-key and unique-key definitions, and the
       one plain index.
Why:   Alembic autogenerate is what a future engineer will reach for to add a
       column to these tables. If the models drift from what 025 created, that
       engineer's first revision proposes "fixing" the database to match the
       drift — dropping a tenants FK, an index, or a server default. This file
       makes the drift a failing test instead of a silent proposal. The scope is
       deliberate: the historical tables carry known, pre-existing drift that is
       not this ticket's to fix, so the comparison is filtered to the three
       V2-001 tables on both the model side and the reflected side.
How:   pytest tests/integration/test_dora_v2_001_schema_parity.py -m integration -v
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import (
    CheckConstraint,
    DefaultClause,
    ForeignKeyConstraint,
    MetaData,
    UniqueConstraint,
    create_engine,
    text,
)

from src.models import Base
from src.models.dora_organization import DORAOrganization
from src.models.dora_organization_identifier import DORAOrganizationIdentifier
from src.models.dora_organization_role import DORAOrganizationRole
from src.models.tenant import Tenant

# The three tables reference tenants(tenant_id). Tenant is imported so the
# metadata can resolve that reference; the tenants table itself is excluded
# from the comparison by the scope hooks below.

_MODELS = (DORAOrganization, DORAOrganizationIdentifier, DORAOrganizationRole)
_SCOPE = frozenset(model.__tablename__ for model in _MODELS)

# What pg_get_constraintdef() prints for each CHECK migration 025 created. The
# strings are the catalog's normalised form, not the migration's source text,
# so a CHECK that was silently rewritten to something weaker fails here.
_EXPECTED_CHECKS = {
    "ck_dora_organizations_legal_name_canonical":
        "CHECK (((legal_name = btrim(legal_name)) AND (legal_name <> ''::text)))",
    "ck_dora_organizations_country_code_iso2":
        "CHECK (((country_code IS NULL) OR ((country_code)::text ~ '^[A-Z]{2}$'::text)))",
    "ck_dora_organization_identifiers_type_canonical":
        "CHECK ((((identifier_type)::text = upper(btrim((identifier_type)::text))) "
        "AND ((identifier_type)::text <> ''::text)))",
    "ck_dora_organization_identifiers_value_canonical":
        "CHECK (((identifier_value = btrim(identifier_value)) AND (identifier_value <> ''::text)))",
    "ck_dora_organization_roles_role_type":
        "CHECK (((role_type)::text = ANY ((ARRAY['financial_entity'::character varying, "
        "'ict_provider'::character varying])::text[])))",
}

# The same five CHECKs as the models declare them. Alembic never compares
# CHECK expressions, so the model side is pinned here in its source form.
_EXPECTED_MODEL_CHECKS = {
    "ck_dora_organizations_legal_name_canonical":
        "legal_name = btrim(legal_name) AND legal_name <> ''",
    "ck_dora_organizations_country_code_iso2":
        "country_code IS NULL OR country_code ~ '^[A-Z]{2}$'",
    "ck_dora_organization_identifiers_type_canonical":
        "identifier_type = upper(btrim(identifier_type)) AND identifier_type <> ''",
    "ck_dora_organization_identifiers_value_canonical":
        "identifier_value = btrim(identifier_value) AND identifier_value <> ''",
    "ck_dora_organization_roles_role_type":
        "role_type IN ('financial_entity', 'ict_provider')",
}

_EXPECTED_FOREIGN_KEYS = {
    "dora_organizations_tenant_id_fkey":
        "FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)",
    "dora_organization_identifiers_tenant_id_fkey":
        "FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)",
    "fk_dora_organization_identifiers_organization":
        "FOREIGN KEY (tenant_id, organization_id) "
        "REFERENCES dora_organizations(tenant_id, organization_id)",
    "dora_organization_roles_tenant_id_fkey":
        "FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)",
    "fk_dora_organization_roles_organization":
        "FOREIGN KEY (tenant_id, organization_id) "
        "REFERENCES dora_organizations(tenant_id, organization_id)",
}

_EXPECTED_UNIQUES = {
    "uq_dora_organizations_tenant_org": "UNIQUE (tenant_id, organization_id)",
    "uq_dora_organization_identifiers_tenant_type_value":
        "UNIQUE (tenant_id, identifier_type, identifier_value)",
    "uq_dora_organization_roles_tenant_org_role":
        "UNIQUE (tenant_id, organization_id, role_type)",
}

_EXPECTED_PLAIN_INDEXES = {
    "ix_dora_organization_identifiers_tenant_org":
        "CREATE INDEX ix_dora_organization_identifiers_tenant_org "
        "ON public.dora_organization_identifiers USING btree (tenant_id, organization_id)",
}

_CONSTRAINT_DEFS_SQL = """
SELECT con.conname, con.contype, pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = rel.relnamespace
WHERE ns.nspname = 'public' AND rel.relname = %s AND con.contype = %s
"""

_INDEX_DEFS_SQL = """
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND tablename = %s AND indexname NOT LIKE '%%_pkey'
"""


def _in_scope_reflected(name, type_, parent_names) -> bool:
    """Alembic include_name hook: reflect only the three tables from the database."""
    if type_ == "table":
        return name in _SCOPE
    return True


def _in_scope_object(obj, name, type_, reflected, compare_to) -> bool:
    """Alembic include_object hook: compare only objects that belong to the three tables."""
    if type_ == "table":
        return name in _SCOPE
    return True


def _scoped_metadata_diff(metadata: MetaData) -> list:
    """Run compare_metadata for the three tables only, with types and server defaults compared."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as connection:
            migration_context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_name": _in_scope_reflected,
                    "include_object": _in_scope_object,
                },
            )
            return compare_metadata(migration_context, metadata)
    finally:
        engine.dispose()


def _flattened(diffs: list) -> list:
    """Return compare_metadata's output as a flat list: column diffs arrive nested in lists."""
    flat = []
    for group in diffs:
        flat.extend(group if isinstance(group, list) else [group])
    return flat


def _copy_of_the_models(*models) -> MetaData:
    """Return a fresh MetaData holding tenants plus the given models' tables, for mutation tests."""
    copied = MetaData()
    for model in (Tenant, *models):
        model.__table__.to_metadata(copied)
    return copied


def _references(model, table_name: str) -> bool:
    """Return True if any foreign key on the model's table points at table_name."""
    for constraint in model.__table__.constraints:
        if isinstance(constraint, ForeignKeyConstraint) and constraint.referred_table.name == table_name:
            return True
    return False


def _model_check_texts() -> dict[str, str]:
    """Return {name: sqltext} for every CHECK the three models declare."""
    texts: dict[str, str] = {}
    for model in _MODELS:
        for constraint in model.__table__.constraints:
            if isinstance(constraint, CheckConstraint):
                texts[constraint.name] = str(constraint.sqltext)
    return texts


def _catalog_definitions(conn, table: str, constraint_type: str) -> dict[str, str]:
    """Return {constraint_name: definition} for one table and one pg_constraint contype."""
    with conn.transaction():
        rows = conn.execute(_CONSTRAINT_DEFS_SQL, [table, constraint_type]).fetchall()
    return {row[0]: row[2] for row in rows}


def _catalog_indexes(conn, table: str) -> dict[str, str]:
    """Return {index_name: CREATE INDEX statement} for one table, primary key excluded."""
    with conn.transaction():
        rows = conn.execute(_INDEX_DEFS_SQL, [table]).fetchall()
    return {row[0]: row[1] for row in rows}


def _model_constraint_names(kind) -> set[str]:
    """Return every named constraint of one SQLAlchemy kind across the three models."""
    names: set[str] = set()
    for model in _MODELS:
        for constraint in model.__table__.constraints:
            if isinstance(constraint, kind) and constraint.name:
                names.add(constraint.name)
    return names


# ── Alembic's comparison, scoped and with server defaults on ────────────────


@pytest.mark.integration
def test_alembic_finds_no_drift_between_the_models_and_the_live_tables(db_connection):
    diffs = _scoped_metadata_diff(Base.metadata)
    assert diffs == [], (
        "ORM metadata and migration 025 disagree; autogenerate would propose: "
        + "; ".join(str(diff)[:200] for diff in diffs)
    )


@pytest.mark.integration
@pytest.mark.parametrize("missing", _MODELS, ids=[m.__tablename__ for m in _MODELS])
def test_the_scope_filter_is_not_hiding_any_of_the_three_tables(db_connection, missing):
    # A comparison that never looked at the tables would also report no
    # drift. Prove the scope keeps each one: dropping that model from the
    # metadata must surface as a table the database has and the models lack.
    # A child cannot outlive the parent its composite FK resolves against, so
    # dropping dora_organizations drops both children from the copy too.
    remaining = [
        model for model in _MODELS
        if model is not missing and not _references(model, missing.__tablename__)
    ]
    diffs = _flattened(_scoped_metadata_diff(_copy_of_the_models(*remaining)))
    removed = [d for d in diffs if d[0] == "remove_table" and d[1].name == missing.__tablename__]
    assert removed, f"the scoped comparison did not notice {missing.__tablename__} missing"


@pytest.mark.integration
def test_server_default_comparison_is_actually_in_effect(db_connection):
    # The flag is what makes a dropped gen_random_uuid() or true default
    # visible. Prove it is honoured, not merely spelled: a copy of the models
    # with one default changed must produce exactly that modify_default.
    mutated = _copy_of_the_models(*_MODELS)
    mutated.tables["dora_organizations"].c.is_active.server_default = DefaultClause(text("false"))
    diffs = _flattened(_scoped_metadata_diff(mutated))
    assert [(d[0], d[2], d[3]) for d in diffs] == [("modify_default", "dora_organizations", "is_active")]


# ── What the comparison cannot see, verified from the catalogs ──────────────


@pytest.mark.integration
def test_check_constraints_are_exactly_what_migration_025_wrote(db_connection):
    found: dict[str, str] = {}
    for table in _SCOPE:
        found.update(_catalog_definitions(db_connection, table, "c"))
    assert found == _EXPECTED_CHECKS
    assert _model_check_texts() == _EXPECTED_MODEL_CHECKS


@pytest.mark.integration
def test_foreign_keys_are_exactly_what_migration_025_wrote(db_connection):
    found: dict[str, str] = {}
    for table in _SCOPE:
        found.update(_catalog_definitions(db_connection, table, "f"))
    assert found == _EXPECTED_FOREIGN_KEYS
    assert set(found) == _model_constraint_names(ForeignKeyConstraint)


@pytest.mark.integration
def test_unique_constraints_are_exactly_what_migration_025_wrote(db_connection):
    found: dict[str, str] = {}
    for table in _SCOPE:
        found.update(_catalog_definitions(db_connection, table, "u"))
    assert found == _EXPECTED_UNIQUES
    assert set(found) == _model_constraint_names(UniqueConstraint)


@pytest.mark.integration
def test_the_only_plain_index_is_the_identifier_tenant_org_index(db_connection):
    found: dict[str, str] = {}
    for table in _SCOPE:
        for name, definition in _catalog_indexes(db_connection, table).items():
            if name not in _EXPECTED_UNIQUES:
                found[name] = definition
    assert found == _EXPECTED_PLAIN_INDEXES
    model_indexes = {index.name for index in DORAOrganizationIdentifier.__table__.indexes}
    assert model_indexes == set(_EXPECTED_PLAIN_INDEXES)
