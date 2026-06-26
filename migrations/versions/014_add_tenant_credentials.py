"""Add email and password_hash columns to the tenants table.

What:  Adds two nullable columns to tenants: email (VARCHAR UNIQUE) and
       password_hash (VARCHAR). Together they enable credential-based login
       via POST /api/v1/auth/login without creating a new table.
Why:   The Doc 19 dashboard requires tenant login. No existing table stores
       credentials; the tenants table is the natural owner for MVP scope
       (one credential set per tenant organisation). Both columns are nullable
       so existing rows are unaffected until a seed script populates them.
How:   alembic upgrade o0p1q2r3
       alembic downgrade -1  (or: alembic downgrade m8n9o0p1)

To seed a tenant's credentials after running this migration:
    python -c "
    from src.services.auth_service import hash_password
    print(hash_password('your_password_here'))
    "
Then run:
    UPDATE tenants SET email='admin@example.com', password_hash='<output above>'
    WHERE tenant_id='<your-tenant-uuid>';
"""

import sqlalchemy as sa
from alembic import op

revision = "o0p1q2r3"
down_revision = "m8n9o0p1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add email and password_hash columns; enforce email uniqueness."""
    op.add_column("tenants", sa.Column("email", sa.VARCHAR(), nullable=True))
    op.add_column("tenants", sa.Column("password_hash", sa.VARCHAR(), nullable=True))
    op.create_unique_constraint("uq_tenants_email", "tenants", ["email"])


def downgrade() -> None:
    """Remove email uniqueness constraint and drop both credential columns."""
    op.drop_constraint("uq_tenants_email", "tenants", type_="unique")
    op.drop_column("tenants", "password_hash")
    op.drop_column("tenants", "email")
