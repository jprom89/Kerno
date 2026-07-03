"""Override capture service — records a human correction and writes the audit entry.

Plain-English summary
---------------------
When a compliance engineer tells Kerno that the AI got a control mapping wrong,
two things must happen in the same database transaction:

  1. The override itself is saved (what the human decided, and their confidence weight).
  2. An immutable, hash-chained audit ledger entry is appended via
     src/services/audit_log.py (who did it, when, and what changed — KER-107).

Both writes go into the same transaction so they are always consistent: if the
database rejects the override record, the audit entry is also rolled back, and
vice versa. There is never a state where one exists without the other.

Tenant isolation applies here exactly as everywhere else: the tenant context
must be set before any write, and the tenant identity comes from the
authenticated session — never from the request body.

If the reviewer provides a justification note (``justification_text``), the
text is anonymised before storage — internal hostnames, email addresses, IP
ranges, cloud account identifiers, and ticket references are stripped by
``anonymisation.py`` before the value reaches the database.

The ``conn`` parameter throughout this module must be a raw database connection
that supports ``conn.execute(sql, params_dict)``. It must not be a SQLAlchemy
Session object. This matches the contract used by retrieval_service.py and
nightly_bias_recalculation.py across the rest of the codebase.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/services/test_override_service.py -v

The test suite covers valid overrides, invalid inputs, tenant isolation
enforcement, reviewer weighting, and hash-chained audit ledger creation.
"""

from __future__ import annotations

import dataclasses
import uuid

from config.constants import JUNIOR_REVIEWER_WEIGHT, SENIOR_REVIEWER_WEIGHT
from src.models.override import Override
from src.services.anonymisation import anonymise
from src.services.audit_log import append_audit_entry
from src.services.tenant_context import resolve_and_set_tenant_context

# Reviewer roles that carry full (senior) confidence weight.
# Roles absent from this set receive the junior weight.
_SENIOR_ROLES = frozenset({"vciso", "fciso"})


@dataclasses.dataclass(frozen=True)
class OverrideInput:
    """The data a caller must supply when submitting a human override.

    Frozen so that neither the service nor any downstream code can mutate the
    input after submission. The ``tenant_id`` field is intentionally absent: the
    service always resolves the tenant from the authenticated session, never from
    caller-supplied input.
    """

    reviewer_id: uuid.UUID
    reviewer_role: str
    action_type: str
    original_control_id: str
    corrected_control_id: str | None = None
    justification_text: str | None = None


def capture_override(session, conn, override_input: OverrideInput) -> Override:
    """Save a human override and write its audit log entry in one transaction.

    Resolves the tenant from the authenticated session, validates it, then writes
    the override record and the audit log entry together. Anonymises the
    justification text before storing it, then reads the database-generated
    ``created_at`` back onto the record. Returns the saved override record so the
    caller can confirm what was stored. Raises ``TenantContextMissingError`` if the
    session cannot supply a valid tenant. Raises ``ValueError`` if input fields
    fail validation. The ``conn`` parameter must be a raw database connection
    supporting ``conn.execute(sql, params_dict)`` — not a SQLAlchemy Session.
    """
    _validate_override_input(override_input)
    tenant_id = resolve_and_set_tenant_context(session, conn)
    confidence_weight = _assign_reviewer_confidence_weight(override_input.reviewer_role)
    override = _build_override_record(tenant_id, override_input, confidence_weight)
    _persist_override(conn, override)
    row = conn.execute(
        "SELECT created_at FROM overrides WHERE override_id = :id",
        {"id": str(override.override_id)},
    ).fetchone()
    override.created_at = row[0]
    _record_override_audit_entry(conn, override)
    return override


def _validate_override_input(override_input: OverrideInput) -> None:
    """Reject override inputs that are structurally invalid before touching the DB.

    Checks that required fields are present and that action-specific constraints
    hold (e.g. an edit or reject must name a corrected control). Raises
    ``ValueError`` with a plain-English message on any violation.
    """
    valid_actions = {"approve", "edit", "reject"}
    if override_input.action_type not in valid_actions:
        raise ValueError(
            f"action_type must be one of {sorted(valid_actions)}; "
            f"received '{override_input.action_type}'."
        )
    if override_input.action_type in {"edit", "reject"}:
        if not override_input.corrected_control_id:
            raise ValueError(
                "corrected_control_id is required when action_type is "
                f"'{override_input.action_type}'."
            )
    if not override_input.original_control_id:
        raise ValueError("original_control_id must not be empty.")


