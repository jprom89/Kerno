"""Unit tests for src/services/recommendation_service.py.

Plain-English summary
---------------------
Verifies the recommendation service without a live database (scoring, the
KER-401 hybrid rationale path, the KER-303 open list, and persistence).
A spy connection records every execute() call. get_evidence_for_control is
patched to return pre-built EvidenceResult lists, isolating the scoring and
persistence logic from the evidence retrieval layer. Tests cover: STATUS_MET /
PARTIAL / GAP outputs, confidence level assignment, requires_review flag,
superseding prior recommendations, input_snapshot persistence, rationale and
gaps content, tenant context ordering (SET LOCAL first), TenantContextMissingError
on None tenant, absence of SQLAlchemy Session API calls, and broken links
appearing in gaps text. The SEC-REMED-005 tests pin the statement shapes of
the review binding: the open queue's bound-decision predicate, the per-control
review lock and its position in generation, the tenant-scoped by-id read, and
every outcome of claim_recommendation_for_review. Whether the lock actually
serialises review and replacement is proven against PostgreSQL in
tests/integration/test_sec_remed_005_review_concurrency.py.

How to run
----------
    pytest tests/unit/services/test_recommendation_service.py -v
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from config.constants import MAX_RATIONALE_LENGTH
from src.exceptions import EntryNotFoundError, StaleRecommendationError, TenantContextMissingError
from src.models.recommendation import (
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    STATUS_GAP,
    STATUS_MET,
    STATUS_PARTIAL,
)
from src.services.evidence_service import LINK_STATUS_ACTIVE, LINK_STATUS_BROKEN, EvidenceResult
from src.services.recommendation_service import (
    ReviewedRecommendation,
    ScoringResult,
    claim_recommendation_for_review,
    generate_recommendation,
    get_recommendation,
    get_recommendation_by_id,
    list_open_recommendations,
)

_TENANT_ID = "c0000000-0000-4000-a000-000000000077"
_CONTROL_ID = "NIS2-Art21-1"
_OTHER_CONTROL_ID = "NIS2-Art21-2"
_RECOMMENDATION_ID = "f0000000-0000-4000-f000-000000000001"
_NEWER_RECOMMENDATION_ID = "f0000000-0000-4000-f000-000000000002"
# Spelled out rather than built from the prefix constant, so a change to the
# key format fails here instead of silently agreeing with itself.
_REVIEW_LOCK_KEY = "recommendation-review:c0000000-0000-4000-a000-000000000077:NIS2-Art21-1"
_RECORD_ID = "rec-uuid-001"
_CONTROL_ROW = (_CONTROL_ID, "NIS2-Art21-1", "Risk Management Measures")
_NOW = datetime.now(timezone.utc)

_PATCH_TARGET = "src.services.recommendation_service.get_evidence_for_control"


# ── Test infrastructure ────────────────────────────────────────────────────────


class _NullResult:
    """Simulates a non-SELECT result — fetchone/fetchall return empty."""

    def fetchone(self):
        """Return None."""
        return None

    def fetchall(self) -> list:
        """Return an empty list."""
        return []


class _SelectResult:
    """Simulates a SELECT result returning a fixed list of row tuples."""

    def __init__(self, rows: list) -> None:
        """Store the rows to return."""
        self._rows = rows

    def fetchone(self):
        """Return the first row, or None."""
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        """Return all rows."""
        return self._rows


class _SpyConn:
    """Records execute() calls; raises on SQLAlchemy Session API usage."""

    def __init__(self, responses: list[tuple[str, object]] | None = None) -> None:
        """Initialise with an empty call log and optional response configuration."""
        self.calls: list[tuple[str, object]] = []
        self._responses = responses or []

    def execute(self, sql, params=None) -> object:
        """Record the call and return the first matching configured response."""
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in str(sql):
                return result
        return _NullResult()

    def add(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.add() called — recommendation_service must use conn.execute()")

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError("conn.flush() called — recommendation_service must use conn.execute()")


def _make_evidence(
    relevance_score: float | None = 0.9,
    link_status: str = LINK_STATUS_ACTIVE,
    title: str = "Security Patch",
    source_system: str | None = "jira",
    record_id: str = _RECORD_ID,
) -> EvidenceResult:
    """Return an EvidenceResult with configurable key fields."""
    return EvidenceResult(
        link_id="link-001",
        control_id=_CONTROL_ID,
        record_id=record_id,
        linked_by="system",
        linked_at=_NOW,
        relevance_score=relevance_score,
        note=None,
        link_status=link_status,
        source_system=source_system,
        external_id="JIRA-101",
        record_type="issue",
        title=title,
        body="Body text.",
        fetched_at=_NOW,
        content_hash="abc123",
    )


def _default_spy() -> _SpyConn:
    """Return a spy pre-configured to return control metadata for the test control."""
    return _SpyConn(
        responses=[("FROM compliance_controls", _SelectResult([_CONTROL_ROW]))]
    )


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """Force the template-rationale path in every test unless one installs its own mock.

    Without this, generate_recommendation would construct a real Mistral client
    whenever the developer's .env carries a key, and unit tests would hit the
    network (KER-401).
    """
    def _raise_disabled():
        raise RuntimeError("LLM disabled in unit tests")

    monkeypatch.setattr(
        "src.services.recommendation_service.get_llm_client", _raise_disabled
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_generate_recommendation_met() -> None:
    """High-relevance evidence produces STATUS_MET and CONFIDENCE_HIGH."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_MET
    assert result.confidence_level == CONFIDENCE_HIGH


