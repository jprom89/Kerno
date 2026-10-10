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

Every decision names the exact recommendation the reviewer saw
(``recommendation_id``, SEC-REMED-005). Before anything is written the service
takes that control's review lock and checks, tenant-scoped, that the
recommendation exists in this tenant, belongs to this control and is still the
control's current one; otherwise the decision is refused and nothing is
written. The id is stored on the override and in its ledger entry, so an
approval of R1 can never be read as an approval of R2. Lock order and the
reasons are recorded in recommendation_service.

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
enforcement, reviewer weighting, and hash-chained audit ledger creation. The
live proof of the binding is tests/integration/test_sec_remed_005_*.py.
"""

from __future__ import annotations

import dataclasses
import uuid

from config.constants import (
    JUNIOR_REVIEWER_WEIGHT,
    RbacRole,
    ReviewerRole,
    SENIOR_REVIEWER_WEIGHT,
)
from src.models.override import Override
from src.services.anonymisation import anonymise
from src.services.audit_log import append_audit_entry
from src.services.recommendation_service import (
    ReviewedRecommendation,
    claim_recommendation_for_review,
)
from src.services.tenant_context import resolve_and_set_tenant_context

# Reviewer roles that carry full (senior) confidence weight.
# Roles absent from this set receive the junior weight.
_SENIOR_ROLES = frozenset({"vciso", "fciso"})

# Bridges the two role vocabularies (KER-202 design decision): a user's RBAC role
# (JWT claim, RbacRole) maps to the override ReviewerRole enum used for confidence
# weighting. The two enums are never merged; this map is the only link between
# them. auditor maps to None — auditors are read-only and are rejected with 403
# before any override is written (see OVERRIDE_CAPABLE_ROLES and the overrides
# router). reviewer_role is therefore always derived from the verified JWT role,
# never accepted from the request body.
REVIEWER_ROLE_MAP: dict[RbacRole, ReviewerRole | None] = {
    RbacRole.VCISO: ReviewerRole.VCISO,                        # senior weight 1.0
    RbacRole.COMPLIANCE_LEAD: ReviewerRole.VCISO,              # senior weight 1.0
    RbacRole.SECURITY_ENGINEER: ReviewerRole.FCISO,           # senior weight 1.0
    RbacRole.PLATFORM_ENGINEER: ReviewerRole.INTERNAL_ADMIN,  # junior weight 0.5
    RbacRole.END_CUSTOMER_ADMIN: ReviewerRole.INTERNAL_ADMIN,  # junior weight 0.5
    RbacRole.AUDITOR: None,                                    # read-only — 403
}

# The RBAC roles permitted to submit an override — every role whose map value is
# not None. Derived from the map so the allow-list and the map never drift.
OVERRIDE_CAPABLE_ROLES: tuple[RbacRole, ...] = tuple(
    role for role, reviewer_role in REVIEWER_ROLE_MAP.items() if reviewer_role is not None
)


def resolve_reviewer_role(rbac_role: str) -> ReviewerRole | None:
    """Return the override ReviewerRole for a user's RBAC role, or None if that role
    may not submit overrides (auditor, or any role not in REVIEWER_ROLE_MAP)."""
    try:
        return REVIEWER_ROLE_MAP[RbacRole(rbac_role)]
    except ValueError:
        return None


@dataclasses.dataclass(frozen=True)
class OverrideInput:
    """The data a caller must supply when submitting a human override.

    Frozen so that neither the service nor any downstream code can mutate the
    input after submission. The ``tenant_id`` field is intentionally absent: the
    service always resolves the tenant from the authenticated session, never from
    caller-supplied input. ``recommendation_id`` defaults to None only so that a
    caller omitting it gets the service's explicit ValueError, not a TypeError.
    """

    reviewer_id: uuid.UUID
    reviewer_role: str
    action_type: str
    original_control_id: str
    corrected_control_id: str | None = None
    justification_text: str | None = None
    recommendation_id: str | None = None


def capture_override(session, conn, override_input: OverrideInput) -> Override:
    """Save a human override and write its audit log entry in one transaction.

    Resolves the tenant from the authenticated session, then claims the reviewed
    recommendation under its control's review lock before writing the override
    record and the audit log entry together. Anonymises the justification text
    before storing it, then reads the database-generated ``created_at`` back onto
    the record. Returns the saved override record so the caller can confirm what
    was stored. Raises ``TenantContextMissingError`` if the session cannot supply
    a valid tenant; ``ValueError`` if input fields fail validation or the
    recommendation belongs to another control; ``EntryNotFoundError`` if the
    recommendation is not this tenant's; ``StaleRecommendationError`` if it has
    been replaced. Every refusal happens before any write. The ``conn``
    parameter must be a raw database connection supporting
    ``conn.execute(sql, params_dict)`` — not a SQLAlchemy Session.
    """
    _validate_override_input(override_input)
    tenant_id = resolve_and_set_tenant_context(session, conn)
    # Canonical form, so a spelling uuid.UUID accepts but PostgreSQL does not
    # (urn:uuid:..., braces) can never reach the database as a 500.
    reviewed = claim_recommendation_for_review(
        conn,
        tenant_id,
        str(uuid.UUID(str(override_input.recommendation_id))),
        override_input.original_control_id,
    )
    confidence_weight = _assign_reviewer_confidence_weight(override_input.reviewer_role)
    override = _build_override_record(tenant_id, override_input, confidence_weight)
    _persist_override(conn, override)
    row = conn.execute(
        "SELECT created_at FROM overrides WHERE override_id = :id",
        {"id": str(override.override_id)},
    ).fetchone()
    override.created_at = row[0]
    _record_override_audit_entry(conn, override, reviewed)
    return override


def _validate_override_input(override_input: OverrideInput) -> None:
    """Reject override inputs that are structurally invalid before touching the DB.

    Checks that required fields are present and that action-specific constraints
    hold (e.g. an edit or reject must name a corrected control and say why), and
    that the decision names the recommendation it reviewed as a UUID. Raises
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
        # Overturning the machine requires a reason. The dashboard has always
        # made this field mandatory, but the server accepted None, "" and "   "
        # identically, so "why" was a habit of one client rather than a
        # property of the record. Tested on whitespace, not on None: the
        # justification is stored as text, and a blank string is exactly as
        # useless to an auditor as a missing one. Approve is deliberately
        # exempt (§14 KER-303 AC-4) — agreeing with the recommendation adds no
        # information beyond the reviewer's name and the timestamp, both of
        # which are already recorded.
        if not (override_input.justification_text or "").strip():
            raise ValueError(
                "justification_text is required when action_type is "
                f"'{override_input.action_type}'."
            )
    if not override_input.original_control_id:
        raise ValueError("original_control_id must not be empty.")
    # Checked last so the existing messages for inputs with several faults are
    # unchanged.
    _validate_recommendation_id(override_input.recommendation_id)


