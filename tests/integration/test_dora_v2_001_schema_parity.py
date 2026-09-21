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
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, create_engine

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
WHERE rel.relname = %s AND con.contype = %s
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


def _scoped_metadata_diff() -> list:
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
            return compare_metadata(migration_context, Base.metadata)
    finally:
        engine.dispose()


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
    diffs = _scoped_metadata_diff()
    assert diffs == [], (
        "ORM metadata and migration 025 disagree; autogenerate would propose: "
        + "; ".join(str(diff)[:200] for diff in diffs)
    )


@pytest.mark.integration
def test_the_scope_filter_is_not_hiding_the_three_tables(db_connection):
    # A comparison that never looked at the tables would also report no
    # drift. Prove the scope keeps them: dropping one model from the metadata
    # must surface as a missing table.
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import MetaData

    two_of_three = MetaData()
    for model in (Tenant, DORAOrganization, DORAOrganizationIdentifier):
        model.__table__.to_metadata(two_of_three)
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.connect() as connection:
            migration_context = MigrationContext.configure(
                connection,
                opts={"include_name": _in_scope_reflected, "include_object": _in_scope_object},
            )
            diffs = compare_metadata(migration_context, two_of_three)
    finally:
        engine.dispose()
    removed = [d for d in diffs if d[0] == "remove_table" and d[1].name == "dora_organization_roles"]
    assert removed, "the scoped comparison did not notice a whole table missing from the metadata"


# ── What the comparison cannot see, verified from the catalogs ──────────────


@pytest.mark.integration
def test_check_constraints_are_exactly_what_migration_025_wrote(db_connection):
    found: dict[str, str] = {}
    for table in _SCOPE:
        found.update(_catalog_definitions(db_connection, table, "c"))
    assert found == _EXPECTED_CHECKS
    assert set(found) == _model_constraint_names(CheckConstraint)


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
