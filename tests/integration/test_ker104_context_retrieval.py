"""Integration tests for KER-104 — retrieve_similar_records against a live PostgreSQL database.

Three tests verify that retrieve_similar_records returns context_records ordered by cosine
distance, respects the limit parameter, and does not return records belonging to another tenant.
Requires DATABASE_URL pointing to a database with migration 015 applied.

Run with:
    pytest tests/integration/test_ker104_context_retrieval.py -m integration -v
"""

from __future__ import annotations

import uuid

import pytest

from config.constants import EMBEDDING_DIMENSION
from src.services.retrieval_service import retrieve_similar_records

# ---------------------------------------------------------------------------
# Test vectors (1536-dimensional so _is_vector_value detects them correctly)
# ---------------------------------------------------------------------------

# Unit vector along dimension 0 — the query and the "near" record point here.
_VEC_NEAR = [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1)

# Vector in the (0,1) plane — cosine distance from _VEC_NEAR is 1 - 1/√2 ≈ 0.293.
_VEC_MID = [1.0, 1.0] + [0.0] * (EMBEDDING_DIMENSION - 2)

# Unit vector along dimension 1 — perpendicular to _VEC_NEAR, cosine distance = 1.0.
_VEC_FAR = [0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSION - 2)

# Tenant B's vector (along dimension 2) — distinct from all Tenant A vectors.
_VEC_B = [0.0, 0.0, 1.0] + [0.0] * (EMBEDDING_DIMENSION - 3)

_QUERY_VECTOR = _VEC_NEAR

# Fixed record UUIDs — deterministic IDs make failure messages readable.
_RECORD_A_NEAR = str(uuid.UUID("a1000000-0000-4000-a000-000000000001"))
_RECORD_A_MID = str(uuid.UUID("a1000000-0000-4000-a000-000000000002"))
_RECORD_A_FAR = str(uuid.UUID("a1000000-0000-4000-a000-000000000003"))
_RECORD_B = str(uuid.UUID("b1000000-0000-4000-b000-000000000001"))


def _fmt(values: list[float]) -> str:
    return "[" + ",".join(str(v) for v in values) + "]"


def _insert_record(conn, record_id: str, tenant_id: uuid.UUID, title: str, vec: list[float]) -> None:
    conn.execute(
        "INSERT INTO context_records "
        "(record_id, tenant_id, source_system, record_type, title, embedding) "
        "VALUES (%s, %s, 'test', 'policy', %s, %s::vector)",
        [record_id, str(tenant_id), title, _fmt(vec)],
    )


# ---------------------------------------------------------------------------
# Fixture — seeds 3 Tenant A records and 1 Tenant B record; cleans up after.
# ---------------------------------------------------------------------------


@pytest.fixture
def ker104_rows(db_connection, tenant_a_id, tenant_b_id):
    """Seed Tenant A (near/mid/far) and Tenant B (b) context rows with known embeddings."""
    _insert_record(db_connection, _RECORD_A_NEAR, tenant_a_id, "near record", _VEC_NEAR)
    _insert_record(db_connection, _RECORD_A_MID, tenant_a_id, "mid record", _VEC_MID)
    _insert_record(db_connection, _RECORD_A_FAR, tenant_a_id, "far record", _VEC_FAR)
    _insert_record(db_connection, _RECORD_B, tenant_b_id, "tenant b record", _VEC_B)

    yield

    db_connection.execute(
        "DELETE FROM context_records WHERE record_id IN (%s, %s, %s, %s)",
        [_RECORD_A_NEAR, _RECORD_A_MID, _RECORD_A_FAR, _RECORD_B],
    )
    db_connection.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_retrieve_returns_records_ordered_by_cosine_distance(db_connection, tenant_a_id, ker104_rows):
    results = retrieve_similar_records(db_connection, tenant_a_id, _QUERY_VECTOR, limit=3)

    assert len(results) == 3, f"Expected 3 results for Tenant A, got {len(results)}"

    distances = [r["distance"] for r in results]
    assert distances == sorted(distances), "Results must be ordered by distance ascending"

    # Near record: identical to query → cosine distance ≈ 0
    assert results[0]["record_id"] == _RECORD_A_NEAR
    assert results[0]["distance"] == pytest.approx(0.0, abs=1e-6)

    # Far record: perpendicular to query → cosine distance ≈ 1
    assert results[-1]["record_id"] == _RECORD_A_FAR
    assert results[-1]["distance"] == pytest.approx(1.0, abs=1e-4)

    # Mid record: between near and far
    assert results[1]["record_id"] == _RECORD_A_MID
    assert 0.0 < results[1]["distance"] < 1.0


@pytest.mark.integration
def test_retrieve_excludes_other_tenant_records(db_connection, tenant_a_id, ker104_rows):
    results = retrieve_similar_records(db_connection, tenant_a_id, _QUERY_VECTOR, limit=10)

    returned_ids = {r["record_id"] for r in results}
    assert _RECORD_B not in returned_ids, (
        "Tenant B record must not appear in Tenant A's retrieval results"
    )
    assert returned_ids == {_RECORD_A_NEAR, _RECORD_A_MID, _RECORD_A_FAR}


@pytest.mark.integration
def test_retrieve_respects_limit_parameter(db_connection, tenant_a_id, ker104_rows):
    results = retrieve_similar_records(db_connection, tenant_a_id, _QUERY_VECTOR, limit=2)

    assert len(results) == 2, f"Expected 2 results with limit=2, got {len(results)}"
    # The 2 returned rows must be the closest two (near and mid)
    returned_ids = {r["record_id"] for r in results}
    assert _RECORD_A_NEAR in returned_ids
    assert _RECORD_A_MID in returned_ids
    assert _RECORD_A_FAR not in returned_ids
