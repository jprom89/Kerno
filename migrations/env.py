"""Alembic migration environment — connects the migration engine to the live database.

Reads DATABASE_URL from the environment, registers all SQLAlchemy models so
autogenerate can detect schema changes, and runs migrations online or offline."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.models import Base

# Import every model module so their table definitions register with Base.metadata.
# Alembic's autogenerate compares Base.metadata against the live schema; a model
# that is never imported is invisible to it and will appear as a missing table.
import src.models.audit_log  # noqa: F401
import src.models.compliance_control  # noqa: F401
import src.models.control_crosswalk  # noqa: F401
import src.models.control_evidence_link  # noqa: F401
import src.models.dora_register_entry  # noqa: F401
import src.models.dora_reporting_window  # noqa: F401
import src.models.dora_submission_run  # noqa: F401
import src.models.dora_submission_window  # noqa: F401
import src.models.override  # noqa: F401
import src.models.recommendation  # noqa: F401
import src.models.retrieval_bias  # noqa: F401
import src.models.tenant  # noqa: F401

config = context.config

database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL must be set before running Alembic migrations. "
        "Copy .env.example to .env and fill in the connection string."
    )
config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit migration SQL to stdout without opening a database connection.

    Useful for generating a SQL script to review or apply manually. Triggered
    by: alembic upgrade head --sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations directly against a live database connection.

    Uses NullPool so the engine does not retain connections after the migration
    command exits — important when running migrations from a script or CI job.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
