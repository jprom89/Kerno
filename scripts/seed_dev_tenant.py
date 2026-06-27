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

from src.services.auth_service import hash_password

_DEV_EMAIL = "admin@kerno.local"
_DEV_PASSWORD = "changeme123"
_DEV_DISPLAY_NAME = "Dev Tenant"

_UPSERT_TENANT = """
INSERT INTO tenants (display_name, email, password_hash, is_active)
VALUES (%s, %s, %s, true)
ON CONFLICT (email) DO UPDATE
  SET password_hash = EXCLUDED.password_hash,
      is_active     = true
"""


def main() -> None:
    """Hash the dev password, connect to DATABASE_URL, and upsert the dev tenant row."""
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
        print(f"Dev tenant seeded — email: {_DEV_EMAIL}  password: {_DEV_PASSWORD}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