def _validate_recommendation_id(recommendation_id: str | None) -> None:
    """Reject a decision that does not name, as a UUID, the recommendation the reviewer saw."""
    if not recommendation_id:
        raise ValueError("recommendation_id is required: name the recommendation that was reviewed.")
    try:
        uuid.UUID(str(recommendation_id))
    except ValueError:
        raise ValueError("recommendation_id must be a UUID.") from None


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
    internal identifiers that must not reach the database, and trims surrounding
    whitespace so the stored text is what validation actually judged. Does not
    write to the database — that is the caller's responsibility.
    """
    anonymised_justification = (
        anonymise(override_input.justification_text.strip())
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
        recommendation_id=uuid.UUID(str(override_input.recommendation_id)),
    )


def _record_override_audit_entry(
    conn, override: Override, reviewed: ReviewedRecommendation
) -> None:
    """Append the override's entry to the tamper-evident audit ledger (KER-107).

    Runs on the same connection and transaction as the override INSERT, so the
    override row and its ledger entry commit or roll back together.
    before_state is the recommendation the reviewer saw: its control, id and
    status. after_state is the control the reviewer decided on (unchanged for
    approve), their already-anonymised justification text, and the same
    recommendation id, so the entry alone says which version was decided.
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
        # actor_id is override.reviewer_id — a verified per-user JWT user_id
        # (KER-202); the tenant-principal placeholder attribution is removed.
        before_state={
            "control_id": override.original_control_id,
            "recommendation_id": reviewed.recommendation_id,
            "recommendation_status": reviewed.status,
        },
        after_state={
            "control_id": override.corrected_control_id or override.original_control_id,
            "justification_text": override.justification_text,
            "recommendation_id": reviewed.recommendation_id,
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
             justification_text, recommendation_id)
        VALUES
            (:override_id, :tenant_id, :reviewer_id, :reviewer_role, :action_type,
             :original_control_id, :corrected_control_id, :reviewer_confidence_weight,
             :justification_text, :recommendation_id)
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
            "recommendation_id": str(override.recommendation_id),
        },
    )


