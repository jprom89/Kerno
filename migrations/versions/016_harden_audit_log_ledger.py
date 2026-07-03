"""Harden audit_log into a tamper-evident, append-only, hash-chained ledger (KER-107).

Plain-English summary
---------------------
The audit_log table becomes the single authoritative audit trail. This migration
alters it in place (the table is never dropped, existing rows are preserved):

  1. Renames override-specific columns to canonical audit names:
     reviewer_id -> actor_id (now nullable: NULL = system-generated event),
     reviewer_role -> actor_role, original_control_id -> control_id (now
     nullable: not every event targets a control), "timestamp" -> created_at.
  2. Adds the generic event columns: object_type, object_id, before_state,
     after_state (JSONB), and the hash-chain columns previous_hash, entry_hash,
     plus sequence_number (BIGSERIAL) as the canonical walk order.
  3. Folds the override-specific columns into the generic ones
     (override_id -> object_id, corrected_control_id / justification_text ->
     after_state) and then drops them.
  4. Backfills a real hash chain over all existing rows, per tenant, using the
     same canonical serialization the application uses — so the entire table is
     verifiable from genesis, with no unhashed "pre-ledger" rows.
  5. Creates the auditor-view indexes (by control, by actor, by time range).
  6. Adds a UNIQUE (tenant_id, previous_hash) constraint: a valid chain is
     linear, so each previous_hash value appears once per tenant — any
     concurrent fork (two entries claiming the same parent) becomes a hard
     database error even under isolation levels where the advisory-lock read
     could see a stale chain head.
  7. Installs BEFORE UPDATE OR DELETE (row-level) and BEFORE TRUNCATE
     (statement-level) triggers that reject every mutation, making the table
     append-only at the database level. TRUNCATE needs its own trigger because
     row-level triggers do not fire for it.

created_at loses its now() default deliberately: the timestamp is part of the
hashed payload, so the application must generate it before insert.

Alembic revision chain:
  Revises: 015_add_embedding_to_context_records (p1q2r3s4)

How to run or test
------------------
Apply:     alembic upgrade q2r3s4t5
Roll back: alembic downgrade p1q2r3s4

The downgrade is lossy by design: non-override ledger entries (system events)
have no representation in the old schema and are deleted, and hash-chain data
is dropped. Downgrading a tamper-evident ledger inherently destroys evidence —
only do it on development databases.

Integration tests: pytest tests/security/test_audit_append_only.py -m integration -v
"""

from alembic import context, op
import sqlalchemy as sa

from config.constants import AUDIT_GENESIS_HASH
from src.services.audit_log import build_canonical_payload, compute_entry_hash

