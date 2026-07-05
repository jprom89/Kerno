"""Dev-only seed script that creates a single tenant with known credentials for local testing.
Run once after 'alembic upgrade head'; idempotent and safe to re-run."""

from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2

from config.constants import DEFAULT_REMEDIATION_SLA_DAYS
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

_SELECT_TENANT_ID = "SELECT tenant_id FROM tenants WHERE email = %s"


def main() -> None:
    """Hash the dev password, connect to DATABASE_URL, and upsert the dev tenant row.

    Also seeds the tenant's default remediation routing rule (KER-110) so the
    remediation trigger works out of the box on a fresh dev database.

    Refuses to run unless KERNO_ENV=development (SEC-02): this hardcodes weak
    credentials, so it must never be pointed at a staging or production database.
    """
    env = os.getenv("KERNO_ENV", "")
    if env != "development":
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

    password_hash = hash_password(_DEV_PASSWORD)
    conn = psycopg2.connect(database_url)
    try:
        with conn:
            cursor = conn.cursor()
            cursor.execute(_UPSERT_TENANT, (_DEV_DISPLAY_NAME, _DEV_EMAIL, password_hash))
            cursor.execute(_SELECT_TENANT_ID, (_DEV_EMAIL,))
            tenant_id = cursor.fetchone()[0]
            # FORCE RLS (migration 018): the routing-rule INSERT must run under
            # the tenant's context — even the owner role obeys the policy.
            cursor.execute("SET LOCAL app.current_tenant_id = %s", (str(tenant_id),))
            cursor.execute(
                _SEED_DEFAULT_ROUTING_RULE,
                (_DEV_JIRA_ASSIGNEE, DEFAULT_REMEDIATION_SLA_DAYS, _DEV_EMAIL),
            )
        print(f"Dev tenant seeded: {_DEV_EMAIL} (password set)")
        print(
            f"Default remediation routing rule ensured — assignee: {_DEV_JIRA_ASSIGNEE}, "
            f"SLA: {DEFAULT_REMEDIATION_SLA_DAYS} days"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
