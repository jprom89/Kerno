"""Drop the dead retrieval_bias_vector column from tenant_embeddings.

Plain-English summary
---------------------
Migration 002 added a ``retrieval_bias_vector`` column to ``tenant_embeddings``
with the intention of storing the per-tenant calibration vector alongside each
embedding row. That design was never completed: no application code ever wrote
into that column, and the retrieval service never read from it in a working way.

Document 9 standardises on the normalised approach (PROMPT_doc9 §3.1): the bias
vector lives in the ``retrieval_bias`` table (one row per tenant) and is passed
to the similarity query as a bound SQL parameter at query time. The column on
``tenant_embeddings`` is therefore dead weight — storing it would require
O(embeddings) writes on every nightly recalculation with no benefit, which is
exactly the write-amplification problem §3.2 explains.

This migration removes the column. The application no longer references it; the
ORM model (``src/models/retrieval_bias.py``) already uses the ``retrieval_bias``
table exclusively.

Alembic revision chain:
  Revises: 003_add_justification_text (no-op) — revision e5f6a7b8
  Next:    (none — this is currently the head revision)

How to run or test
------------------
Apply:

    alembic upgrade f1a2b3c4

Roll back (restores the column; next step in a full rollback would be
``alembic downgrade e5f6a7b8`` through to 002):

    alembic downgrade e5f6a7b8

Integration tests that exercise the live similarity query live in
tests/security/test_tenant_isolation.py (marked @pytest.mark.integration).
"""

from alembic import op

from config.constants import EMBEDDING_DIMENSION

revision = "f1a2b3c4"
down_revision = "e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop the unused retrieval_bias_vector column from tenant_embeddings.

    The column was created by migration 002 but was never written by the
    application. Dropping it removes the denormalisation that conflicted with
    the normalised bias-vector design documented in PROMPT_doc9 §3.1.
    """
    op.drop_column("tenant_embeddings", "retrieval_bias_vector")


def downgrade() -> None:
    """Restore the retrieval_bias_vector column to tenant_embeddings.

    Adds the column back as ``vector(EMBEDDING_DIMENSION)`` to match the type
    migration 002 originally created. The column will be empty (NULL) after
    restoration — no data existed in it before the upgrade — which is safe
    because ``retrieval_bias_vector`` was always nullable in migration 002.
    """
    op.execute(
        f"ALTER TABLE tenant_embeddings "
        f"ADD COLUMN retrieval_bias_vector vector({EMBEDDING_DIMENSION})"
    )
