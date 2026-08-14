"""Make ai_decision_log append-only except for the retention prune.

What:  installs four triggers on ai_decision_log — UPDATE and TRUNCATE are
       rejected outright, DELETE is rejected for any row still inside the
       retention window, and INSERT has its created_at stamped by the server.
Why:   KER-203 gave this table a 180-day retention promise and a nightly prune
       to honour it, but nothing stopped anyone from editing a decision record
       or erasing yesterday's. Retention was a property of the prune job's
       Python code, not a rule the database would enforce (§15 KER-405 #2).
How:   alembic upgrade x9y0z1a2   (roll back: alembic downgrade w8x9y0z1)
       Proven by tests/security/test_ai_decision_log_append_only.py against a
       live database.

Deliberately NOT the same trigger pair as audit_log
---------------------------------------------------
§15 KER-405 #2 asks for "the same trigger pair" that migration 016 put on
audit_log. Taken literally that is a contradiction, and this migration does not
do it. audit_log's pair rejects EVERY delete because the human decision ledger
is kept forever. ai_decision_log is a different object: §13 KER-203 AC-4
requires a nightly job that deletes rows past a 180-day window. Copying the
audit pair would make that prune raise for every tenant on every run and render
AC-4 unimplementable.

So DELETE is guarded by age rather than forbidden. The invariant is "a decision
inside the retention window cannot be erased"; the prune is simply the job that
issues the deletes the trigger already permits. The age check lives in the
trigger and not in a session flag the caller sets, because a flag the deleter
turns on itself enforces nothing — and a buggy prune with a wrong cutoff would
then quietly destroy recent records instead of failing.

Why INSERT is guarded too
-------------------------
created_at was client-settable. Without the stamp, the retention window could be
walked around in two statements: insert a row backdated past the floor, then
delete it — the DELETE guard would see an old row and allow it. Forcing
NEW.created_at = now() closes that, and costs one assignment per insert. It also
means an aged row can only be created by a session that deliberately disables
this trigger, which is what the retention tests do.

What this is and is not
-----------------------
Row and statement triggers reject UPDATE, TRUNCATE, and DELETE of rows inside
the retention window, so accidental and application-path mutation fail closed.
This is NOT tamper-evidence. The application still connects as the table owner
(§17 ticket C2 is held), so it can disable these triggers or drop them, and
unlike audit_log there is no hash chain behind them — §13 KER-203 decision 2
deliberately did not give this table one. Retention pruning of rows older than
AI_DECISION_LOG_RETENTION_DAYS is the only permitted delete. Do not describe
this as tamper-evident, tamper-proof, or unalterable, and do not extend the §15
approved demo claim to cover the AI decision log.

The 180 days is hardcoded below and must match
config.constants.AI_DECISION_LOG_RETENTION_DAYS. Changing the Python constant
without a matching migration desyncs the two: the prune would select rows this
trigger still protects, and because a BEFORE DELETE trigger aborts the whole
statement, one such row fails that tenant's entire prune.
test_sql_retention_window_matches_the_python_constant fails on drift.
"""

from alembic import op

revision = "x9y0z1a2"
down_revision = "w8x9y0z1"
branch_labels = None
depends_on = None

# Must equal config.constants.AI_DECISION_LOG_RETENTION_DAYS. Guarded by a test
# rather than imported: a migration describes the schema at a point in time and
# must not change meaning when a constant is later edited.
RETENTION_WINDOW = "180 days"


def upgrade() -> None:
    """Install the mutation guard, the server-side timestamp, and the four triggers."""
    _create_block_function()
    _create_timestamp_function()
    _create_triggers()


def downgrade() -> None:
    """Remove the four triggers and both functions, returning retention to convention.

    Triggers are dropped before their functions: PostgreSQL refuses to drop a
    function a trigger still references, and IF EXISTS does not suppress a
    dependency error. Nothing here touches audit_log_block_mutation() — audit_log
    owns that function and migration 016 drops it.
    """
    op.execute("DROP TRIGGER IF EXISTS ai_decision_log_no_truncate ON ai_decision_log")
    op.execute("DROP TRIGGER IF EXISTS ai_decision_log_retain_window ON ai_decision_log")
    op.execute("DROP TRIGGER IF EXISTS ai_decision_log_no_update ON ai_decision_log")
    op.execute("DROP TRIGGER IF EXISTS ai_decision_log_server_timestamp ON ai_decision_log")
    op.execute("DROP FUNCTION IF EXISTS ai_decision_log_block_mutation()")
    op.execute("DROP FUNCTION IF EXISTS ai_decision_log_stamp_created_at()")


def _create_block_function() -> None:
    """Create the guard that rejects whichever operation invoked it.

    One function serves the update, delete and truncate triggers; TG_OP names the
    rejected operation. audit_log's equivalent cannot be reused because its
    message hardcodes its own table name.
    """
    op.execute(
        """
        CREATE FUNCTION ai_decision_log_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'ai_decision_log is append-only: % is not permitted within the '
                'retention window', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _create_timestamp_function() -> None:
    """Create the trigger function that stamps created_at server-side on insert."""
    op.execute(
        """
        CREATE FUNCTION ai_decision_log_stamp_created_at() RETURNS trigger AS $$
        BEGIN
            NEW.created_at := now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def _create_triggers() -> None:
    """Install the insert stamp and the update, windowed-delete and truncate guards.

    The delete guard carries a WHEN clause so it fires only for rows still inside
    the window — the prune's rows are past it, so the guard is never entered on
    the prune's hot path. Row-level triggers do not fire for TRUNCATE, hence the
    separate statement-level guard: without it one statement would empty the table.
    """
    op.execute(
        """
        CREATE TRIGGER ai_decision_log_server_timestamp
        BEFORE INSERT ON ai_decision_log
        FOR EACH ROW EXECUTE FUNCTION ai_decision_log_stamp_created_at()
        """
    )
    op.execute(
        """
        CREATE TRIGGER ai_decision_log_no_update
        BEFORE UPDATE ON ai_decision_log
        FOR EACH ROW EXECUTE FUNCTION ai_decision_log_block_mutation()
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER ai_decision_log_retain_window
        BEFORE DELETE ON ai_decision_log
        FOR EACH ROW
        WHEN (OLD.created_at >= now() - interval '{RETENTION_WINDOW}')
        EXECUTE FUNCTION ai_decision_log_block_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER ai_decision_log_no_truncate
        BEFORE TRUNCATE ON ai_decision_log
        FOR EACH STATEMENT EXECUTE FUNCTION ai_decision_log_block_mutation()
        """
    )