def test_generate_recommendation_partial() -> None:
    """Medium-relevance evidence produces STATUS_PARTIAL and CONFIDENCE_MEDIUM."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.55)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_PARTIAL
    assert result.confidence_level == CONFIDENCE_MEDIUM


def test_generate_recommendation_gap_no_evidence() -> None:
    """Zero active evidence produces STATUS_GAP."""
    spy = _default_spy()
    with patch(_PATCH_TARGET, return_value=[]):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_GAP


def test_generate_recommendation_gap_low_score() -> None:
    """Evidence present but score below MEDIUM threshold -> STATUS_GAP."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.2)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_GAP


def test_low_confidence_sets_requires_review() -> None:
    """CONFIDENCE_LOW output sets requires_review=True (AC-3)."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.1)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.confidence_level == CONFIDENCE_LOW
    assert result.requires_review is True


def test_high_confidence_clears_requires_review() -> None:
    """CONFIDENCE_HIGH output sets requires_review=False."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.confidence_level == CONFIDENCE_HIGH
    assert result.requires_review is False


def test_prior_recommendation_superseded() -> None:
    """Second generate call issues UPDATE to mark prior row is_superseded=True."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    supersede_calls = [
        sql for sql, _ in spy.calls
        if "is_superseded = TRUE" in str(sql)
    ]
    assert len(supersede_calls) >= 1, "UPDATE to set is_superseded=TRUE must be called"


def test_input_snapshot_persisted() -> None:
    """input_snapshot in INSERT params contains control_id, evidence_count, generated_at."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    insert_calls = [
        (sql, params) for sql, params in spy.calls
        if "INSERT INTO recommendations" in str(sql)
    ]
    assert len(insert_calls) == 1, "Exactly one INSERT expected"
    _, params = insert_calls[0]
    # input_snapshot is serialised for the live driver (KER-401 fix).
    snapshot = json.loads(params["input_snapshot"])
    assert snapshot["control_id"] == _CONTROL_ID
    assert snapshot["evidence_count"] == 1
    assert "generated_at" in snapshot


def test_rationale_non_empty_string() -> None:
    """rationale is a non-empty string under MAX_RATIONALE_LENGTH characters."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert isinstance(result.rationale, str)
    assert len(result.rationale) > 0
    assert len(result.rationale) <= MAX_RATIONALE_LENGTH


def test_gaps_none_when_met() -> None:
    """STATUS_MET recommendation has gaps=None (AC-2)."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_MET
    assert result.gaps is None


