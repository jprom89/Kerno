"""Unit tests for the file-complete NIS2 inspection checklist.

What:  every seeded control has a Pass if / Fail if list; Approve is blocked
       when the ref is unknown; restore-test and Art22 cannot be fudged.
Why:   the chair must tick artefacts, not interpret the law.
How:   pytest tests/unit/services/test_control_inspection.py -v
"""

from __future__ import annotations

from src.services.control_inspection import approve_rule_for, inspection_items_for

# The twelve refs in scripts/seed_nis2_controls.py — the default cycle scope.
SEEDED_NIS2_REFS = (
    "NIS2-Art20-1",
    "NIS2-Art20-2",
    "NIS2-Art21-1",
    "NIS2-Art21-2-a",
    "NIS2-Art23-1",
    "NIS2-Art23-4",
    "NIS2-Art21-2-d",
    "NIS2-Art22-1",
    "NIS2-Art21-2-e",
    "NIS2-Art21-2-j",
    "NIS2-Art21-2-b",
    "NIS2-Art21-2-c",
)


def test_every_seeded_ref_has_a_required_artefact() -> None:
    """A control with no required artefact cannot be inspected without guessing."""
    for control_ref in SEEDED_NIS2_REFS:
        items = inspection_items_for(control_ref)
        required = [item for item in items if item.required_for_met]
        assert required, f"{control_ref} has no required artefact"
        for item in items:
            assert item.pass_if
            assert item.fail_if


def test_unknown_ref_blocks_approve() -> None:
    """Do not invent a Pass if for a catalogue row that is not in this cycle."""
    assert inspection_items_for("NIS2-Art99") == ()
    assert "Do not Approve" in approve_rule_for("NIS2-Art99")


def test_backup_without_restore_date_is_partial() -> None:
    """Backups alone are never met. The restore-test date is the second tick."""
    items = inspection_items_for("NIS2-Art21-2-c")
    labels = {item.label: item.required_for_met for item in items}
    assert labels["Backup exists"] is True
    assert labels["Last restore-test date"] is False
    rule = approve_rule_for("NIS2-Art21-2-c")
    assert "Backup exists" in rule
    assert "partial, never met" in rule


def test_art22_is_not_the_vendor_list() -> None:
    """Art22 is ENISA/Cooperation Group status, not Art21-2-d's spreadsheet."""
    items = inspection_items_for("NIS2-Art22-1")
    assert len(items) == 1
    assert "vendor spreadsheet" in items[0].fail_if.lower()
    assert "ENISA" in items[0].pass_if
