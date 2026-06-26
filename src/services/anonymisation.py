"""Anonymisation pipeline — strips identifying markers before data crosses tenant boundaries.

Plain-English summary
---------------------
Before any piece of security metadata can be used for cross-tenant analytics or
model improvement, Kerno must remove anything that could identify a specific
company's internal environment. This file does that stripping.

Each of the five identifier types defined in LEARNING_PIPELINE_SPEC.md Section
4.2 has a named rule: a regular expression that finds the identifier and a
replacement token that replaces it. The function never changes the original
string — it always produces a new, cleaned copy. It also logs which *type* of
identifier it removed each time, so there is an audit trail of what was stripped
without recording the sensitive value itself.

Legal context: this pipeline is the gate that makes cross-tenant telemetry legal
under GDPR Article 6(1)(f) — Legitimate Interest. Data must not leave the
cleaning stage until every rule has been applied. (LEARNING_PIPELINE_SPEC.md
Section 4.2.)

Note on identifier count: the build instruction (PROMPT_doc8_learning_pipeline.md
File 6) references "six" identifier types but LEARNING_PIPELINE_SPEC.md Section
4.2 defines exactly five. Per the project owner's clarification (2026-06-19),
the word "six" is a copy error; only the five types enumerated in the spec table
are in scope. KER-102's acceptance criterion carries the same error and should be
corrected during backlog grooming.

How to run or test
------------------
Unit tests (no database required):

    pytest tests/unit/services/test_anonymisation.py -v

The test suite has 14 cases covering each identifier type, multi-type inputs,
edge cases (empty string, non-string input), and immutability of the input.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AnonymisationRule:
    """A single identifier type the pipeline knows how to remove.

    Keeps the rule's name, compiled pattern, and replacement token together so
    the rule registry below is readable without cross-referencing several lists.
    The dataclass is frozen so a rule cannot be mutated at runtime.
    """

    name: str
    pattern: re.Pattern
    replacement_token: str


# ---------------------------------------------------------------------------
# The five anonymisation rules from LEARNING_PIPELINE_SPEC.md Section 4.2.
# Ordered so that the most specific patterns run before broader ones: emails
# before hostnames (an email contains a hostname), ARNs before bare cloud
# account IDs. Edit this list — or append to it — to adjust coverage.
# ---------------------------------------------------------------------------

_RULES: list[_AnonymisationRule] = [
    # 1. Developer email addresses — must run before hostname stripping so
    #    that "dev@host.internal" is replaced whole, not in two passes.
    #    Replacement: [INTERNAL_EMAIL]
    _AnonymisationRule(
        name="developer_email",
        pattern=re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
        ),
        replacement_token="[INTERNAL_EMAIL]",
    ),

    # 2. Internal hostnames — names that use private TLDs (.internal, .local,
    #    .corp, .intranet, .lan, .home) typical of corporate infrastructure.
    #    Replacement: [INTERNAL_HOST]
    _AnonymisationRule(
        name="internal_hostname",
        pattern=re.compile(
            r"\b[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
            r"\.(?:internal|local|corp|intranet|lan|home)\b",
            re.IGNORECASE,
        ),
        replacement_token="[INTERNAL_HOST]",
    ),

    # 3. IP address ranges — IPv4 addresses, optionally with CIDR notation.
    #    Replacement: [IP_RANGE]
    _AnonymisationRule(
        name="ip_range",
        pattern=re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?:/\d{1,2})?\b"
        ),
        replacement_token="[IP_RANGE]",
    ),

    # 4. Cloud account identifiers.
    #    AWS ARNs (arn:aws:...) — the clearest unambiguous cloud pattern.
    #    Azure subscription and tenant references (subscriptions/<uuid> /
    #    tenants/<uuid>). GCP project references (projects/<project-id>).
    #    Replacement: [CLOUD_ACCOUNT]
    _AnonymisationRule(
        name="cloud_account_id",
        pattern=re.compile(
            r"(?:"
            r"arn:[a-z0-9\-]+:[a-z0-9\-]+:[a-z0-9\-]*:\d{12}:[^\s]+"   # AWS ARN
            r"|(?:subscriptions|tenants)/[0-9a-f]{8}-[0-9a-f]{4}-"       # Azure sub/tenant
            r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
            r"|projects/[a-z][a-z0-9\-]{4,28}[a-z0-9]"                  # GCP project id
            r")",
            re.IGNORECASE,
        ),
        replacement_token="[CLOUD_ACCOUNT]",
    ),

    # 5. Internal ticket references — patterns like PROJ-123, NIS2-4521,
    #    JIRA-9 etc. that identify internal work items. (LEARNING_PIPELINE_SPEC
    #    Section 4.2: pattern [A-Z]+-[0-9]+.)
    #    Replacement: [INTERNAL_TICKET]
    _AnonymisationRule(
        name="internal_ticket_reference",
        pattern=re.compile(r"\b[A-Z]+-\d+\b"),
        replacement_token="[INTERNAL_TICKET]",
    ),
]


def anonymise(raw_text: str) -> str:
    """Return a cleaned copy of ``raw_text`` with all identifying markers removed.

    Applies every anonymisation rule in sequence. Each substitution produces a
    new string — the original is never modified. Logs the *type* of each
    identifier that was removed (not the value itself) so there is an audit
    trail without recording sensitive data. Raises ``ValueError`` if the input is
    not a string. Safe to call with an empty string (returns it unchanged).
    """
    if not isinstance(raw_text, str):
        raise ValueError(
            f"anonymise() expects a string; received {type(raw_text).__name__}."
        )
    cleaned = raw_text
    for rule in _RULES:
        cleaned = _apply_rule(cleaned, rule)
    return cleaned


def _apply_rule(text: str, rule: _AnonymisationRule) -> str:
    """Apply one anonymisation rule and log every match found.

    Returns the text with all matches replaced by the rule's token. Logs at
    INFO level each time a match is found, recording only the identifier *type*
    — never the matched value — so the log is safe to ship to a central
    log aggregator without leaking tenant data.
    """
    matches = rule.pattern.findall(text)
    if not matches:
        return text
    for _ in matches:
        logger.info(
            "Anonymisation: stripped identifier of type '%s'.",
            rule.name,
        )
    return rule.pattern.sub(rule.replacement_token, text)