def test_gaps_present_when_partial() -> None:
    """STATUS_PARTIAL recommendation has a non-empty gaps string."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.55)]
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.status == STATUS_PARTIAL
    assert isinstance(result.gaps, str)
    assert len(result.gaps) > 0


def test_tenant_context_set_before_query() -> None:
    """SET LOCAL must be the first SQL call — tenant context before any SELECT."""
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert len(spy.calls) > 0, "At least one SQL call expected"
    assert "SET LOCAL" in str(spy.calls[0][0]), "First SQL call must be SET LOCAL"


def test_none_tenant_raises() -> None:
    """tenant_id=None raises TenantContextMissingError before any SQL is issued."""
    spy = _default_spy()
    with pytest.raises(TenantContextMissingError):
        generate_recommendation(spy, None, _CONTROL_ID)
    sql_calls = [s for s, _ in spy.calls if "SET LOCAL" not in str(s)]
    assert len(sql_calls) == 0, "No query SQL must be issued when tenant_id is None"


def test_no_sqlalchemy_session_api() -> None:
    """conn.add() and conn.flush() must never be called by the recommendation service.

    _SpyConn.add() and .flush() raise AssertionError if invoked. A clean return
    from generate_recommendation proves only the raw-connection API was used.
    """
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]
    with patch(_PATCH_TARGET, return_value=evidence):
        generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)


def test_broken_links_noted_in_gaps() -> None:
    """A broken evidence link (LINK_STATUS_BROKEN) is noted in the gaps string."""
    spy = _default_spy()
    broken = _make_evidence(
        relevance_score=None,
        link_status=LINK_STATUS_BROKEN,
        source_system=None,
    )
    with patch(_PATCH_TARGET, return_value=[broken]):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)
    assert result.gaps is not None, "gaps must be set when a broken link is present"
    assert "broken" in result.gaps.lower(), "gaps must mention the broken link"


# ── list_open_recommendations (KER-303) ───────────────────────────────────────


class _RowsResult:
    """Serves configured rows for the open-list SELECT and count queries."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


_OPEN_ROW = (
    "e0000000-0000-4000-e000-000000000001", _CONTROL_ID, "NIS2-21.2a",
    "Risk analysis policy", "governance", "partial", "medium", 0.66,
    "Partial coverage.", ["rec-1", "rec-2"], _NOW,
)


def _open_list_spy(rows: list, total: int) -> _SpyConn:
    return _SpyConn(responses=[
        ("LEFT JOIN compliance_controls", _RowsResult(rows)),
        ("SELECT count(*)", _RowsResult([(total,)])),
    ])


def _single_spaced(sql: str) -> str:
    return " ".join(sql.split())


def test_open_list_uses_the_corrected_predicate() -> None:
    spy = _open_list_spy([], 0)
    list_open_recommendations(spy, _TENANT_ID, page=1, page_size=20)
    page_sql = next(s for s, _ in spy.calls if "LEFT JOIN compliance_controls" in str(s))
    count_sql = next(s for s, _ in spy.calls if "SELECT count(*)" in str(s))
    for sql in (_single_spaced(page_sql), _single_spaced(count_sql)):
        assert "r.is_superseded = FALSE" in sql
        assert "newer.tenant_id = r.tenant_id" in sql
        assert "newer.control_id = r.control_id" in sql
        assert "newer.is_superseded = FALSE" in sql
        assert (
            "(newer.generated_at, newer.recommendation_id) > (r.generated_at, r.recommendation_id)"
            in sql
        )
        assert "o.tenant_id = r.tenant_id" in sql
        assert "o.recommendation_id = r.recommendation_id" in sql
        # An override closes only the recommendation it names: no timestamp
        # race between overrides and recommendations, no match by control.
        assert "o.created_at > r.generated_at" not in sql
        assert "o.created_at" not in sql
        assert "original_control_id" not in sql
    assert "ORDER BY r.generated_at DESC, r.recommendation_id DESC" in _single_spaced(page_sql)


