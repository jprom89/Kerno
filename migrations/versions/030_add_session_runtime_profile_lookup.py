"""Add the runtime's own grant-profile lookup, and grant it to the runtime in both profiles.

What:  creates kerno_bootstrap.session_runtime_profile(): a fixed, read-only
       SECURITY DEFINER lookup owned by the owner, with a pinned search_path,
       no argument, EXECUTE revoked from PUBLIC. It returns the profile that
       kerno_bootstrap.runtime_assignment records for session_user — the login
       that authenticated the calling connection — or NULL; nothing else.
       Replaces kerno_bootstrap.apply_runtime_grants with migration 029's own
       definition plus exactly one line, granting EXECUTE on that lookup in both
       profiles. Re-applies the recorded assignment, when one exists, so the
       assigned runtime holds the lookup at once.
Why:   PILOT-READY-003 (issue #20, gate G10). A pilot app must refuse to serve
       unless the database has given its login the pilot grant set, and a full
       app likewise outside development. runtime_assignment stays owner-only:
       the app gets a one-row answer about itself, not the table, the owner's
       URL or a lookup by a role name it could choose.
How:   python scripts/migrate_test_database.py upgrade head          (test database)
       python scripts/migrate_test_database.py downgrade d5e6f7g8
       Tests: pytest tests/unit/models/test_runtime_profile_lookup_migration.py -v

Alembic revision chain:
  Revises: d5e6f7g8 (029_create_runtime_bootstrap_and_grant_mechanism)
  Next:    (none - this is the head revision)

Decisions recorded here because the migration is where they take effect
---------------------------------------------------------------------

The privileged diff is one grant. The new apply_runtime_grants is built from
029's own text, read from that historical file unchanged, with one line added
after the H1 grant in the array both profiles share:
'GRANT EXECUTE ON FUNCTION kerno_bootstrap.session_runtime_profile() TO %I'.
Building it from 029 instead of copying it keeps the two definitions provably
identical apart from that line; the downgrade restores 029's text exactly. 029
itself is not edited. revoke_runtime_grants needs no change: it already
revokes every function in kerno_bootstrap.

session_user, not current_user: inside a SECURITY DEFINER function
current_user is the owner, while session_user is the authenticated login and
cannot be changed without superuser. The function takes no argument, so a
caller cannot ask about another role.

Re-applying the recorded assignment is the documented 029 hook ("later
migrations call the procedure for the role and profile in runtime_assignment,
when a row exists"). It revokes and re-grants that role's set in this
transaction; it creates no role and changes no other role. A recorded role that
is no longer valid (for example set NOLOGIN without revoke-runtime first) makes
the procedure raise and this migration fail closed.

The downgrade drops the lookup, which removes its ACL with it, and restores
029's procedure, leaving the recorded role exactly 029's matrix. It deletes no
role, no data and no assignment.
"""

import importlib.util
import pathlib

from alembic import op

revision = "e6f7g8h9"
down_revision = "d5e6f7g8"
branch_labels = None
depends_on = None

_MIGRATION_029 = pathlib.Path(__file__).with_name("029_create_runtime_bootstrap_and_grant_mechanism.py")

_CREATE_PROFILE_LOOKUP = """
CREATE FUNCTION kerno_bootstrap.session_runtime_profile()
RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
  SELECT a.profile FROM kerno_bootstrap.runtime_assignment AS a
  WHERE a.role_name = session_user
$$;
REVOKE ALL ON FUNCTION kerno_bootstrap.session_runtime_profile() FROM PUBLIC;
"""

_DROP_PROFILE_LOOKUP = "DROP FUNCTION kerno_bootstrap.session_runtime_profile();"

# 029's H1 grant line, in the array both profiles share, and the one line
# this migration adds after it.
_H1_GRANT_LINE = "    'GRANT EXECUTE ON FUNCTION kerno_bootstrap.resolve_login(text, text) TO %I',\n"
_LOOKUP_GRANT_LINE = "    'GRANT EXECUTE ON FUNCTION kerno_bootstrap.session_runtime_profile() TO %I',\n"
_CREATE_APPLY = "CREATE PROCEDURE kerno_bootstrap.apply_runtime_grants("
_REPLACE_APPLY = "CREATE OR REPLACE PROCEDURE kerno_bootstrap.apply_runtime_grants("

_REAPPLY_RECORDED_ASSIGNMENT = """
DO $$
DECLARE
  recorded kerno_bootstrap.runtime_assignment%ROWTYPE;
BEGIN
  SELECT * INTO recorded FROM kerno_bootstrap.runtime_assignment;
  IF FOUND THEN
    CALL kerno_bootstrap.apply_runtime_grants(recorded.role_name, recorded.profile);
  END IF;
END
$$;
"""


def upgrade() -> None:
    """Create the lookup, replace apply_runtime_grants with 029's plus the lookup grant, re-apply any assignment.

    The procedure text is built, and 029's text checked, before any statement
    runs, so an unexpected 029 refuses without executing DDL.
    """
    procedure_with_lookup = apply_procedure_with_lookup_grant()
    op.execute(_CREATE_PROFILE_LOOKUP)
    op.execute(procedure_with_lookup)
    op.execute(_REAPPLY_RECORDED_ASSIGNMENT)


def downgrade() -> None:
    """Restore 029's apply_runtime_grants exactly, then drop the lookup (its grants go with it)."""
    original_procedure = migration_029_apply_procedure_replacing()
    op.execute(original_procedure)
    op.execute(_DROP_PROFILE_LOOKUP)


def migration_029_apply_procedure() -> str:
    """Return 029's CREATE PROCEDURE text for apply_runtime_grants, read from the historical migration unchanged."""
    spec = importlib.util.spec_from_file_location("kerno_migration_029_for_030", _MIGRATION_029)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._CREATE_APPLY_PROCEDURE


def migration_029_apply_procedure_replacing() -> str:
    """029's procedure as CREATE OR REPLACE; raises RuntimeError if 029's text is not the one this was written for."""
    procedure = migration_029_apply_procedure()
    if procedure.count(_CREATE_APPLY) != 1 or procedure.count(_H1_GRANT_LINE) != 1:
        raise RuntimeError("migration 029's apply_runtime_grants text is not the reviewed one; refusing to replace it")
    return procedure.replace(_CREATE_APPLY, _REPLACE_APPLY)


def apply_procedure_with_lookup_grant() -> str:
    """029's procedure as CREATE OR REPLACE with the lookup grant added after the H1 grant, in both profiles."""
    return migration_029_apply_procedure_replacing().replace(_H1_GRANT_LINE, _H1_GRANT_LINE + _LOOKUP_GRANT_LINE)
