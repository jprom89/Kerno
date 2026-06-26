"""Unit tests for the anonymisation pipeline (KER-102).

Each of the five identifier types defined in LEARNING_PIPELINE_SPEC.md Section
4.2 must be stripped by ``anonymise()``. Every test below proves exactly one
type, using a sample string that contains a representative match.

Negative tests prove that invalid input fails loudly (CLAUDE.md Section 7.2).
"""

import pytest

from src.services.anonymisation import anonymise


# ---------------------------------------------------------------------------
# One positive test per identifier type (five total)
# ---------------------------------------------------------------------------

def test_anonymise_strips_developer_email():
    """Developer email addresses must be replaced with [INTERNAL_EMAIL]."""
    result = anonymise("Contact alice.smith+tag@example.corp for access.")
    assert "[INTERNAL_EMAIL]" in result
    assert "alice.smith" not in result
    assert "example.corp" not in result


def test_anonymise_strips_internal_hostname():
    """Hostnames on private TLDs (.internal etc.) must become [INTERNAL_HOST]."""
    result = anonymise("Connect to db-primary.internal on port 5432.")
    assert "[INTERNAL_HOST]" in result
    assert "db-primary.internal" not in result


def test_anonymise_strips_ip_range():
    """IPv4 addresses and CIDR ranges must be replaced with [IP_RANGE]."""
    result = anonymise("Allow 10.0.0.0/8 and 192.168.1.42 through the firewall.")
    assert result.count("[IP_RANGE]") == 2
    assert "10.0.0.0" not in result
    assert "192.168.1.42" not in result


def test_anonymise_strips_cloud_account_id_aws_arn():
    """AWS ARNs must be replaced with [CLOUD_ACCOUNT]."""
    result = anonymise(
        "Role arn:aws:iam::123456789012:role/AdminRole was used."
    )
    assert "[CLOUD_ACCOUNT]" in result
    assert "123456789012" not in result


def test_anonymise_strips_cloud_account_id_azure_subscription():
    """Azure subscription references must be replaced with [CLOUD_ACCOUNT]."""
    result = anonymise(
        "Resource under subscriptions/12345678-1234-1234-1234-123456789abc."
    )
    assert "[CLOUD_ACCOUNT]" in result
    assert "12345678-1234-1234-1234-123456789abc" not in result


def test_anonymise_strips_cloud_account_id_gcp_project():
    """GCP project references must be replaced with [CLOUD_ACCOUNT]."""
    result = anonymise("Deployed to projects/my-prod-project-01.")
    assert "[CLOUD_ACCOUNT]" in result
    assert "my-prod-project-01" not in result


def test_anonymise_strips_internal_ticket_reference():
    """Internal ticket references (e.g. JIRA-123) must become [INTERNAL_TICKET].

    The spec pattern is [A-Z]+-[0-9]+: one or more UPPERCASE letters, then a
    hyphen, then digits. Prefixes that contain digits (e.g. NIS2 — the regulation
    name) do NOT match; the spec is intentionally narrow. Use pure-alpha prefixes
    like SEC, PROJ, KER in ticket IDs that should be stripped.
    """
    result = anonymise("Tracked in SEC-4521 and KER-101.")
    assert result.count("[INTERNAL_TICKET]") == 2
    assert "SEC-4521" not in result
    assert "KER-101" not in result


# ---------------------------------------------------------------------------
# Multi-type: a single string containing several identifier types
# ---------------------------------------------------------------------------

def test_anonymise_strips_multiple_types_in_one_string():
    """All identifier types must be stripped when they appear together."""
    mixed = (
        "Admin dev@corp.internal ran PROJ-99 "
        "from 10.1.2.3 on host.local."
    )
    result = anonymise(mixed)
    assert "[INTERNAL_EMAIL]" in result
    assert "[INTERNAL_TICKET]" in result
    assert "[IP_RANGE]" in result
    assert "[INTERNAL_HOST]" in result
    assert "dev@corp.internal" not in result
    assert "PROJ-99" not in result
    assert "10.1.2.3" not in result
    assert "host.local" not in result


# ---------------------------------------------------------------------------
# Immutability: original string must not be modified
# ---------------------------------------------------------------------------

def test_anonymise_does_not_mutate_input():
    """The original string must remain unchanged after anonymise() returns."""
    original = "Contact bob@example.internal."
    original_copy = original[:]
    anonymise(original)
    assert original == original_copy


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_anonymise_empty_string_returns_empty_string():
    """An empty input must produce an empty output without raising."""
    assert anonymise("") == ""


def test_anonymise_clean_string_returns_unchanged():
    """A string with no identifiers must pass through exactly as-is."""
    clean = "This control requires multi-factor authentication."
    assert anonymise(clean) == clean


# ---------------------------------------------------------------------------
# Negative tests — invalid input must fail loudly (CLAUDE.md Section 7.2)
# ---------------------------------------------------------------------------

def test_anonymise_non_string_input_raises_value_error():
    """Passing a non-string must raise ValueError, not silently coerce the input."""
    with pytest.raises(ValueError):
        anonymise(None)


def test_anonymise_integer_input_raises_value_error():
    """Integers must not be silently converted to strings — raise ValueError."""
    with pytest.raises(ValueError):
        anonymise(12345)


def test_anonymise_list_input_raises_value_error():
    """Lists must not be silently joined — raise ValueError."""
    with pytest.raises(ValueError):
        anonymise(["internal text"])
