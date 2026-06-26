"""No-op migration — justification_text was folded into the table creation migrations.

Plain-English summary
---------------------
This migration was originally written to ADD COLUMN justification_text to the
overrides and audit_log tables. It has been replaced by a no-op because the
column is now included in those tables from day one:

  - overrides.justification_text  — created in 003_create_override_table.py
  - audit_log.justification_text  — created in 004_create_audit_log_table.py

Adding a column on day one rather than via a later ALTER TABLE migration is
preferable because:
  1. The column is referenced by the application from the first release; there
     is no window where it is absent.
  2. It removes the risk of a partially-applied migration leaving the table
     in an inconsistent state relative to the ORM model.

This migration file is kept in the Alembic revision chain to preserve the
revision ID ``e5f6a7b8`` that may already appear in applied-migrations records
in existing environments. Removing it would break ``alembic upgrade head`` for
any database that has already applied earlier revisions. The no-op upgrade and
downgrade functions ensure the migration chain remains unbroken with no schema
side effects.

Alembic revision chain:
  Revises: 004_create_audit_log_table (c3d4e5f6)
  Next:    (none — this is currently the head revision)

How to run or test
------------------
Apply (no-op, nothing changes in the database):

    alembic upgrade e5f6a7b8

Roll back (no-op):

    alembic downgrade c3d4e5f6
"""

from alembic import op  # noqa: F401 — required by Alembic even for no-op migrations

revision = "e5f6a7b8"
down_revision = "c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No operation — justification_text already exists in overrides and audit_log.

    See the module docstring for the full explanation of why this migration is a
    no-op rather than the ADD COLUMN it was originally designed to perform.
    """


def downgrade() -> None:
    """No operation — justification_text columns are managed by migrations 003 and 004.

    Dropping the column here would break the table definitions created by
    003_create_override_table.py and 004_create_audit_log_table.py. Those
    migrations own the column lifecycle.
    """
