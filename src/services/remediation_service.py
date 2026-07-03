"""Remediation routing service (KER-110) — turns confirmed gaps into Jira tasks and flags re-review.

trigger_remediation() creates a Jira issue for a gap control using the tenant's routing rule
(category-specific rule first, tenant default as fallback) and records the action in the
KER-107 audit ledger; flag_for_rereview() marks the control for re-review when Jira reports
the issue closed. Run tests with: pytest tests/unit/services/test_remediation_service.py -v
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

from src.models.recommendation import STATUS_GAP
from src.services.audit_log import append_audit_entry
from src.services.coverage_service import CoverageControl, get_coverage_controls
from src.services.jira_client import JiraClient
from src.services.recommendation_service import get_recommendation
from src.services.tenant_context import resolve_and_set_tenant_context

_SELECT_CATEGORY_RULE = """
SELECT rule_id, assignee_jira_account_id, sla_days
FROM remediation_routing_rules
WHERE tenant_id = :tenant_id
AND control_category = :control_category
ORDER BY created_at DESC
LIMIT 1
"""

_SELECT_DEFAULT_RULE = """
SELECT rule_id, assignee_jira_account_id, sla_days
FROM remediation_routing_rules
WHERE tenant_id = :tenant_id
AND control_category IS NULL
ORDER BY created_at DESC
LIMIT 1
"""

_INSERT_TASK = """
INSERT INTO remediation_tasks
    (tenant_id, control_id, jira_issue_key, assignee_jira_account_id, due_date)
VALUES
    (:tenant_id, :control_id, :jira_issue_key, :assignee_jira_account_id, :due_date)
"""

_SELECT_OPEN_TASK = """
SELECT task_id
FROM remediation_tasks
WHERE tenant_id = :tenant_id
AND control_id = :control_id
AND jira_issue_key = :jira_issue_key
AND re_review_flagged_at IS NULL
ORDER BY created_at DESC
LIMIT 1
"""

_UPDATE_TASK_CLOSED = """
UPDATE remediation_tasks
SET closed_at = :closed_at,
    re_review_flagged_at = :re_review_flagged_at