def _assign_reviewer_confidence_weight(reviewer_role: str) -> float:
    """Return the numeric confidence weight for a given reviewer role.

    Senior reviewers (vCISO, fCISO) receive the full senior weight; all other
    roles receive the junior weight. Both values come from config/constants.py
    — they are never hard-coded here. (LEARNING_PIPELINE_SPEC.md Section 5.2.)
    """
    if reviewer_role in _SENIOR_ROLES:
        return SENIOR_REVIEWER_WEIGHT
    return JUNIOR_REVIEWER_WEIGHT


def _build_override_record(
    tenant_id: uuid.UUID,
    override_input: OverrideInput,
    confidence_weight: float,
) -> Override:
    """Construct an Override model instance from validated inputs.

    Generates the override_id in Python (not via server_default) so the audit log
    can reference it before the record is committed — avoiding a RETURNING clause
    round-trip. Anonymises justification_text before storing it, stripping any
    internal identifiers that must not reach the database. Does not write to the
    database — that is the caller's responsibility.
    """
    anonymised_justification = (
        anonymise(override_input.justification_text)
        if override_input.justification_text is not None
        else None
    )
    return Override(
        override_id=uuid.uuid4(),
        tenant_id=tenant_id,
        reviewer_id=override_input.reviewer_id,
        reviewer_role=override_input.reviewer_role,
        action_type=override_input.action_type,
        original_control_id=override_input.original_control_id,
        corrected_control_id=override_input.corrected_control_id,
        reviewer_confidence_weight=confidence_weight,
        justification_text=anonymised_justification,
    )


def _record_override_audit_entry(conn, override: Override) -> None:
    """Append the override's entry to the tamper-evident audit ledger (KER-107).

    Runs on the same connection and transaction as the override INSERT, so the
    override row and its ledger entry commit or roll back together. Overrides
    carry no stored pre-decision snapshot, so the minimal before/after
    representation is: before_state = the control the AI recommended,
    after_state = the control the reviewer decided on (unchanged for approve)
    plus their already-anonymised justification text.
    """
    append_audit_entry(
        conn,
        override.tenant_id,
        actor_id=override.reviewer_id,
        actor_role=override.reviewer_role,
        action_type=override.action_type,
        object_type="override",
        object_id=str(override.override_id),
        control_id=override.original_control_id,
        before_state={"control_id": override.original_control_id},
        after_state={
            "control_id": override.corrected_control_id or override.original_control_id,
            "justification_text": override.justification_text,
        },
    )


def _persist_override(conn, override: Override) -> None:
    """Write the override record to the database using a parameterised INSERT.

    Uses ``conn.execute(sql, params)`` directly — not a SQLAlchemy Session — to
    stay consistent with the raw-connection contract used throughout this
    codebase. The override_id is generated in Python before this call so the
    audit log can reference it without a RETURNING clause.
    """
    conn.execute(
        """
        INSERT INTO overrides
            (override_id, tenant_id, reviewer_id, reviewer_role, action_type,
             original_control_id, corrected_control_id, reviewer_confidence_weight,
             justification_text)
        VALUES
            (:override_id, :tenant_id, :reviewer_id, :reviewer_role, :action_type,
             :original_control_id, :corrected_control_id, :reviewer_confidence_weight,
             :justification_text)
        """,
        {
            "override_id": str(override.override_id),
            "tenant_id": str(override.tenant_id),
            "reviewer_id": str(override.reviewer_id),
            "reviewer_role": override.reviewer_role,
            "action_type": override.action_type,
            "original_control_id": override.original_control_id,
            "corrected_control_id": override.corrected_control_id,
            "reviewer_confidence_weight": override.reviewer_confidence_weight,
            "justification_text": override.justification_text,
        },
    )


