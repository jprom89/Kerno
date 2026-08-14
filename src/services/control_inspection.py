"""
control_inspection.py

What: The binary artefact list for each seeded NIS2 control in this operating
 cycle. An artefact either exists and is linked, or it does not.
Why: The chair is not a GRC expert and must not invent "looks good enough."
 Approve a control as met only when every required artefact is linked.
 Missing a listed partial item makes the control partial, not met.
 This is file-complete for the cycle. It is not a BSI finding that the
 company meets §30 or §38.
How: Import inspection_items_for(control_ref). Tests ride
 tests/unit/services/test_meeting_pack_service.py
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class InspectionItem:
    """One artefact the chair can tick without interpreting the law."""

    label: str
    pass_if: str
    fail_if: str
    required_for_met: bool


# Approve as met only when every required_for_met item is linked and matches
# pass_if. If a required item is missing, the control is gap (or partial when
# at least one required item is present and a non-required item is missing).
_INSPECTION: dict[str, tuple[InspectionItem, ...]] = {
    "NIS2-Art20-1": (
        InspectionItem(
            label="Named accountable person",
            pass_if="A document names a person (not a department) as accountable for cybersecurity for this entity.",
            fail_if="No name, or only 'the IT team'.",
            required_for_met=True,
        ),
        InspectionItem(
            label="Dated management approval",
            pass_if="Minutes, a signed policy approval, or a dated GF/board decision that cyber measures for this service were approved.",
            fail_if="Undated draft, or a policy with no approver.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art20-2": (
        InspectionItem(
            label="Director training attendance",
            pass_if="A list of management-body members with a training date on cyber or NIS2.",
            fail_if="A generic staff awareness certificate, or no date.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art21-1": (
        InspectionItem(
            label="Risk list for this service",
            pass_if="A spreadsheet or register of risks that names this service, with an owner on each row.",
            fail_if="A blank template, or a company-wide list that never mentions this service.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art21-2-a": (
        InspectionItem(
            label="Information-security or IT policy",
            pass_if="A policy PDF with an approval date.",
            fail_if="Undated draft, or a marketing one-pager.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art23-1": (
        InspectionItem(
            label="Who notifies which authority",
            pass_if="A playbook or one-pager that names the person/role who notifies, and BSI or the relevant CSIRT.",
            fail_if="'We would figure it out' or no authority named.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art23-4": (
        InspectionItem(
            label="24h and 72h times written down",
            pass_if="The playbook (or a sibling page) literally states 24 hours for early warning and 72 hours for notification.",
            fail_if="'Notify ASAP' with no hours.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art21-2-d": (
        InspectionItem(
            label="Vendors this service depends on",
            pass_if="A list that includes at least production hosting, identity, and DNS/email if used, marked as supporting this service.",
            fail_if="Empty list, or a 200-row accounts-payable dump with no 'critical for this service' mark.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art22-1": (
        InspectionItem(
            label="Coordinated-assessment status note",
            pass_if=(
                "A dated note: either they are in an ENISA/Cooperation Group "
                "assessment (with the name), or they are not and they follow "
                "BSI/ENISA advisories. This article is not 'have a vendor list'."
            ),
            fail_if="A vendor spreadsheet offered as if it were this article.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art21-2-e": (
        InspectionItem(
            label="How vulnerabilities are handled",
            pass_if="A named tool or written procedure (for example Dependabot, a patch policy, a scanner).",
            fail_if="'Developers just update things' with no tool or procedure.",
            required_for_met=True,
        ),
        InspectionItem(
            label="Last scan or pentest date",
            pass_if="A report or ticket with a date of the last vulnerability test.",
            fail_if="No date. Missing this item makes the control partial, not met.",
            required_for_met=False,
        ),
    ),
    "NIS2-Art21-2-j": (
        InspectionItem(
            label="Where production runs",
            pass_if="A named host (AWS/GCP/Azure/other) for this service.",
            fail_if="'In the cloud' with no provider.",
            required_for_met=True,
        ),
        InspectionItem(
            label="MFA on production admin",
            pass_if="A console screenshot or IdP export showing MFA required for admin of this service.",
            fail_if="'We use MFA' with no screenshot or export.",
            required_for_met=True,
        ),
    ),
    "NIS2-Art21-2-b": (
        InspectionItem(
            label="On-call or incident channel",
            pass_if="A rota, PagerDuty/Opsgenie, or a named Slack/phone channel.",
            fail_if="'The developer would notice.'",
            required_for_met=True,
        ),
        InspectionItem(
            label="Incident-handling notes",
            pass_if="A playbook page (may be the same file as Art23) covering how an incident is handled.",
            fail_if="Channel exists but there is no written handling note. Missing this item makes the control partial.",
            required_for_met=False,
        ),
    ),
    "NIS2-Art21-2-c": (
        InspectionItem(
            label="Backup exists",
            pass_if="A console screenshot or vendor page showing backups for this service.",
            fail_if="'We have backups' with no screenshot.",
            required_for_met=True,
        ),
        InspectionItem(
            label="Last restore-test date",
            pass_if="An email, ticket, or log with the date a restore was actually tested.",
            fail_if="Backups with no restore date. Missing this item makes the control partial, not met.",
            required_for_met=False,
        ),
    ),
}


def inspection_items_for(control_ref: str) -> tuple[InspectionItem, ...]:
    """Return the artefact list for this control, or empty if it has none yet."""
    return _INSPECTION.get(control_ref, ())


def approve_rule_for(control_ref: str) -> str:
    """Return the one-line Approve rule the chair reads before clicking."""
    items = inspection_items_for(control_ref)
    if not items:
        return (
            "No artefact list for this ref. Do not Approve. Leave as gap and "
            "ask what document would prove it."
        )
    required = [item.label for item in items if item.required_for_met]
    optional = [item.label for item in items if not item.required_for_met]
    rule = "Approve as met only if linked: " + "; ".join(required) + "."
    if optional:
        rule += " Without " + " / ".join(optional) + " this is partial, never met."
    return rule