WHERE task_id = :task_id
"""


@dataclasses.dataclass(frozen=True)
class RoutingRule:
    """The routing decision applied to one remediation trigger."""

    rule_id: str
    assignee_jira_account_id: str
    sla_days: int


@dataclasses.dataclass(frozen=True)
class RemediationResult:
    """Return type of trigger_remediation — what was created and for whom."""

    control_id: str
    jira_issue_key: str
    due_date: date
    assignee_jira_account_id: str


@dataclasses.dataclass(frozen=True)
class ReReviewResult:
    """Return type of flag_for_rereview."""

    control_id: str
    jira_issue_key: str
    flagged_for_rereview: bool


def trigger_remediation(conn, session, control_id: str) -> RemediationResult:
    """Create a Jira remediation task for a confirmed gap and record it in the audit ledger.

    Resolves the tenant from the authenticated session, re-derives the control's
    system-of-record status via the KER-109 coverage query (so this decision is
    identical to the dashboard figure), and raises ValueError unless it is a gap.
    Raises ValueError when no routing rule exists, JiraClientError when Jira is
    unreachable or unconfigured, TenantContextMissingError on invalid session.
    """
    tenant_id = resolve_and_set_tenant_context(session, conn)
    control = _find_control(conn, tenant_id, control_id)
    if control.status != STATUS_GAP:
        raise ValueError(
            f"Control {control_id!r} has status '{control.status}' — remediation "
            "can only be triggered for confirmed gaps."
        )
    rule = _find_routing_rule(conn, tenant_id, control.category)
    due_date = datetime.now(timezone.utc).date() + timedelta(days=rule.sla_days)
    # Jira is called before the DB writes: a failure after issue creation leaves
    # an orphaned (visible, harmless) Jira task, whereas the reverse order could
    # record a task row for an issue that never existed — an untruthful trail.
    client = JiraClient()
    jira_issue_key = client.create_issue(
        project_key=client.project_key,
        summary=f"Remediation: {control.control_ref} — {control.title}",
        assignee_account_id=rule.assignee_jira_account_id,
        due_date=due_date,
        description=_build_issue_description(conn, tenant_id, control),
    )
    _insert_task(conn, tenant_id, control_id, jira_issue_key, rule, due_date)
    _record_trigger_audit_entry(conn, tenant_id, control_id, jira_issue_key, rule, due_date)
    return RemediationResult(
        control_id=control_id,
        jira_issue_key=jira_issue_key,
        due_date=due_date,
        assignee_jira_account_id=rule.assignee_jira_account_id,
    )


def flag_for_rereview(conn, session, control_id: str, jira_issue_key: str) -> ReReviewResult:
    """Mark a control for re-review after Jira reports its remediation task closed.

    Validates that an open remediation task exists for this (tenant, control,
    issue key) triple — so a caller cannot flag arbitrary controls — then stamps
    closed_at and re_review_flagged_at and records the closure in the audit
    ledger. Raises ValueError when no matching open task exists.
    """
    tenant_id = resolve_and_set_tenant_context(session, conn)
    row = conn.execute(
        _SELECT_OPEN_TASK,
        {
            "tenant_id": str(tenant_id),
            "control_id": control_id,
            "jira_issue_key": jira_issue_key,
        },
    ).fetchone()
    if row is None:
        raise ValueError(
            f"No open remediation task found for control {control_id!r} "
            f"and Jira issue {jira_issue_key!r}."
        )
    now = datetime.now(timezone.utc)
    conn.execute(
        _UPDATE_TASK_CLOSED,
        {"task_id": str(row[0]), "closed_at": now, "re_review_flagged_at": now},
    )
    append_audit_entry(
        conn,
        tenant_id,
        actor_id=None,
        actor_role="system",
        action_type="remediation_closed",
        object_type="control",
        object_id=control_id,
        control_id=control_id,
        after_state={"jira_issue_key": jira_issue_key, "flagged_for_rereview": True},
    )
    return ReReviewResult(
        control_id=control_id, jira_issue_key=jira_issue_key, flagged_for_rereview=True
    )


def _find_control(conn, tenant_id, control_id: str) -> CoverageControl:
    # Reuses the KER-109 coverage pass so the gap decision here is identical to
    # the dashboard's system-of-record figure — one resolution rule everywhere.
    controls = get_coverage_controls(conn, tenant_id)
    match = next((c for c in controls if c.control_id == control_id), None)
    if match is None:
        raise ValueError(f"Control {control_id!r} not found in the active catalogue.")
    return match


def _find_routing_rule(conn, tenant_id, category: str) -> RoutingRule:
    """Return the category-specific rule, falling back to the tenant default (NULL category)."""
    row = conn.execute(
        _SELECT_CATEGORY_RULE,
        {"tenant_id": str(tenant_id), "control_category": category},
    ).fetchone()
    if row is None:
        row = conn.execute(_SELECT_DEFAULT_RULE, {"tenant_id": str(tenant_id)}).fetchone()
    if row is None:
        raise ValueError(
            f"No remediation routing rule configured for category {category!r} "
            "and no tenant default rule exists."
        )
    return RoutingRule(rule_id=str(row[0]), assignee_jira_account_id=row[1], sla_days=row[2])


def _build_issue_description(conn, tenant_id, control: CoverageControl) -> str:
    recommendation = get_recommendation(conn, str(tenant_id), control.control_id)
    rationale = (
        recommendation.rationale if recommendation is not None
        else "No recommendation on record for this control."
    )
    return (
        f"Control {control.control_ref} ({control.control_id}) in category "
        f"'{control.category}' is a confirmed compliance gap.\n\n"
        f"Latest assessment rationale:\n{rationale}"
    )


def _record_trigger_audit_entry(
    conn, tenant_id, control_id: str, jira_issue_key: str, rule: RoutingRule, due_date: date
) -> None:
    append_audit_entry(
        conn,
        tenant_id,
        # actor_id is unavailable until per-user JWT claims land (see KER-108
        # status note in CLAUDE.md §8); the role records that a human triggered this.
        actor_id=None,
        actor_role="compliance_engineer",
        action_type="remediation_triggered",
        object_type="control",
        object_id=control_id,
        control_id=control_id,
        after_state={
            "jira_issue_key": jira_issue_key,
            "due_date": due_date.isoformat(),
            "assignee_jira_account_id": rule.assignee_jira_account_id,
        },
    )


def _insert_task(
    conn, tenant_id, control_id: str, jira_issue_key: str, rule: RoutingRule, due_date: date
) -> None:
    conn.execute(
        _INSERT_TASK,
        {
            "tenant_id": str(tenant_id),
            "control_id": control_id,
            "jira_issue_key": jira_issue_key,
            "assignee_jira_account_id": rule.assignee_jira_account_id,
            "due_date": due_date,
        },
    )