def test_open_list_paginates_and_maps_rows() -> None:
    spy = _open_list_spy([_OPEN_ROW], 41)
    items, total = list_open_recommendations(spy, _TENANT_ID, page=3, page_size=20)
    assert total == 41
    _, params = next((s, p) for s, p in spy.calls if "LEFT JOIN" in str(s))
    assert params["page_size"] == 20
    assert params["page_offset"] == 40  # (page 3 - 1) * 20
    item = items[0]
    assert item.control_ref == "NIS2-21.2a"
    assert item.category == "governance"
    assert item.evidence_count == 2
    assert item.confidence_level == "medium"


def test_open_list_sets_tenant_context_first() -> None:
    spy = _open_list_spy([], 0)
    list_open_recommendations(spy, _TENANT_ID, page=1, page_size=20)
    assert "SET LOCAL" in str(spy.calls[0][0])


def test_open_list_invalid_tenant_raises_before_sql() -> None:
    spy = _open_list_spy([], 0)
    with pytest.raises(TenantContextMissingError):
        list_open_recommendations(spy, None, page=1, page_size=20)
    assert len(spy.calls) == 0


# ── KER-401: hybrid rationale, fallback, and generation records ───────────────


def _mock_rationale_client(payload: dict):
    from unittest.mock import MagicMock

    client = MagicMock()
    choice = MagicMock()
    choice.message.content = json.dumps(payload)
    client.chat.complete.return_value = MagicMock(choices=[choice])
    return client


def test_llm_writes_rationale_but_cannot_alter_the_score(monkeypatch) -> None:
    monkeypatch.setenv("KERNO_LLM_MODEL", "mistral-large-latest")
    client = _mock_rationale_client({
        "rationale": "The Security Patch record demonstrates active remediation.",
        "own_status": "partial",
        "own_confidence": 0.55,
    })
    monkeypatch.setattr(
        "src.services.recommendation_service.get_llm_client", lambda: client
    )
    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.9)]  # scorer says met/high
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)

    # Prose from the LLM; verdict from the scorer — the opinion is recorded
    # but provably does NOT leak into the persisted decision (KER-401 AC-2/4).
    assert result.rationale == "The Security Patch record demonstrates active remediation."
    assert result.status == STATUS_MET
    assert result.confidence_level == CONFIDENCE_HIGH
    assert result.input_snapshot["rationale_source"] == "llm"
    assert result.input_snapshot["llm_opinion"] == {"status": "partial", "confidence": 0.55}


def test_template_fallback_on_llm_failure() -> None:
    # The autouse guard makes the LLM raise; generation must still succeed via
    # the template path — which must NOT leak numeric scores/thresholds (KER-401),
    # exactly the same rule the LLM prompt follows. This test is the standing
    # coverage that the fallback path itself stays scrubbed.
    import re

    spy = _default_spy()
    evidence = [_make_evidence(relevance_score=0.2)]  # a gap — the case that used to leak
    with patch(_PATCH_TARGET, return_value=evidence):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)

    assert result.input_snapshot["rationale_source"] == "template"
    assert result.input_snapshot["llm_opinion"] is None
    assert result.rationale  # deterministic template text, never empty
    # No decimal scores (0.32), no threshold/relevance/confidence-score wording.
    assert not re.search(r"\d\.\d", result.rationale), f"numeric leak: {result.rationale!r}"
    lowered = result.rationale.lower()
    assert "confidence score" not in lowered
    assert "threshold" not in lowered
    assert "relevance" not in lowered


