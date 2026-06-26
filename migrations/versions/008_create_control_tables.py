"""Create the compliance_controls, control_crosswalks, and control_evidence_links tables.

Plain-English summary
---------------------
Document 11 (KER-103) introduces the NIS2 control catalogue — the library of
regulatory obligations the Decision layer reasons over. Three tables are created:

  compliance_controls     — one row per regulatory obligation (NIS2, DORA, CRA,
                            AI Act). No RLS: controls are platform-wide data, not
                            per-tenant. A unique constraint on (framework,
                            control_ref) prevents duplicate imports.

  control_crosswalks      — directional links between controls in different
                            frameworks (e.g. NIS2 Art21 ≈ DORA Art9). One row per
                            ordered pair. No RLS for the same reason.

  control_evidence_links  — stub join table connecting controls to ingested context
                            records (from Document 10). The table is created here
                            so migration 008 is fully reversible. No data is
                            written by Document 11; Document 12 populates the table
                            and applies RLS to it.

Alembic revision chain:
  Revises: 007_create_context_tables (g2h3i4j5)
  Next:    (none — this is currently the head revision)

How to run or test
------------------
Apply:

    alembic upgrade h3i4j5k6

Roll back (drops all three tables in reverse dependency order):

    alembic downgrade g2h3i4j5

Unit tests that exercise the schema logic live in:
    tests/unit/services/test_control_service.py
    tests/unit/models/test_compliance_control.py
"""

from alembic import op

revision = "h3i4j5k6"
down_revision = "g2h3i4j5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the three control tables in dependency order.

    compliance_controls must exist before control_crosswalks (FK on both
    source and target) and before control_evidence_links (FK on control_id).
    context_records (created by migration 007) must exist before
    control_evidence_links (FK on record_id).
    """
    _create_compliance_controls()
    _create_control_crosswalks()
    _create_control_evidence_links()


def downgrade() -> None:
    """Drop all three tables in reverse dependency order.

    control_evidence_links and control_crosswalks both reference
    compliance_controls, so they must be dropped first.
    """
    op.drop_table("control_evidence_links")
    op.drop_table("control_crosswalks")
    op.drop_table("compliance_controls")


def _create_compliance_controls() -> None:
    """Create the compliance_controls table with all columns from Document 11 §3.1.

    entity_types is stored as TEXT[] (PostgreSQL array) so a single row can
    list multiple entity classifications without a separate join table.
    Unique constraint on (framework, control_ref) prevents duplicate imports
    when the seed script or future load jobs run more than once.
    No RLS: controls are global platform data shared across all tenants.
    """
    op.execute(
        """
        CREATE TABLE compliance_controls (
            control_id       UUID         PRIMARY KEY,
            framework        VARCHAR(32)  NOT NULL,
            control_ref      VARCHAR(64)  NOT NULL,
            category         VARCHAR(64)  NOT NULL,
            title            VARCHAR(120) NOT NULL,
            obligation_text  TEXT         NOT NULL,
            entity_types     TEXT[]       NOT NULL,
            is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
            CONSTRAINT uq_compliance_controls_framework_ref
                UNIQUE (framework, control_ref)
        )
        """
    )
    op.execute("CREATE INDEX ON compliance_controls (framework)")
    op.execute("CREATE INDEX ON compliance_controls (category)")


def _create_control_crosswalks() -> None:
    """Create the control_crosswalks table with FK references to compliance_controls.

    Both source_control_id and target_control_id reference compliance_controls
    so the database enforces that both sides of a crosswalk link must exist.
    Unique constraint on (source_control_id, target_control_id) ensures each
    directional pair is recorded at most once.
    No RLS: crosswalks are platform-wide metadata.
    """
    op.execute(
        """
        CREATE TABLE control_crosswalks (
            crosswalk_id       UUID        PRIMARY KEY,
            source_control_id  UUID        NOT NULL
                REFERENCES compliance_controls(control_id),
            target_control_id  UUID        NOT NULL
                REFERENCES compliance_controls(control_id),
            relationship       VARCHAR(32) NOT NULL,
            note               TEXT,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_control_crosswalks_pair
                UNIQUE (source_control_id, target_control_id)
        )
        """
    )
    op.execute("CREATE INDEX ON control_crosswalks (source_control_id)")


def _create_control_evidence_links() -> None:
    """Create the control_evidence_links stub table.

    This table is the many-to-many join between compliance controls and ingested
    context records. It is created here so migration 008 is reversible. Document
    12 writes all data and adds RLS. The FK to context_records relies on
    migration 007 (g2h3i4j5) having created that table first.
    Unique constraint on (control_id, record_id) ensures each pair is linked once.
    """
    op.execute(
        """
        CREATE TABLE control_evidence_links (
            link_id          UUID         PRIMARY KEY,
            control_id       UUID         NOT NULL
                REFERENCES compliance_controls(control_id),
            record_id        UUID         NOT NULL
                REFERENCES context_records(record_id),
            linked_by        VARCHAR(255) NOT NULL,
            linked_at        TIMESTAMPTZ  NOT NULL,
            relevance_score  FLOAT,
            note             TEXT,
            CONSTRAINT uq_control_evidence_links_pair
                UNIQUE (control_id, record_id)
        )
        """
    )
    op.execute("CREATE INDEX ON control_evidence_links (control_id)")
    op.execute("CREATE INDEX ON control_evidence_links (record_id)")