revision = "q2r3s4t5"
down_revision = "p1q2r3s4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Transform audit_log into the hash-chained ledger, preserving existing rows.

    Order matters: columns are renamed and added first, override data is folded
    into the generic columns, the hash chain is backfilled while UPDATE is still
    permitted, NOT NULL is enforced only after the backfill, and the append-only
    triggers are installed last so the backfill itself is not blocked.
    """
    # The backfill reads and rewrites live rows, which offline --sql mode
    # cannot express as a static script. Fail loudly rather than emit a
    # partial migration.
    if context.is_offline_mode():
        raise RuntimeError(
            "Migration 016 backfills the audit hash chain from live data and "
            "must run online — offline --sql mode is not supported for it."
        )
    _rename_columns_to_canonical_names()
    _add_ledger_columns()
    _fold_override_columns_into_generic_columns()
    _backfill_hash_chain()
    _enforce_ledger_not_null_constraints()
    _drop_superseded_override_columns()
    _create_auditor_view_indexes()
    _add_chain_fork_guard()
    _create_append_only_trigger()


def downgrade() -> None:
    """Restore the migration-004 audit_log schema. Lossy — see module docstring.

    Drops the trigger first so the restorative UPDATEs are permitted, deletes
    system-event rows (unrepresentable in the old schema), reconstructs the
    override-specific columns from object_id/after_state, then drops the ledger
    columns and renames everything back.
    """
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_block_truncate ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_block_mutation()")
    op.execute("ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS uq_audit_log_tenant_previous_hash")
    _drop_auditor_view_indexes()
    op.execute("DELETE FROM audit_log WHERE object_type != 'override'")
    _restore_override_columns()
    _drop_ledger_columns()
    _rename_columns_back()


# ---------------------------------------------------------------------------
# Upgrade steps
# ---------------------------------------------------------------------------


def _rename_columns_to_canonical_names() -> None:
    """Rename override-era columns to the canonical audit vocabulary.

    actor_id and control_id also become nullable: NULL actor_id marks a
    system-generated event, NULL control_id an event with no target control.
    created_at loses its database default because the application now generates
    the timestamp (it is part of the hashed payload).
    """
    op.execute("ALTER TABLE audit_log RENAME COLUMN reviewer_id TO actor_id")
    op.execute("ALTER TABLE audit_log RENAME COLUMN reviewer_role TO actor_role")
    op.execute("ALTER TABLE audit_log RENAME COLUMN original_control_id TO control_id")
    op.execute('ALTER TABLE audit_log RENAME COLUMN "timestamp" TO created_at')
    op.execute("ALTER TABLE audit_log ALTER COLUMN actor_id DROP NOT NULL")
    op.execute("ALTER TABLE audit_log ALTER COLUMN control_id DROP NOT NULL")
    op.execute("ALTER TABLE audit_log ALTER COLUMN created_at DROP DEFAULT")


def _add_ledger_columns() -> None:
    """Add the generic event columns and the hash-chain columns.

    Hash columns start nullable so existing rows can be backfilled; NOT NULL is
    enforced afterwards. BIGSERIAL assigns sequence numbers to existing rows
    immediately, establishing the canonical chain-walk order.
    """
    op.execute(
        """
        ALTER TABLE audit_log
            ADD COLUMN object_type   TEXT,
            ADD COLUMN object_id     TEXT,
            ADD COLUMN before_state  JSONB,
            ADD COLUMN after_state   JSONB,
            ADD COLUMN previous_hash TEXT,
            ADD COLUMN entry_hash    TEXT,
            ADD COLUMN sequence_number BIGSERIAL
        """
    )


def _fold_override_columns_into_generic_columns() -> None:
    """Copy override-specific data into the generic event columns.

    Every pre-existing row is an override event, so before_state is the AI's
    recommended control and after_state is the reviewer's decided control plus
    their anonymised justification — the same minimal representation
    override_service writes for new entries.
    """
    op.execute(
        """
        UPDATE audit_log SET
            object_type  = 'override',
            object_id    = override_id::text,
            before_state = jsonb_build_object('control_id', control_id),
            after_state  = jsonb_build_object(
                'control_id', COALESCE(corrected_control_id, control_id),
                'justification_text', justification_text
            )
        """
    )
    op.execute("ALTER TABLE audit_log ALTER COLUMN object_type SET NOT NULL")


def _backfill_hash_chain() -> None:
    """Compute a genuine hash chain over all existing rows, per tenant.

    Uses the application's own canonical serialization and hash function so
    verify_audit_chain() can validate legacy rows identically to new ones. Rows
    are chained in sequence_number order — the same order verification walks.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, actor_id, actor_role, action_type, object_type, "
            "object_id, control_id, before_state, after_state, created_at "
            "FROM audit_log ORDER BY tenant_id, sequence_number"
        )
    ).fetchall()
    latest_hash_by_tenant: dict = {}
    for row in rows:
        tenant_key = str(row.tenant_id)
        previous_hash = latest_hash_by_tenant.get(tenant_key, AUDIT_GENESIS_HASH)
        canonical_payload = build_canonical_payload(
            row.id, row.tenant_id, row.actor_id, row.actor_role, row.action_type,
            row.object_type, row.object_id, row.control_id,
            row.before_state, row.after_state, row.created_at,
        )
        entry_hash = compute_entry_hash(previous_hash, canonical_payload)
        bind.execute(
            sa.text(
                "UPDATE audit_log SET previous_hash = :previous_hash, "
                "entry_hash = :entry_hash WHERE id = :entry_id"
            ),
            {"previous_hash": previous_hash, "entry_hash": entry_hash, "entry_id": row.id},
        )
        latest_hash_by_tenant[tenant_key] = entry_hash


def _enforce_ledger_not_null_constraints() -> None:
    """Make the hash columns mandatory now that every row has been backfilled."""
    op.execute("ALTER TABLE audit_log ALTER COLUMN previous_hash SET NOT NULL")
    op.execute("ALTER TABLE audit_log ALTER COLUMN entry_hash SET NOT NULL")


def _drop_superseded_override_columns() -> None:
    """Drop the override-specific columns whose data now lives in the generic ones.

    Dropping override_id also removes its foreign key to overrides and its
    index — the generic ledger must not require a parent override row.
    """
    op.execute(
        """
        ALTER TABLE audit_log
            DROP COLUMN override_id,
            DROP COLUMN corrected_control_id,
            DROP COLUMN justification_text
        """
    )


