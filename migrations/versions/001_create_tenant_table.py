"""Create the tenants table — the root of every tenant-isolation boundary.

This is migration 001, the first in the chain. Every other table in the schema
references ``tenants.tenant_id`` via a foreign key, so this table must exist
before any other migration can run.

Plain-English summary
---------------------
One row is inserted here per customer company when it registers on the
platform. The ``tenant_id`` UUID that the database generates at that moment
becomes the immutable anchor for the company's data across every other table in
the system. Deactivating a company (``is_active = false``) disables their
access without deleting their data.

Alembic revision chain:
  Revises: (nothing — this is the first migration)
  Next:     002_create_embedding_table_with_rls (a1b2c3d4)

How to run or test
------------------
Apply:

    alembic upgrade 001

Roll back:

    alembic downgrade base

The Tenant ORM model is in src/models/tenant.py. Integration tests that create
and query tenant rows live in tests/integration/test_rag_pipeline_end_to_end.py.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the tenants table with all columns the application requires.

    Uses ``gen_random_uuid()`` as the server-side default for ``tenant_id`` so
    the application never supplies the primary key — the database mints it at
    registration time and it is immutable thereafter (KER-101).
    """
    op.create_table(
        "tenants",
        sa.Column(
            "tenant_id",
            PostgresUUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    """Drop the tenants table.

    All child tables (tenant_embeddings, retrieval_bias, overrides, audit_log)
    must be dropped first — their foreign key constraints will block this
    operation otherwise. Downgrade those migrations before running this one.
    """
    op.drop_table("tenants")
