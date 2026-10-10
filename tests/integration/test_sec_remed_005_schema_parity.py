"""SEC-REMED-005: the ORM models and migrations 003/010/028 describe the same overrides and recommendations tables.

Runs Alembic's scoped metadata comparison (types and server defaults on) against kerno_test, pins from the catalogs
what it cannot see, and proves the database itself refuses a decision bound across tenants or controls. Run:
pytest tests/integration/test_sec_remed_005_schema_parity.py --require-live-database -m integration
"""

from __future__ import annotations

import os
import uuid

import psycopg2.errors
import pytest
from sqlalchemy import DefaultClause, ForeignKeyConstraint, MetaData, UniqueConstraint, create_engine, text

from src.models import Base
from src.models.override import Override
from src.models.recommendation import Recommendation
from src.models.tenant import Tenant

# Tenant is imported so the metadata can resolve overrides.tenant_id's foreign
# key; the scope hooks exclude it from the comparison.

_SCOPE = frozenset({Override.__tablename__, Recommendation.__tablename__})

_EXPECTED_OVERRIDE_FOREIGN_KEYS = {
    "overrides_tenant_id_fkey": "FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)",
    "fk_overrides_reviewed_recommendation":
        "FOREIGN KEY (tenant_id, recommendation_id, original_control_id) "
        "REFERENCES recommendations(tenant_id, recommendation_id, control_id)",
}
_EXPECTED_RECOMMENDATION_UNIQUES = {
    "uq_recommendations_tenant_recommendation_control": "UNIQUE (tenant_id, recommendation_id, control_id)",
}
_EXPECTED_OVERRIDE_INDEXES = {
    "overrides_tenant_id_idx": "CREATE INDEX overrides_tenant_id_idx ON public.overrides USING btree (tenant_id)",
    "ix_overrides_tenant_recommendation":
        "CREATE INDEX ix_overrides_tenant_recommendation ON public.overrides "
        "USING btree (tenant_id, recommendation_id, created_at)",
}
_EXPECTED_RECOMMENDATION_INDEXES = {
    "idx_recommendations_current":
        "CREATE INDEX idx_recommendations_current ON public.recommendations "
        "USING btree (tenant_id, control_id, is_superseded)",
    "uq_recommendations_tenant_recommendation_control":
        "CREATE UNIQUE INDEX uq_recommendations_tenant_recommendation_control ON public.recommendations "
        "USING btree (tenant_id, recommendation_id, control_id)",
}
_STANDARD_POLICY = (
    "tenant_isolation_policy", "*",
    "(tenant_id = (current_setting('app.current_tenant_id'::text, true))::uuid)", True,
)

_CONSTRAINT_DEFS_SQL = """
SELECT con.conname, pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class rel ON rel.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = rel.relnamespace
WHERE ns.nspname = 'public' AND rel.relname = %s AND con.contype = %s
"""
_INDEX_DEFS_SQL = """
SELECT indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND tablename = %s AND indexname NOT LIKE '%%_pkey'
"""
_COLUMN_SQL = """
SELECT data_type, is_nullable, column_default FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'overrides' AND column_name = 'recommendation_id'
"""

_CONTROL = "c5050000-0000-4000-c000-000000000201"
_OTHER_CONTROL = "c5050000-0000-4000-c000-000000000202"
_REVIEWER = "d5050000-0000-4000-d000-000000000003"


def _in_scope_reflected(name, type_, parent_names) -> bool:
    """Alembic include_name hook: reflect only the two tables in scope."""
    if type_ == "table":
        return name in _SCOPE
    return True


def _in_scope_object(obj, name, type_, reflected, compare_to) -> bool:
    """Alembic include_object hook: compare only objects belonging to the two tables in scope."""
    if type_ == "table":
        return name in _SCOPE
    return True


def _scoped_metadata_diff(metadata: MetaData) -> list:
    """Run compare_metadata for the two tables only, types and server defaults compared, flattened."""
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
            diffs = compare_metadata(migration_context, metadata)
    finally:
        engine.dispose()
    flat = []
    for group in diffs:
        flat.extend(group if isinstance(group, list) else [group])
    return flat


def _copy(*models) -> MetaData:
    """Return a fresh MetaData holding the given models' tables, for mutation tests."""
    copied = MetaData()
    for model in models:
        model.__table__.to_metadata(copied)
    return copied


def _catalog(conn, table: str, sql: str, *params) -> dict[str, str]:
    with conn.transaction():
        return dict(conn.execute(sql, [table, *params]).fetchall())


def _insert_recommendation(conn, tenant_id, control_id: str) -> str:
    recommendation_id = str(uuid.uuid4())
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        conn.execute(
            """INSERT INTO recommendations
               (recommendation_id, tenant_id, control_id, status, confidence_level,
                confidence_score, rationale, evidence_ids, requires_review, input_snapshot)
               VALUES (%s, %s, %s, 'met', 'high', 0.9, 'Seeded.', %s, FALSE, '{}')""",
            [recommendation_id, str(tenant_id), control_id, []],
        )
    return recommendation_id


def _insert_decision(conn, tenant_id, control_id: str, recommendation_id: str | None) -> None:
    with conn.transaction():
        conn.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
        conn.execute(
            """INSERT INTO overrides
               (tenant_id, reviewer_id, reviewer_role, action_type, original_control_id,
                reviewer_confidence_weight, recommendation_id)
               VALUES (%s, %s, 'vciso', 'approve', %s, 1.0, %s)""",
            [str(tenant_id), _REVIEWER, control_id, recommendation_id],
        )


