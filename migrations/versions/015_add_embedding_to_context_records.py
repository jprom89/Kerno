"""Add embedding column to context_records for pgvector similarity search.

KER-104 requires querying context_records by vector similarity to a query embedding.
Migration 007 created the table without an embedding column; this migration adds it
as nullable so existing rows are unaffected until embeddings are computed.
An HNSW index is omitted here — pgvector recommends creating HNSW indexes after
rows exist, so that step belongs in a future migration once data is loaded.
"""

from alembic import op

from config.constants import EMBEDDING_DIMENSION

revision = "p1q2r3s4"
down_revision = "o0p1q2r3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add embedding vector(EMBEDDING_DIMENSION) NULL to context_records."""
    op.execute(
        f"ALTER TABLE context_records ADD COLUMN embedding vector({EMBEDDING_DIMENSION})"
    )


def downgrade() -> None:
    """Remove the embedding column from context_records."""
    op.execute("ALTER TABLE context_records DROP COLUMN embedding")
