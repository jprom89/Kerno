"""Unit tests for src/services/override_service.py.

Plain-English summary
---------------------
These tests verify the override capture service without a live database. Every
test uses a spy connection that records the raw SQL execute() calls the service
issues, allowing assertions about the order of those calls, the parameters
passed, and the absence of SQLAlchemy Session API calls (add(), flush()) that
would indicate the wrong connection contract.

Thirteen tests cover: justification text handling (null, email, hostname,
dual-write to both tables), connection contract correctness, SET LOCAL
ordering, audit log correctness, reviewer weight assignment, input validation,
tenant isolation, and the conftest named-parameter regex.

How to run
----------
    pytest tests/unit/services/test_override_service.py -v
"""

from __future__ import annotations

import uuid

import pytest

from config.constants import JUNIOR_REVIEWER_WEIGHT, SENIOR_REVIEWER_WEIGHT
from src.exceptions import TenantContextMissingError
from src.services.override_service import OverrideInput, capture_override

# A deterministic UUIDv4 for the test tenant — fixed so test failure messages
# are readable ("tenant c000..." rather than a random UUID).
_TENANT_ID = uuid.UUID("c0000000-0000-4000-c000-000000000003")

# A deterministic UUIDv4 for the test reviewer — fixed for the same reason.
_REVIEWER_ID = uuid.UUID("d0000000-0000-4000-d000-000000000004")


# ── Test infrastructure ───────────────────────────────────────────────────────


class _NullResult:
    """Simulates the return value of a non-SELECT execute call (e.g. INSERT).

    The application services call fetchall() or fetchone() on the result of
    conn.execute(). For INSERT statements, these must return empty/None rather
    than raising an exception — this class satisfies that contract.
    """

    def fetchall(self) -> list:
        """Return an empty list, as a non-SELECT result has no rows."""
        return []

    def fetchone(self):
        """Return None, as a non-SELECT result has no next row."""
        return None


class _SpyConn:
    """Records every execute() call and raises on SQLAlchemy Session API usage.

    The override service must use ``conn.execute(sql, params)`` — the raw
    connection contract. If it accidentally calls ``conn.add()`` or
    ``conn.flush()`` (SQLAlchemy Session API), those methods raise
    AssertionError so the test fails with a clear message rather than a
    silent wrong-API call.

    Inspect ``self.calls`` after capture_override() to assert on SQL order
    and parameters. Each entry is a (sql, params) tuple in call order.
    """

    def __init__(self) -> None:
        """Initialise with an empty call log."""
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params=None) -> _NullResult:
        """Append (sql, params) to the call log and return a null result."""
        self.calls.append((sql, params))
        return _NullResult()

    def commit(self) -> None:
        """No-op — unit tests do not need real transaction control."""

    def rollback(self) -> None:
        """No-op — unit tests do not need real transaction control."""

    def add(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError(
            "conn.add() was called. The override service must use "
            "conn.execute(sql, params) — not the SQLAlchemy Session API."
        )

    def flush(self, *args, **kwargs) -> None:
        """Raise to detect incorrect SQLAlchemy Session API usage."""
        raise AssertionError(
            "conn.flush() was called. The override service must use "
            "conn.execute(sql, params) — not the SQLAlchemy Session API."
        )


class _FakeSession:
    """Minimal session that supplies a fixed tenant UUID to the service layer.

    ``resolve_and_set_tenant_context()`` in ``tenant_context.py`` reads the
    tenant identity by calling ``session.resolve_tenant_id()``. This class
    implements exactly that interface with a deterministic value so tests are
    reproducible.
    """

    def __init__(self, tenant_id: uuid.UUID = _TENANT_ID) -> None:
        """Store the tenant UUID that resolve_tenant_id() will return."""
        self._tenant_id = tenant_id

    def resolve_tenant_id(self) -> uuid.UUID:
        """Return the fixed test tenant UUID."""
        return self._tenant_id


def _make_input(**kwargs) -> OverrideInput:
    """Build an OverrideInput with sensible defaults; override any field via kwargs.

    Defaults produce a valid 'approve' override with no justification text.
    Pass keyword arguments to override any field for a specific test scenario.
    """
    defaults: dict = {
        "reviewer_id": _REVIEWER_ID,
        "reviewer_role": "vciso",
        "action_type": "approve",
        "original_control_id": "ctrl-001",
        "corrected_control_id": None,
        "justification_text": None,
    }
    defaults.update(kwargs)
    return OverrideInput(**defaults)


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_null_justification_text_stored_as_none() -> None:
    """justification_text=None must reach the overrides table as NULL, not empty string."""
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(justification_text=None))
    override_params = spy.calls[1][1]
    assert override_params["justification_text"] is None


