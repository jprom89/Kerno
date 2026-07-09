"""Create the users table with Row-Level Security (KER-202).

Plain-English summary
---------------------
Sprint 1 authenticated one email/password per tenant. KER-202 introduces per-user
identity: this migration creates the users table so each person has their own
login, RBAC role, and verified user_id (the actor recorded on overrides and in the
audit ledger).

The table is tenant-scoped with RLS enabled, but is a deliberate exception to the
migration-018 FORCE rule (design decision, KER-202): it does NOT get FORCE ROW
LEVEL SECURITY. Login must look up a user by email *before* any tenant context
exists, and FORCE would block even the owner role (which the app connects as) from
that pre-context lookup — making login impossible (proven: SET row_security=off
errors under FORCE). This mirrors how migration 018 leaves the tenants table
unforced for exactly the same auth-bootstrap reason.

The policy permits reads when no tenant context is set (so login can scan by
email) and restricts to the tenant otherwise. Security note: because FORCE is
absent, the owner role the app connects as bypasses the policy entirely — so the
policy protects only non-owner roles, and the login function MUST filter by email
and verify the password before issuing any JWT. In KER-202 the only query against
users is that login lookup (subsequent requests read identity from the JWT, never
re-query users), and no endpoint lists users per tenant, so no cross-tenant read
path is exposed.

Email is unique per tenant, not globally, so two tenants may each have an
admin@… address.

role stores a config.constants.RbacRole value as TEXT (not a database enum) so the
six-role vocabulary can evolve without an enum migration; it is validated at the
application layer.

Alembic revision chain:
  Revises: 018_force_rls_on_tenant_tables (s4t5u6v7)

How to run or test
------------------
Apply:     alembic upgrade t5u6v7w8
Roll back: alembic downgrade s4t5u6v7
"""

from alembic import op

revision = "t5u6v7w8"
down_revision = "s4t5u6v7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the users table, its unique constraint and index, then its RLS policy."""
    _create_users_table()
    _apply_rls_to_users()


def downgrade() -> None:
    """Drop the RLS policy, then the users table."""
    op.execute("DROP POLICY IF EXISTS tenant_isolation_policy ON users")
    op.execute("DROP TABLE IF EXISTS users")


def _create_users_table() -> None:
    """Create the users table with a per-tenant unique email and a tenant index.

    role is TEXT (an RbacRole value validated in the application), and password_hash
    stores only the scrypt digest produced by auth_service.hash_password.
    """
    op.execute(
        """
        CREATE TABLE users (
            user_id       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id     UUID        NOT NULL REFERENCES tenants(tenant_id),
            email         TEXT        NOT NULL,
            password_hash TEXT        NOT NULL,
            role          TEXT        NOT NULL,
            is_active     BOOLEAN     NOT NULL DEFAULT true,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_users_tenant_email UNIQUE (tenant_id, email)
        )
        """
    )
    op.execute("CREATE INDEX ix_users_tenant_id ON users (tenant_id)")


def _apply_rls_to_users() -> None:
    """Enable RLS (but NOT FORCE) on users with a context-optional isolation policy.

    Deliberately no FORCE (see module docstring): login needs a pre-context lookup.
    The policy restricts to the current tenant when app.current_tenant_id is set,
    and permits reads when it is unset or empty so the login scan by email can run.
    NULLIF turns the empty-string setting into NULL so the ::uuid cast never errors.
    (LEARNING_PIPELINE_SPEC.md Section 3.2; KER-202 design decision.)
    """
    op.execute("ALTER TABLE users ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_policy ON users
          USING (
            tenant_id = NULLIF(current_setting('app.current_tenant_id', TRUE), '')::uuid
            OR current_setting('app.current_tenant_id', TRUE) IS NULL
            OR current_setting('app.current_tenant_id', TRUE) = ''
          )
        """
    )