def _create_auditor_view_indexes() -> None:
    """Create the indexes behind the three auditor query patterns and chain walks."""
    op.execute("CREATE INDEX ix_audit_log_tenant_sequence ON audit_log (tenant_id, sequence_number)")
    op.execute("CREATE INDEX ix_audit_log_tenant_control ON audit_log (tenant_id, control_id)")
    op.execute("CREATE INDEX ix_audit_log_tenant_actor ON audit_log (tenant_id, actor_id)")
    op.execute("CREATE INDEX ix_audit_log_tenant_created ON audit_log (tenant_id, created_at)")


def _add_chain_fork_guard() -> None:
    """Add UNIQUE (tenant_id, previous_hash) so a forked chain cannot be stored.

    A valid chain is linear: each previous_hash value appears exactly once per
    tenant. Two concurrent appends that both read the same chain head would
    write duplicate (tenant_id, previous_hash) pairs — this constraint turns
    that race into a database error even under isolation levels where the
    advisory-lock-then-read sequence could see a stale head.
    """
    op.execute(
        "ALTER TABLE audit_log ADD CONSTRAINT uq_audit_log_tenant_previous_hash "
        "UNIQUE (tenant_id, previous_hash)"
    )


def _create_append_only_trigger() -> None:
    """Install the database-level append-only guards.

    The row-level trigger rejects every UPDATE and DELETE; the statement-level
    trigger rejects TRUNCATE, which row-level triggers do not fire for — without
    it, a full-table wipe would leave an empty (and therefore trivially valid)
    ledger. Table owners can still disable triggers — which is why the hash
    chain exists as the second, tamper-evident layer.
    """
    op.execute(
        """
        CREATE FUNCTION audit_log_block_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_block_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_block_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_block_mutation()
        """
    )


# ---------------------------------------------------------------------------
# Downgrade steps
# ---------------------------------------------------------------------------


def _drop_auditor_view_indexes() -> None:
    """Drop the four auditor-view indexes created by this migration."""
    op.execute("DROP INDEX IF EXISTS ix_audit_log_tenant_sequence")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_tenant_control")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_tenant_actor")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_tenant_created")


def _restore_override_columns() -> None:
    """Reconstruct the migration-004 override columns from the generic ones.

    corrected_control_id is NULL for approve actions (the reviewer changed
    nothing), and otherwise the decided control from after_state. Restoring the
    NOT NULL foreign key requires every referenced override row to still exist.
    """
    op.execute(
        """
        ALTER TABLE audit_log
            ADD COLUMN override_id UUID,
            ADD COLUMN corrected_control_id TEXT,
            ADD COLUMN justification_text TEXT
        """
    )
    op.execute(
        """
        UPDATE audit_log SET
            override_id = object_id::uuid,
            corrected_control_id = CASE
                WHEN action_type = 'approve' THEN NULL
                ELSE after_state->>'control_id'
            END,
            justification_text = after_state->>'justification_text'
        """
    )
    op.execute("ALTER TABLE audit_log ALTER COLUMN override_id SET NOT NULL")
    op.execute(
        "ALTER TABLE audit_log ADD CONSTRAINT audit_log_override_id_fkey "
        "FOREIGN KEY (override_id) REFERENCES overrides(override_id)"
    )
    op.execute("CREATE INDEX ON audit_log (override_id)")


def _drop_ledger_columns() -> None:
    """Drop the ledger columns added by this migration.

    Dropping sequence_number also drops its owned BIGSERIAL sequence.
    """
    op.execute(
        """
        ALTER TABLE audit_log
            DROP COLUMN object_type,
            DROP COLUMN object_id,
            DROP COLUMN before_state,
            DROP COLUMN after_state,
            DROP COLUMN previous_hash,
            DROP COLUMN entry_hash,
            DROP COLUMN sequence_number
        """
    )


def _rename_columns_back() -> None:
    """Rename the canonical columns back to their migration-004 names."""
    op.execute("ALTER TABLE audit_log ALTER COLUMN actor_id SET NOT NULL")
    op.execute("ALTER TABLE audit_log ALTER COLUMN control_id SET NOT NULL")
    op.execute("ALTER TABLE audit_log RENAME COLUMN actor_id TO reviewer_id")
    op.execute("ALTER TABLE audit_log RENAME COLUMN actor_role TO reviewer_role")
    op.execute("ALTER TABLE audit_log RENAME COLUMN control_id TO original_control_id")
    op.execute('ALTER TABLE audit_log RENAME COLUMN created_at TO "timestamp"')
    op.execute('ALTER TABLE audit_log ALTER COLUMN "timestamp" SET DEFAULT now()')