def test_email_in_justification_text_is_anonymised() -> None:
    """An email address in justification_text must be replaced with [INTERNAL_EMAIL]."""
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Reviewed by alice@example.com"),
    )
    override_params = spy.calls[1][1]
    assert "[INTERNAL_EMAIL]" in override_params["justification_text"]
    assert "alice@example.com" not in override_params["justification_text"]


def test_internal_hostname_in_justification_text_is_anonymised() -> None:
    """An internal hostname in justification_text must be replaced with [INTERNAL_HOST]."""
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Control mapped via proxy.internal gateway"),
    )
    override_params = spy.calls[1][1]
    assert "[INTERNAL_HOST]" in override_params["justification_text"]
    assert "proxy.internal" not in override_params["justification_text"]


def test_anonymised_value_appears_in_both_override_and_audit_log() -> None:
    """The anonymised text must be stored identically in both the override and audit log rows."""
    spy = _SpyConn()
    capture_override(
        _FakeSession(),
        spy,
        _make_input(justification_text="Contact admin@kerno.io for details"),
    )
    override_params = spy.calls[1][1]
    audit_params = spy.calls[2][1]
    assert "[INTERNAL_EMAIL]" in override_params["justification_text"]
    assert audit_params["justification_text"] == override_params["justification_text"]


def test_no_sqlalchemy_session_api_called() -> None:
    """conn.add() and conn.flush() must never be called during override capture.

    _SpyConn.add() and .flush() raise AssertionError if invoked, so a clean
    return from capture_override() proves the SQLAlchemy Session API was not used.
    """
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())


def test_set_local_fires_before_insert_override() -> None:
    """SET LOCAL must be the very first SQL call — tenant context before any write."""
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    first_sql, _ = spy.calls[0]
    assert "SET LOCAL" in first_sql


def test_audit_log_references_correct_override_id() -> None:
    """The override_id in the audit log INSERT must match the override_id in the overrides INSERT."""
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input())
    override_params = spy.calls[1][1]
    audit_params = spy.calls[2][1]
    assert audit_params["override_id"] == override_params["override_id"]


def test_vciso_gets_senior_confidence_weight() -> None:
    """A vciso reviewer must be assigned SENIOR_REVIEWER_WEIGHT."""
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(reviewer_role="vciso"))
    override_params = spy.calls[1][1]
    assert override_params["reviewer_confidence_weight"] == SENIOR_REVIEWER_WEIGHT


def test_internal_admin_gets_junior_confidence_weight() -> None:
    """An internal_admin reviewer must be assigned JUNIOR_REVIEWER_WEIGHT."""
    spy = _SpyConn()
    capture_override(_FakeSession(), spy, _make_input(reviewer_role="internal_admin"))
    override_params = spy.calls[1][1]
    assert override_params["reviewer_confidence_weight"] == JUNIOR_REVIEWER_WEIGHT


def test_invalid_action_type_raises_value_error() -> None:
    """An unrecognised action_type must raise ValueError before any SQL is issued."""
    spy = _SpyConn()
    with pytest.raises(ValueError, match="action_type"):
        capture_override(_FakeSession(), spy, _make_input(action_type="approve_all"))
    assert len(spy.calls) == 0


def test_edit_without_corrected_control_id_raises_value_error() -> None:
    """action_type='edit' with no corrected_control_id must raise ValueError before any SQL."""
    spy = _SpyConn()
    with pytest.raises(ValueError, match="corrected_control_id"):
        capture_override(
            _FakeSession(),
            spy,
            _make_input(action_type="edit", corrected_control_id=None),
        )
    assert len(spy.calls) == 0


def test_none_session_raises_tenant_context_missing_error() -> None:
    """A None session must raise TenantContextMissingError before any SQL is issued."""
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        capture_override(None, spy, _make_input())
    assert len(spy.calls) == 0


def test_named_param_regex_does_not_match_postgresql_type_casts() -> None:
    """The conftest :name regex must not match the second colon in ::typename casts.

    PostgreSQL uses ::typename syntax for explicit type casts (e.g. ::uuid,
    ::timestamptz). Without a negative lookbehind, the regex would treat the
    type name as a named parameter. This test imports the compiled pattern from
    conftest and verifies it only matches genuine :name placeholders.
    """
    from tests.conftest import _NAMED_PARAM_RE

    sql = "WHERE tenant_id = :tenant_id AND ts > '1970-01-01'::timestamptz"
    matches = _NAMED_PARAM_RE.findall(sql)
    assert matches == ["tenant_id"]
    assert "timestamptz" not in matches
