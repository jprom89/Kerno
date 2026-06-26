"""Unit tests for the constants defined in src/models/compliance_control.py.

Plain-English summary
---------------------
Five tests confirm that all authoritative string constants required by
PROMPT_doc11_nis2_control_mapping.md §3.4–3.6 are importable from the model
module and carry the correct values. No database connection is required.

Tests cover: framework constants, category constants, relationship constants,
entity-type constants, and uniqueness of all constant values across the entire
set (a duplicate would silently break the Trust Center Coverage Matrix).

How to run
----------
    pytest tests/unit/models/test_compliance_control.py -v
"""

from __future__ import annotations

import src.models.compliance_control as cc


def test_framework_constants_defined() -> None:
    """All four framework string constants must be importable with expected values."""
    assert cc.FRAMEWORK_NIS2 == "nis2"
    assert cc.FRAMEWORK_DORA == "dora"
    assert cc.FRAMEWORK_CRA == "cra"
    assert cc.FRAMEWORK_AI_ACT == "ai_act"


def test_category_constants_defined() -> None:
    """All seven CATEGORY_ constants must be importable with expected values."""
    assert cc.CATEGORY_GOVERNANCE == "governance"
    assert cc.CATEGORY_RISK_MANAGEMENT == "risk_management"
    assert cc.CATEGORY_INCIDENT_HANDLING == "incident_handling"
    assert cc.CATEGORY_SUPPLY_CHAIN == "supply_chain"
    assert cc.CATEGORY_VULNERABILITY == "vulnerability"
    assert cc.CATEGORY_AI_OVERSIGHT == "ai_oversight"
    assert cc.CATEGORY_OPERATIONAL_RESILIENCE == "operational_resilience"


def test_relationship_constants_defined() -> None:
    """All three RELATIONSHIP_ constants must be importable with expected values."""
    assert cc.RELATIONSHIP_EQUIVALENT == "equivalent"
    assert cc.RELATIONSHIP_PARTIAL == "partial"
    assert cc.RELATIONSHIP_RELATED == "related"


def test_entity_type_constants_defined() -> None:
    """Both ENTITY_ constants must be importable with expected values."""
    assert cc.ENTITY_ESSENTIAL == "essential"
    assert cc.ENTITY_IMPORTANT == "important"


def test_no_duplicate_constant_values() -> None:
    """Every constant value must be a unique string across the entire module.

    A duplicate value would mean two different concepts share the same database
    string, which would silently corrupt filtering and the Trust Center display.
    Checks all FRAMEWORK_, CATEGORY_, RELATIONSHIP_, and ENTITY_ constants.
    """
    all_values = [
        cc.FRAMEWORK_NIS2,
        cc.FRAMEWORK_DORA,
        cc.FRAMEWORK_CRA,
        cc.FRAMEWORK_AI_ACT,
        cc.CATEGORY_GOVERNANCE,
        cc.CATEGORY_RISK_MANAGEMENT,
        cc.CATEGORY_INCIDENT_HANDLING,
        cc.CATEGORY_SUPPLY_CHAIN,
        cc.CATEGORY_VULNERABILITY,
        cc.CATEGORY_AI_OVERSIGHT,
        cc.CATEGORY_OPERATIONAL_RESILIENCE,
        cc.ENTITY_ESSENTIAL,
        cc.ENTITY_IMPORTANT,
        cc.RELATIONSHIP_EQUIVALENT,
        cc.RELATIONSHIP_PARTIAL,
        cc.RELATIONSHIP_RELATED,
    ]
    assert len(all_values) == len(set(all_values)), (
        "Duplicate constant values detected: "
        + str([v for v in all_values if all_values.count(v) > 1])
    )