def test_malformed_opinion_keeps_rationale_and_drops_opinion(monkeypatch) -> None:
    monkeypatch.setenv("KERNO_LLM_MODEL", "mistral-large-latest")
    client = _mock_rationale_client({
        "rationale": "Coverage is adequate.",
        "own_status": "definitely-fine",  # not a valid status
        "own_confidence": 0.9,
    })
    monkeypatch.setattr(
        "src.services.recommendation_service.get_llm_client", lambda: client
    )
    spy = _default_spy()
    with patch(_PATCH_TARGET, return_value=[_make_evidence()]):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)

    assert result.input_snapshot["rationale_source"] == "llm"
    assert result.input_snapshot["llm_opinion"] is None


def test_generation_emits_decision_log_and_ledger_on_same_conn() -> None:
    spy = _default_spy()
    with patch(_PATCH_TARGET, return_value=[_make_evidence()]):
        generate_recommendation(
            spy, _TENANT_ID, _CONTROL_ID,
            triggered_by_user_id="d0000000-0000-4000-d000-000000000004",
            triggered_by_role="compliance_lead",
        )

    log_params = next(p for s, p in spy.calls if "INSERT INTO ai_decision_log" in str(s))
    assert log_params["control_id"] == _CONTROL_ID
    assert log_params["model_version"].startswith("evidence-rules-v1+")
    assert len(log_params["input_snapshot_hash"]) == 64

    audit_params = next(p for s, p in spy.calls if "INSERT INTO audit_log" in str(s))
    assert audit_params["action_type"] == "recommendation_generated"
    assert audit_params["actor_id"] == "d0000000-0000-4000-d000-000000000004"
    assert audit_params["actor_role"] == "compliance_lead"


def test_unknown_control_raises_entry_not_found() -> None:
    from src.exceptions import EntryNotFoundError

    spy = _SpyConn()  # no compliance_controls row configured
    with pytest.raises(EntryNotFoundError):
        generate_recommendation(spy, _TENANT_ID, "ghost-control")


# ── KER-401: 429 backoff (narrow, rate-limit only) ────────────────────────────


def _rate_limit_error():
    """A stand-in for Mistral's SDKError carrying a 429 raw_response."""
    from unittest.mock import MagicMock

    exc = Exception("API error occurred: Status 429. Body: rate_limited")
    exc.raw_response = MagicMock(status_code=429)
    return exc


def test_backoff_retries_then_recovers_on_rate_limit(monkeypatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setenv("KERNO_LLM_MODEL", "mistral-large-latest")
    sleeps: list[float] = []
    monkeypatch.setattr(
        "src.services.recommendation_service.time.sleep", lambda s: sleeps.append(s)
    )
    calls = {"n": 0}

    def _complete(**kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:  # 429 on the first two attempts, then succeed
            raise _rate_limit_error()
        choice = MagicMock()
        choice.message.content = json.dumps({
            "rationale": "Recovered prose naming the evidence.",
            "own_status": "met", "own_confidence": 0.8,
        })
        return MagicMock(choices=[choice])

    client = MagicMock()
    client.chat.complete.side_effect = _complete
    monkeypatch.setattr("src.services.recommendation_service.get_llm_client", lambda: client)

    spy = _default_spy()
    with patch(_PATCH_TARGET, return_value=[_make_evidence(relevance_score=0.9)]):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)

    assert result.input_snapshot["rationale_source"] == "llm"
    assert result.rationale == "Recovered prose naming the evidence."
    assert sleeps == [1.0, 2.0]  # backed off 1s then 2s before the 3rd call recovered
    assert calls["n"] == 3


def test_non_rate_limit_error_falls_straight_through_no_retry(monkeypatch) -> None:
    from unittest.mock import MagicMock

    monkeypatch.setenv("KERNO_LLM_MODEL", "mistral-large-latest")
    sleeps: list[float] = []
    monkeypatch.setattr(
        "src.services.recommendation_service.time.sleep", lambda s: sleeps.append(s)
    )
    client = MagicMock()
    client.chat.complete.side_effect = Exception("connection reset")  # NOT a 429
    monkeypatch.setattr("src.services.recommendation_service.get_llm_client", lambda: client)

    spy = _default_spy()
    with patch(_PATCH_TARGET, return_value=[_make_evidence()]):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)

    assert result.input_snapshot["rationale_source"] == "template"  # fell straight through
    assert sleeps == []  # no backoff for a non-rate-limit error
    assert client.chat.complete.call_count == 1  # tried once, no retries


