"""Unit tests for src/models/dora_register_entry.py.

Plain-English summary
---------------------
Five tests verify the DORARegisterEntry ORM model and its module-level
constants without a live database. Tests check that criticality and
provider-type constants have the correct string values, that the table
name is correct, that the tenant_id column exists for RLS, and that the
data_types and countries_supported columns are backed by PostgreSQL arrays.

How to run
----------
    pytest tests/unit/models/test_dora_register_entry.py -v
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import ARRAY

from src.models.dora_register_entry import (
    CRITICALITY_CRITICAL,
    CRITICALITY_HIGH,
    CRITICALITY_STANDARD,
    PROVIDER_TYPE_CLOUD,
    PROVIDER_TYPE_MANAGED_SERVICE,
    PROVIDER_TYPE_OTHER,
    PROVIDER_TYPE_SOFTWARE,
    PROVIDER_TYPE_TELECOM,
    DORARegisterEntry,
)


def test_criticality_constants_are_correct() -> None:
    """Criticality level constants match the exact strings specified in §3.2."""
    assert CRITICALITY_CRITICAL == "critical"
    assert CRITICALITY_HIGH == "high"
    assert CRITICALITY_STANDARD == "standard"


def test_provider_type_constants_are_correct() -> None:
    """Provider type constants match the exact strings specified in §3.3."""
    assert PROVIDER_TYPE_CLOUD == "cloud"
    assert PROVIDER_TYPE_SOFTWARE == "software"
    assert PROVIDER_TYPE_MANAGED_SERVICE == "managed_service"
    assert PROVIDER_TYPE_TELECOM == "telecom"
    assert PROVIDER_TYPE_OTHER == "other"


def test_table_name_is_correct() -> None:
    """The ORM model targets the dora_register_entries table."""
    assert DORARegisterEntry.__tablename__ == "dora_register_entries"


def test_tenant_id_column_exists() -> None:
    """The tenant_id column exists and is part of the table definition."""
    columns = {col.name for col in DORARegisterEntry.__table__.columns}
    assert "tenant_id" in columns, "tenant_id column is required for RLS"


def test_array_backed_fields_exist() -> None:
    """data_types and countries_supported are stored as PostgreSQL ARRAY columns."""
    data_types_col = DORARegisterEntry.__table__.columns["data_types"]
    countries_col = DORARegisterEntry.__table__.columns["countries_supported"]
    assert isinstance(data_types_col.type, ARRAY), "data_types must be ARRAY type"
    assert isinstance(countries_col.type, ARRAY), "countries_supported must be ARRAY type"
