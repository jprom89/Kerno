"""PILOT-READY-004 (G6) — the Next.js upload proxy's admission policy matches the API's.

What:  reads frontend/lib/evidence-upload-limits.ts and frontend/lib/roles.ts
       as text and compares them with the backend: the per-process admission
       cap and the busy refusal's Retry-After are equal, the roles the proxy
       lets through to send a body are exactly EVIDENCE_CAPABLE_ROLES, and the
       proxy's forwarding deadline outlasts the longest the API may spend on
       one upload (its reception deadline plus its PDF extraction deadline).
Why:   the proxy and the API are configured separately. A different cap or
       Retry-After would make the two refuse differently; a role list wider
       than the API's would receive bodies the API refuses, and a narrower one
       would refuse uploads the API accepts; a forwarding deadline shorter
       than the API's work would answer 504 for an upload the API then stores.
       The size limits are compared by test_evidence_upload_bounds.py.
How:   pytest tests/unit/api/test_evidence_upload_policy_consistency.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

from config.constants import (
    EVIDENCE_UPLOAD_MAX_ACTIVE,
    EVIDENCE_UPLOAD_RECEPTION_DEADLINE_SECONDS,
    EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS,
    PDF_EXTRACTION_DEADLINE_SECONDS,
)
from src.services.evidence_intake import EVIDENCE_CAPABLE_ROLES

_FRONTEND_LIB = Path(__file__).resolve().parents[3] / "frontend" / "lib"
_FRONTEND_LIMITS = _FRONTEND_LIB / "evidence-upload-limits.ts"
_FRONTEND_ROLES = _FRONTEND_LIB / "roles.ts"
_MILLISECONDS_PER_SECOND = 1000
_INTEGER_LITERAL = r"\d+(?:_\d+)*"
_STRING_LITERAL = r'"[^"\\]*"'
_STRING_LIST = rf"\[\s*(?:{_STRING_LITERAL}\s*,\s*)*(?:{_STRING_LITERAL}\s*)?\]"


def _typescript_constant(path: Path, name: str) -> str:
    """Return the source text of an exported TypeScript constant's value, failing if it is not declared."""
    source = path.read_text(encoding="utf-8")
    match = re.search(rf"^export const {name} = ([^;]+);", source, re.MULTILINE)
    assert match, f"{name} is missing from {path.name}"
    return match.group(1).strip()


def _integer(path: Path, name: str) -> int:
    """Return a constant written as one integer literal; anything computed fails rather than being guessed at."""
    expression = _typescript_constant(path, name)
    assert re.fullmatch(_INTEGER_LITERAL, expression), f"{name} is not a plain integer literal: {expression}"
    return int(expression)


def _string_list(path: Path, name: str) -> list[str]:
    """Return a constant written as an array of string literals, in order, duplicates kept."""
    expression = _typescript_constant(path, name)
    assert re.fullmatch(_STRING_LIST, expression), f"{name} is not an array of string literals: {expression}"
    return [literal.strip('"') for literal in re.findall(_STRING_LITERAL, expression)]


def test_the_proxy_admits_as_many_uploads_per_process_as_the_api():
    assert _integer(_FRONTEND_LIMITS, "EVIDENCE_UPLOAD_MAX_ACTIVE_PER_PROCESS") == EVIDENCE_UPLOAD_MAX_ACTIVE


def test_the_proxy_and_the_api_tell_a_refused_caller_the_same_retry_after():
    assert _integer(_FRONTEND_LIMITS, "EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS") == EVIDENCE_UPLOAD_RETRY_AFTER_SECONDS


def test_the_proxy_receives_bodies_only_for_the_roles_the_api_lets_upload():
    proxy_roles = _string_list(_FRONTEND_ROLES, "EVIDENCE_UPLOAD_ROLES")
    assert len(proxy_roles) == len(set(proxy_roles))
    assert set(proxy_roles) == {role.value for role in EVIDENCE_CAPABLE_ROLES}


def test_the_proxy_waits_longer_for_the_api_than_the_api_may_spend_on_one_upload():
    forward_deadline_ms = _integer(_FRONTEND_LIMITS, "EVIDENCE_UPLOAD_FORWARD_DEADLINE_MS")
    api_work_seconds = EVIDENCE_UPLOAD_RECEPTION_DEADLINE_SECONDS + PDF_EXTRACTION_DEADLINE_SECONDS
    assert forward_deadline_ms / _MILLISECONDS_PER_SECOND > api_work_seconds