# ── KER-401: rationale prompt feeds content, not scores ───────────────────────


def test_rationale_prompt_includes_body_and_excludes_scores() -> None:
    from src.services.recommendation_service import _build_rationale_prompt

    meta = ("NIS2-21.2e", "NIS2-21.2e", "Security in acquisition and development")
    evidence = [_make_evidence(relevance_score=0.42, title="Secure SDLC Policy v1.0")]
    evidence[0] = evidence[0].__class__(**{**evidence[0].__dict__,
                                           "body": "Mandatory code review and SAST gates."})
    scoring = ScoringResult(confidence_score=0.32, status="gap",
                            confidence_level="low", requires_review=True)
    messages = _build_rationale_prompt(meta, evidence, scoring)
    prompt = " ".join(m["content"] for m in messages)
    # The evidence line the model reads (everything after "Active evidence:").
    evidence_section = prompt.split("Active evidence:", 1)[1]

    # The evidence CONTENT is present…
    assert "Mandatory code review and SAST gates." in prompt
    assert "Secure SDLC Policy v1.0" in prompt
    # …but no relevance/confidence numbers reach the model at all…
    assert "0.42" not in prompt
    assert "0.32" not in prompt
    # …and the evidence lines carry no relevance score wording (the instruction
    # legitimately uses the word "relevance" to forbid it — that is not a leak).
    assert "relevance" not in evidence_section.lower()
    # The verdict is present as status + level only (no raw score).
    assert "status=gap" in prompt
    assert "confidence level=low" in prompt


def test_rationale_prompt_truncates_long_body() -> None:
    from config.constants import EVIDENCE_BODY_PROMPT_CHAR_LIMIT
    from src.services.recommendation_service import _build_rationale_prompt

    long_body = "A" * (EVIDENCE_BODY_PROMPT_CHAR_LIMIT + 500)
    evidence = [_make_evidence(relevance_score=0.9)]
    evidence[0] = evidence[0].__class__(**{**evidence[0].__dict__, "body": long_body})
    meta = ("c", "REF", "Title")
    scoring = ScoringResult(confidence_score=0.9, status="met",
                            confidence_level="high", requires_review=False)
    prompt = " ".join(m["content"] for m in _build_rationale_prompt(meta, evidence, scoring))

    assert "A" * EVIDENCE_BODY_PROMPT_CHAR_LIMIT in prompt        # cap kept
    assert "A" * (EVIDENCE_BODY_PROMPT_CHAR_LIMIT + 1) not in prompt  # nothing past it
    assert "…" in prompt                                          # ellipsis marks the cut


def test_rationale_prompt_carries_verdict_alignment_clause() -> None:
    # Regression lock for the mismatch fix (KER-401): the prompt must instruct
    # the model not to let a thorough-sounding document pull its wording away
    # from a gap/partial verdict. The behavioural proof is a live-LLM check;
    # this guards the instruction against silent removal.
    from src.services.recommendation_service import _build_rationale_prompt

    meta = ("c", "REF", "Title")
    scoring = ScoringResult(confidence_score=0.18, status="gap",
                            confidence_level="low", requires_review=True)
    prompt = " ".join(m["content"] for m in _build_rationale_prompt(meta, [_make_evidence()], scoring)).lower()
    assert "gap" in prompt and "partial" in prompt
    assert "thorough" in prompt or "comprehensive" in prompt  # the sounds-strong caveat
    assert "final" in prompt  # verdict is fixed


# ── SEC-REMED-005: review lock, current-row reads, and the review claim ───────


