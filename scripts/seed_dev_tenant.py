"""Dev-only seed script: creates the dev tenant, one login user per RBAC role, and the
default remediation routing rule. Run once after 'alembic upgrade head'; idempotent."""

from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2

from config.constants import DEFAULT_REMEDIATION_SLA_DAYS, RbacRole
from src.services.auth_service import hash_password

_DEV_EMAIL = "admin@kerno.local"
_DEV_PASSWORD = "changeme123"
_DEV_DISPLAY_NAME = "Dev Tenant"
_DEV_JIRA_ASSIGNEE = "dev-jira-account-id"

_UPSERT_TENANT = """
INSERT INTO tenants (display_name, email, password_hash, is_active)
VALUES (%s, %s, %s, true)
ON CONFLICT (email) DO UPDATE
  SET password_hash = EXCLUDED.password_hash,
      is_active     = true
"""

_SELECT_TENANT_ID = "SELECT tenant_id FROM tenants WHERE email = %s"

# One active user per RBAC role, for local per-user login testing (KER-202).
# Idempotent: skipped if the (tenant, email) already exists. The users table has
# RLS but not FORCE (migration 019), so the owner role the script connects as can
# insert without setting tenant context.
_INSERT_DEV_USER = """
INSERT INTO users (tenant_id, email, password_hash, role, is_active)
VALUES (%s, %s, %s, %s, true)
ON CONFLICT (tenant_id, email) DO NOTHING
"""

# Default remediation routing rule (KER-110): NULL category = applies to every
# category without a specific rule. Idempotent — skipped if a default exists.
_SEED_DEFAULT_ROUTING_RULE = """
INSERT INTO remediation_routing_rules
    (tenant_id, control_category, assignee_jira_account_id, sla_days)
SELECT t.tenant_id, NULL, %s, %s
FROM tenants t
WHERE t.email = %s
AND NOT EXISTS (
    SELECT 1 FROM remediation_routing_rules r
    WHERE r.tenant_id = t.tenant_id AND r.control_category IS NULL
)
"""


def _seed_dev_users(cursor, tenant_id, password: str) -> list[str]:
    """Insert one active user per RBAC role under the dev tenant; return their emails.

    Each user logs in as {role}@kerno.local with the given password. Idempotent —
    re-running leaves any existing user untouched. (KER-202.)
    """
    emails: list[str] = []
    for role in RbacRole:
        email = f"{role.value}@kerno.local"
        cursor.execute(
            _INSERT_DEV_USER, (str(tenant_id), email, hash_password(password), role.value)
        )
        emails.append(email)
    return emails


def _apply_seed(cursor, dev_password: str | None) -> list[str]:
    """Upsert the dev tenant, its per-role users, and the default routing rule.

    Returns the list of seeded user emails (empty when DEV_SEED_PASSWORD is unset,
    in which case per-user login seeding is skipped). Runs inside the caller's
    transaction.
    """
    cursor.execute(_UPSERT_TENANT, (_DEV_DISPLAY_NAME, _DEV_EMAIL, hash_password(_DEV_PASSWORD)))
    cursor.execute(_SELECT_TENANT_ID, (_DEV_EMAIL,))
    tenant_id = cursor.fetchone()[0]
    seeded_emails = _seed_dev_users(cursor, tenant_id, dev_password) if dev_password else []
    # FORCE RLS (migration 018): the routing-rule INSERT must run under the
    # tenant's context — even the owner role obeys the policy on that table.
    cursor.execute("SET LOCAL app.current_tenant_id = %s", (str(tenant_id),))
    cursor.execute(
        _SEED_DEFAULT_ROUTING_RULE,
        (_DEV_JIRA_ASSIGNEE, DEFAULT_REMEDIATION_SLA_DAYS, _DEV_EMAIL),
    )
    return seeded_emails


def main() -> None:
    """Connect to DATABASE_URL and seed the dev tenant, per-role users, and routing rule.

    Refuses to run unless KERNO_ENV=development (SEC-02): this uses weak dev
    credentials and must never be pointed at a staging or production database.
    Per-role users are seeded only when DEV_SEED_PASSWORD is set.
    """
    if os.getenv("KERNO_ENV", "") != "development":
        print(
            "ERROR: seed_dev_tenant.py refused to run — KERNO_ENV is not "
            "'development'. Set KERNO_ENV=development to run this script locally.",
            file=sys.stderr,
        )
        sys.exit(1)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "ERROR: DATABASE_URL is not set. Copy .env.example to .env and fill it in.",
            file=sys.stderr,
        )
        sys.exit(1)

    dev_password = os.environ.get("DEV_SEED_PASSWORD")
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            seeded_emails = _apply_seed(conn.cursor(), dev_password)
        print(f"Dev tenant seeded: {_DEV_EMAIL} (password set)")
        if seeded_emails:
            print(f"Seeded {len(seeded_emails)} per-role dev users: " + ", ".join(seeded_emails))
        else:
            print(
                "DEV_SEED_PASSWORD not set — skipped per-role user seeding; "
                "set it in .env to enable local per-user login."
            )
        print(
            f"Default remediation routing rule ensured — assignee: {_DEV_JIRA_ASSIGNEE}, "
            f"SLA: {DEFAULT_REMEDIATION_SLA_DAYS} days"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
