"""Add Trust Center fields to tenants — public visibility flag and URL slug (KER-204).

Alembic revision chain:
  Revises: u6v7w8x9 (020_create_ai_decision_log)
  Next:    (none — this is the head revision)

Plain-English summary
---------------------
The Trust Center (KER-204) gives each customer a public status page at
/trust-center/{tenant_slug}/status. That needs two things on the tenants
table: a URL-safe slug that identifies the company without exposing its
tenant_id, and an opt-in flag (trust_center_public) that defaults to private.

Backfill-before-constrain: tenant_slug is UNIQUE NOT NULL on a table with
existing rows, so the column is added nullable first, every existing row gets
a deterministic slug (display_name lowercased, non-alphanumerics collapsed to
hyphens; the first 8 characters of the tenant_id appended on collision or
empty result), and only then are NOT NULL and UNIQUE applied.

Server default for new rows: existing insert paths (tenant registration,
integration-test seeding, the dev seed script) do not supply a slug, and a
bare NOT NULL column would break every one of them. New rows therefore
default to 'tenant-' plus 8 random hex characters — a valid, unique,
non-identifying placeholder the customer can later replace. The §13 spec text
names only UNIQUE NOT NULL; the default is additive to preserve existing
functionality and is recorded here per §11.

Row-Level Security: unchanged. The tenants table stays ENABLE-without-FORCE
(migration 018's auth-bootstrap exception) — the public slug lookup is
exactly such a pre-context read.

How to run or test
------------------
Apply:      alembic upgrade v7w8x9y0   (or: alembic upgrade head)
Roll back:  alembic downgrade u6v7w8x9
Verified by tests/unit/api/test_trust_center.py and the dev-DB checks in the
§11 review block for this file.
"""

import re

from alembic import op

revision = "v7w8x9y0"
down_revision = "u6v7w8x9"
branch_labels = None
depends_on = None

# How many characters of the tenant_id are appended to de-duplicate a slug.
_SLUG_SUFFIX_LENGTH = 8


def _slugify(display_name: str) -> str:
    """Return the URL-safe form of a display name: lowercase, hyphen-separated.

    Non-alphanumeric runs collapse to single hyphens and leading/trailing
    hyphens are trimmed, so "Dev Tenant" becomes "dev-tenant". May return an
    empty string for names with no alphanumeric characters — the caller
    handles that with the tenant-id suffix.
    """
    return re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")


def _backfill_slugs(bind) -> None:
    """Give every existing tenant row a deterministic, unique slug.

    Slugs derive from display_name; a collision (or an empty slugify result)
    appends the first characters of the tenant_id, which is unique by
    construction. Runs inside the migration's transaction.
    """
    rows = bind.exec_driver_sql(
        "SELECT tenant_id, display_name FROM tenants WHERE tenant_slug IS NULL"
    ).fetchall()
    assigned_slugs: set[str] = set()
    for tenant_id, display_name in rows:
        slug = _slugify(display_name or "")
        if not slug or slug in assigned_slugs:
            slug = f"{slug}-{str(tenant_id)[:_SLUG_SUFFIX_LENGTH]}".strip("-")
        assigned_slugs.add(slug)
        bind.exec_driver_sql(
            "UPDATE tenants SET tenant_slug = %(slug)s WHERE tenant_id = %(tenant_id)s",
            {"slug": slug, "tenant_id": str(tenant_id)},
        )


def upgrade() -> None:
    """Add trust_center_public and tenant_slug, backfilling slugs before constraining.

    Order matters: the flag first (it has a default, so it is safe on a
    populated table), then the slug column nullable, then the backfill, then
    NOT NULL, the unique constraint, and the insert-path default.
    """
    op.execute(
        "ALTER TABLE tenants ADD COLUMN trust_center_public BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute("ALTER TABLE tenants ADD COLUMN tenant_slug VARCHAR")
    _backfill_slugs(op.get_bind())
    op.execute("ALTER TABLE tenants ALTER COLUMN tenant_slug SET NOT NULL")
    op.execute(
        "ALTER TABLE tenants ADD CONSTRAINT uq_tenants_tenant_slug UNIQUE (tenant_slug)"
    )
    # New rows from existing insert paths (registration, test seeding) supply
    # no slug: give them a unique, non-identifying placeholder.
    op.execute(
        "ALTER TABLE tenants ALTER COLUMN tenant_slug "
        "SET DEFAULT 'tenant-' || substr(gen_random_uuid()::text, 1, 8)"
    )


def downgrade() -> None:
    """Remove both Trust Center columns (constraint and default drop with them)."""
    op.execute("ALTER TABLE tenants DROP COLUMN tenant_slug")
    op.execute("ALTER TABLE tenants DROP COLUMN trust_center_public")