def _review_lock_positions(spy: _SpyConn) -> list[int]:
    return [
        index for index, (sql, params) in enumerate(spy.calls)
        if "pg_advisory_xact_lock" in str(sql) and params == {"lock_key": _REVIEW_LOCK_KEY}
    ]


def _writes(spy: _SpyConn) -> list[str]:
    return [
        str(sql) for sql, _ in spy.calls
        if str(sql).lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


def _claim_spy(reviewed_row: tuple | None, current_row: tuple | None) -> _SpyConn:
    return _SpyConn(responses=[
        ("FOR SHARE", _SelectResult([reviewed_row] if reviewed_row else [])),
        ("ORDER BY generated_at DESC", _SelectResult([current_row] if current_row else [])),
    ])


def test_generation_takes_the_review_lock_after_the_llm_call_and_before_superseding(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KERNO_LLM_MODEL", "mistral-large-latest")
    spy = _default_spy()
    client = _mock_rationale_client({
        "rationale": "The Security Patch record demonstrates active remediation.",
        "own_status": "met",
        "own_confidence": 0.9,
    })
    llm_response = client.chat.complete.return_value
    statements_before_llm_call: list[int] = []

    def _complete(**kwargs):
        statements_before_llm_call.append(len(spy.calls))
        return llm_response

    client.chat.complete.side_effect = _complete
    monkeypatch.setattr("src.services.recommendation_service.get_llm_client", lambda: client)
    with patch(_PATCH_TARGET, return_value=[_make_evidence()]):
        result = generate_recommendation(spy, _TENANT_ID, _CONTROL_ID)

    assert result.input_snapshot["rationale_source"] == "llm"
    assert len(statements_before_llm_call) == 1
    lock_positions = _review_lock_positions(spy)
    assert len(lock_positions) == 1
    statements = [str(sql) for sql, _ in spy.calls]
    supersede_position = next(
        index for index, sql in enumerate(statements) if "is_superseded = TRUE" in sql
    )
    ledger_lock_position = next(
        index for index, (_, params) in enumerate(spy.calls) if params == {"lock_key": _TENANT_ID}
    )
    # No lock may be held across the external request, and the review lock
    # precedes both the supersede and the tenant ledger lock.
    assert lock_positions[0] >= statements_before_llm_call[0]
    assert lock_positions[0] < supersede_position < ledger_lock_position


def test_get_recommendation_breaks_generated_at_ties_by_recommendation_id() -> None:
    spy = _SpyConn()
    assert get_recommendation(spy, _TENANT_ID, _CONTROL_ID) is None
    sql, params = next((s, p) for s, p in spy.calls if "FROM recommendations" in str(s))
    assert "is_superseded = FALSE" in sql
    assert "ORDER BY generated_at DESC, recommendation_id DESC" in sql
    assert params == {"tenant_id": _TENANT_ID, "control_id": _CONTROL_ID}


def test_get_recommendation_by_id_is_tenant_scoped_and_reads_superseded_rows() -> None:
    spy = _SpyConn()
    assert get_recommendation_by_id(spy, _TENANT_ID, uuid.UUID(_RECOMMENDATION_ID)) is None
    assert "SET LOCAL" in str(spy.calls[0][0])
    sql, params = next((s, p) for s, p in spy.calls if "FROM recommendations" in str(s))
    assert "WHERE recommendation_id = :recommendation_id" in sql
    assert "AND tenant_id = :tenant_id" in sql
    assert "is_superseded = FALSE" not in sql
    assert params == {"recommendation_id": _RECOMMENDATION_ID, "tenant_id": _TENANT_ID}


def test_claim_takes_the_review_lock_before_reading_the_reviewed_row_for_share() -> None:
    spy = _claim_spy(
        (_RECOMMENDATION_ID, _CONTROL_ID, STATUS_PARTIAL, False), (_RECOMMENDATION_ID,)
    )
    claim_recommendation_for_review(spy, _TENANT_ID, _RECOMMENDATION_ID, _CONTROL_ID)
    assert len(spy.calls) == 3
    lock_sql, lock_params = spy.calls[0]
    assert "pg_advisory_xact_lock(hashtextextended(:lock_key, 0))" in lock_sql
    assert lock_params == {"lock_key": _REVIEW_LOCK_KEY}
    read_sql, read_params = spy.calls[1]
    assert "FOR SHARE" in read_sql
    assert "tenant_id = :tenant_id" in read_sql
    assert read_params == {"tenant_id": _TENANT_ID, "recommendation_id": _RECOMMENDATION_ID}
    current_sql, current_params = spy.calls[2]
    assert "is_superseded = FALSE" in current_sql
    assert "ORDER BY generated_at DESC, recommendation_id DESC" in current_sql
    assert current_params == {"tenant_id": _TENANT_ID, "control_id": _CONTROL_ID}


def test_claim_of_an_id_outside_the_tenant_raises_entry_not_found() -> None:
    spy = _claim_spy(None, None)
    with pytest.raises(EntryNotFoundError):
        claim_recommendation_for_review(spy, _TENANT_ID, _RECOMMENDATION_ID, _CONTROL_ID)
    assert _writes(spy) == []


def test_claim_of_another_controls_recommendation_raises_value_error() -> None:
    spy = _claim_spy(
        (_RECOMMENDATION_ID, _OTHER_CONTROL_ID, STATUS_PARTIAL, False), (_RECOMMENDATION_ID,)
    )
    with pytest.raises(ValueError, match="does not belong to control"):
        claim_recommendation_for_review(spy, _TENANT_ID, _RECOMMENDATION_ID, _CONTROL_ID)
    assert _writes(spy) == []


def test_claim_of_a_superseded_recommendation_is_stale() -> None:
    # The current-id read is made to name the same row, so the superseded
    # flag alone has to refuse the decision.
    spy = _claim_spy(
        (_RECOMMENDATION_ID, _CONTROL_ID, STATUS_PARTIAL, True), (_RECOMMENDATION_ID,)
    )
    with pytest.raises(StaleRecommendationError, match="newer recommendation"):
        claim_recommendation_for_review(spy, _TENANT_ID, _RECOMMENDATION_ID, _CONTROL_ID)
    assert _writes(spy) == []


def test_claim_is_stale_when_a_newer_recommendation_is_current() -> None:
    spy = _claim_spy(
        (_RECOMMENDATION_ID, _CONTROL_ID, STATUS_PARTIAL, False), (_NEWER_RECOMMENDATION_ID,)
    )
    with pytest.raises(StaleRecommendationError, match="newer recommendation"):
        claim_recommendation_for_review(spy, _TENANT_ID, _RECOMMENDATION_ID, _CONTROL_ID)
    assert _writes(spy) == []


def test_claim_is_stale_when_the_control_has_no_current_recommendation() -> None:
    spy = _claim_spy((_RECOMMENDATION_ID, _CONTROL_ID, STATUS_PARTIAL, False), None)
    with pytest.raises(StaleRecommendationError):
        claim_recommendation_for_review(spy, _TENANT_ID, _RECOMMENDATION_ID, _CONTROL_ID)
    assert _writes(spy) == []


def test_claim_of_the_current_recommendation_returns_the_reviewed_row() -> None:
    # The driver may hand back UUID objects; the claim compares and returns text.
    spy = _claim_spy(
        (uuid.UUID(_RECOMMENDATION_ID), _CONTROL_ID, STATUS_PARTIAL, False),
        (uuid.UUID(_RECOMMENDATION_ID),),
    )
    reviewed = claim_recommendation_for_review(spy, _TENANT_ID, _RECOMMENDATION_ID, _CONTROL_ID)
    assert reviewed == ReviewedRecommendation(
        recommendation_id=_RECOMMENDATION_ID, control_id=_CONTROL_ID, status=STATUS_PARTIAL
    )
    assert _writes(spy) == []