@pytest.fixture
def binding_rows(db_connection, tenant_a_id, tenant_b_id):
    yield
    db_connection.rollback()
    for tenant_id in (tenant_a_id, tenant_b_id):
        with db_connection.transaction():
            db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_id)])
            db_connection.execute(
                "DELETE FROM overrides WHERE original_control_id IN (%s, %s)", [_CONTROL, _OTHER_CONTROL]
            )
            db_connection.execute(
                "DELETE FROM recommendations WHERE control_id IN (%s, %s)", [_CONTROL, _OTHER_CONTROL]
            )


# ── Alembic's comparison, scoped, server defaults on ────────────────────────


@pytest.mark.integration
def test_alembic_finds_no_drift_between_the_models_and_the_live_tables(db_connection):
    diffs = _scoped_metadata_diff(Base.metadata)
    assert diffs == [], "autogenerate would propose: " + "; ".join(str(d)[:200] for d in diffs)


@pytest.mark.integration
def test_the_scope_filter_is_not_hiding_either_table(db_connection):
    diffs = _scoped_metadata_diff(_copy(Tenant))
    assert {d[1].name for d in diffs if d[0] == "remove_table"} == _SCOPE


@pytest.mark.integration
def test_server_default_comparison_is_actually_in_effect(db_connection):
    mutated = _copy(Tenant, Recommendation, Override)
    mutated.tables["recommendations"].c.is_superseded.server_default = DefaultClause(text("true"))
    diffs = _scoped_metadata_diff(mutated)
    assert [(d[0], d[2], d[3]) for d in diffs] == [("modify_default", "recommendations", "is_superseded")]


# ── What the comparison cannot see, from the catalogs ───────────────────────


@pytest.mark.integration
def test_the_binding_column_is_a_nullable_uuid_with_no_default(db_connection):
    with db_connection.transaction():
        assert db_connection.execute(_COLUMN_SQL).fetchone() == ("uuid", "YES", None)


@pytest.mark.integration
def test_foreign_keys_and_the_candidate_key_are_exactly_what_the_migrations_wrote(db_connection):
    assert _catalog(db_connection, "overrides", _CONSTRAINT_DEFS_SQL, "f") == _EXPECTED_OVERRIDE_FOREIGN_KEYS
    assert _catalog(db_connection, "recommendations", _CONSTRAINT_DEFS_SQL, "f") == {}
    assert _catalog(db_connection, "recommendations", _CONSTRAINT_DEFS_SQL, "u") == _EXPECTED_RECOMMENDATION_UNIQUES
    assert _catalog(db_connection, "overrides", _CONSTRAINT_DEFS_SQL, "u") == {}
    model_foreign_keys = {
        c.name for c in Override.__table__.constraints if isinstance(c, ForeignKeyConstraint)
    }
    model_uniques = {c.name for c in Recommendation.__table__.constraints if isinstance(c, UniqueConstraint)}
    assert model_foreign_keys == set(_EXPECTED_OVERRIDE_FOREIGN_KEYS)
    assert model_uniques == set(_EXPECTED_RECOMMENDATION_UNIQUES)


@pytest.mark.integration
def test_every_index_is_exactly_what_the_migrations_wrote(db_connection):
    assert _catalog(db_connection, "overrides", _INDEX_DEFS_SQL) == _EXPECTED_OVERRIDE_INDEXES
    assert _catalog(db_connection, "recommendations", _INDEX_DEFS_SQL) == _EXPECTED_RECOMMENDATION_INDEXES


@pytest.mark.integration
@pytest.mark.parametrize("table", sorted(_SCOPE))
def test_rls_stays_enabled_and_forced_with_the_standard_policy(db_connection, table):
    with db_connection.transaction():
        enabled, forced = db_connection.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", [table]
        ).fetchone()
        policies = db_connection.execute(
            "SELECT polname, polcmd, pg_get_expr(polqual, polrelid), polwithcheck IS NULL "
            "FROM pg_policy WHERE polrelid = %s::regclass",
            [table],
        ).fetchall()
    assert (enabled, forced) == (True, True)
    assert policies == [_STANDARD_POLICY]


# ── The database refuses a binding across tenants or controls ───────────────


@pytest.mark.integration
def test_the_database_refuses_a_decision_bound_to_another_tenants_recommendation(
    db_connection, tenant_a_id, tenant_b_id, binding_rows
):
    tenant_b_recommendation = _insert_recommendation(db_connection, tenant_b_id, _CONTROL)

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _insert_decision(db_connection, tenant_a_id, _CONTROL, tenant_b_recommendation)


@pytest.mark.integration
def test_the_database_refuses_a_decision_bound_to_another_controls_recommendation(
    db_connection, tenant_a_id, binding_rows
):
    other_control_recommendation = _insert_recommendation(db_connection, tenant_a_id, _OTHER_CONTROL)

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _insert_decision(db_connection, tenant_a_id, _CONTROL, other_control_recommendation)


@pytest.mark.integration
def test_the_database_accepts_a_matching_binding_and_a_legacy_unbound_decision(
    db_connection, tenant_a_id, binding_rows
):
    recommendation_id = _insert_recommendation(db_connection, tenant_a_id, _CONTROL)

    _insert_decision(db_connection, tenant_a_id, _CONTROL, recommendation_id)
    _insert_decision(db_connection, tenant_a_id, _CONTROL, None)

    with db_connection.transaction():
        db_connection.execute("SET LOCAL app.current_tenant_id = %s", [str(tenant_a_id)])
        rows = db_connection.execute(
            "SELECT recommendation_id FROM overrides WHERE original_control_id = %s", [_CONTROL]
        ).fetchall()
    bindings = {str(row[0]) if row[0] is not None else None for row in rows}
    assert bindings == {recommendation_id, None}
