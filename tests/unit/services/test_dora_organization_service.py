"""Unit tests for dora_organization_service — validation, normalisation, guards, ledger.

No database. A spy connection records every execute() so the tests can assert
what SQL was issued, in what order, with what parameters, and that nothing at
all was issued when validation rejects the input. Everything that needs a
real database — constraints, RLS, the composite FK — lives in
tests/security/test_dora_organization_isolation.py and
tests/integration/test_dora_v2_001_organizations.py.

Run: pytest tests/unit/services/test_dora_organization_service.py -v
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.exceptions import EntryNotFoundError, TenantContextMissingError
from src.services.dora_organization_service import (
    ACTION_ORGANIZATION_CREATED,
    ACTION_ORGANIZATION_IDENTIFIER_ADDED,
    ACTION_ORGANIZATION_ROLE_ADDED,
    ACTION_ORGANIZATION_UPDATED,
    OBJECT_TYPE_ORGANIZATION,
    OBJECT_TYPE_ORGANIZATION_IDENTIFIER,
    OBJECT_TYPE_ORGANIZATION_ROLE,
    ORGANIZATION_CAPABLE_ROLES,
    OrganizationInput,
    add_organization_identifier,
    add_organization_role,
    create_organization,
    get_organization,
    list_organization_identifiers,
    list_organization_roles,
    list_organizations,
    update_organization,
)

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_OTHER_TENANT_ID = "b0000000-0000-4000-b000-000000000002"
_ORG_ID = "c0000000-0000-4000-c000-000000000001"
_ACTOR_ID = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_NOW = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)


class _NullResult:
    """Simulates a non-SELECT result — fetchone/fetchall return empty."""

    def fetchone(self):
        return None

    def fetchall(self) -> list:
        return []


class _SelectResult:
    """Simulates a SELECT result returning fixed row tuples."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _SpyConn:
    """Records execute() calls; returns the first configured response whose fragment matches."""

    def __init__(self, responses: list[tuple[str, object]] | None = None) -> None:
        self.calls: list[tuple] = []
        self._responses = responses or []

    def execute(self, sql, params=None) -> object:
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in str(sql):
                return result
        return _NullResult()


def _org_row(legal_name: str = "Alpha Bank AG", country_code: str | None = "DE") -> tuple:
    return (_ORG_ID, _TENANT_ID, legal_name, country_code, True, _NOW, _NOW)


def _org_exists() -> tuple[str, object]:
    """Spy response making get/require-organisation find _ORG_ID."""
    return ("FROM dora_organizations\nWHERE tenant_id = :tenant_id\n  AND organization_id", _SelectResult([_org_row()]))


def _calls_matching(spy: _SpyConn, fragment: str) -> list[tuple]:
    return [c for c in spy.calls if fragment in str(c[0])]


def _write(fn, spy, *args, **kwargs):
    return fn(spy, _TENANT_ID, *args, actor_id=_ACTOR_ID, actor_role="compliance_lead", **kwargs)


# ── Organisation validation ─────────────────────────────────────────────────


@pytest.mark.parametrize("blank", ["", "   ", "\n\t", None])
def test_blank_legal_name_is_rejected_before_any_sql(blank):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="legal_name"):
        _write(create_organization, spy, OrganizationInput(legal_name=blank))
    assert spy.calls == []


def test_legal_name_is_trimmed():
    spy = _SpyConn()
    created = _write(create_organization, spy, OrganizationInput(legal_name="  Alpha Bank AG  "))
    assert created.legal_name == "Alpha Bank AG"
    insert = _calls_matching(spy, "INSERT INTO dora_organizations")[0]
    assert insert[1]["legal_name"] == "Alpha Bank AG"


def test_two_organizations_may_share_a_legal_name():
    # No name-uniqueness check exists anywhere in the service: two creates with
    # the same name issue two independent INSERTs with distinct ids.
    spy = _SpyConn()
    first = _write(create_organization, spy, OrganizationInput(legal_name="Example Group Services Ltd"))
    second = _write(create_organization, spy, OrganizationInput(legal_name="Example Group Services Ltd"))
    assert first.organization_id != second.organization_id
    assert len(_calls_matching(spy, "INSERT INTO dora_organizations")) == 2


@pytest.mark.parametrize(("raw", "expected"), [("de", "DE"), (" fr ", "FR"), ("Nl", "NL")])
def test_country_code_is_trimmed_and_upper_cased(raw, expected):
    spy = _SpyConn()
    created = _write(create_organization, spy, OrganizationInput(legal_name="X", country_code=raw))
    assert created.country_code == expected


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_blank_country_code_becomes_none(blank):
    spy = _SpyConn()
    created = _write(create_organization, spy, OrganizationInput(legal_name="X", country_code=blank))
    assert created.country_code is None


@pytest.mark.parametrize("bad", ["D", "DEU", "1A", "D-", "d1", "ÄT"])
def test_malformed_country_code_is_rejected_before_any_sql(bad):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="country_code"):
        _write(create_organization, spy, OrganizationInput(legal_name="X", country_code=bad))
    assert spy.calls == []


# ── Identifiers ─────────────────────────────────────────────────────────────


def test_identifier_type_is_trimmed_and_upper_cased_and_value_trimmed():
    spy = _SpyConn([_org_exists()])
    added = _write(add_organization_identifier, spy, _ORG_ID, " lei ", "  5493001KJTIIGC8Y1R12 ")
    assert added.identifier_type == "LEI"
    assert added.identifier_value == "5493001KJTIIGC8Y1R12"
    insert = _calls_matching(spy, "INSERT INTO dora_organization_identifiers")[0]
    assert insert[1]["identifier_type"] == "LEI"
    assert insert[1]["identifier_value"] == "5493001KJTIIGC8Y1R12"


def test_identifier_value_case_is_preserved():
    # Only the type is canonicalised. Values are not blindly lowercased.
    spy = _SpyConn([_org_exists()])
    added = _write(add_organization_identifier, spy, _ORG_ID, "EUID", "DEr.123AbC")
    assert added.identifier_value == "DEr.123AbC"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_identifier_type_is_rejected_before_any_sql(blank):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="identifier_type"):
        _write(add_organization_identifier, spy, _ORG_ID, blank, "X")
    assert spy.calls == []


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_identifier_value_is_rejected_before_any_sql(blank):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="identifier_value"):
        _write(add_organization_identifier, spy, _ORG_ID, "LEI", blank)
    assert spy.calls == []


def test_duplicate_identifier_in_the_same_tenant_is_rejected():
    spy = _SpyConn([
        _org_exists(),
        ("FROM dora_organization_identifiers\nWHERE tenant_id = :tenant_id\n  AND identifier_type",
         _SelectResult([("some-other-org-id",)])),
    ])
    with pytest.raises(ValueError, match="already identifies"):
        _write(add_organization_identifier, spy, _ORG_ID, "LEI", "X1")
    assert _calls_matching(spy, "INSERT INTO dora_organization_identifiers") == []


def test_duplicate_check_is_scoped_to_the_tenant():
    # The pre-check query must carry the tenant predicate: uniqueness is per
    # tenant, so the same pair in another tenant must never be consulted.
    spy = _SpyConn([_org_exists()])
    _write(add_organization_identifier, spy, _ORG_ID, "LEI", "X1")
    check = _calls_matching(spy, "AND identifier_value = :identifier_value")[0]
    assert "tenant_id = :tenant_id" in str(check[0])
    assert check[1]["tenant_id"] == _TENANT_ID


def test_identifier_on_unknown_organization_is_not_found():
    spy = _SpyConn()  # organisation lookup returns nothing
    with pytest.raises(EntryNotFoundError):
        _write(add_organization_identifier, spy, _ORG_ID, "LEI", "X1")
    assert _calls_matching(spy, "INSERT INTO") == []


# ── Roles ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["financial_entity", "ict_provider"])
def test_each_initial_role_is_accepted(role):
    spy = _SpyConn([_org_exists()])
    added = _write(add_organization_role, spy, _ORG_ID, role)
    assert added.role_type == role
    assert _calls_matching(spy, "INSERT INTO dora_organization_roles")[0][1]["role_type"] == role


@pytest.mark.parametrize(
    "bad", ["direct_provider", "subcontractor", "rank_1", "signatory", "consumer", "FINANCIAL_ENTITY", ""]
)
def test_unsupported_role_is_rejected_before_any_sql(bad):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="role_type"):
        _write(add_organization_role, spy, _ORG_ID, bad)
    assert spy.calls == []


def test_same_role_cannot_be_assigned_twice():
    spy = _SpyConn([
        _org_exists(),
        ("FROM dora_organization_roles\nWHERE tenant_id = :tenant_id\n  AND organization_id = :organization_id\n  AND role_type",
         _SelectResult([("existing-role-id",)])),
    ])
    with pytest.raises(ValueError, match="already holds"):
        _write(add_organization_role, spy, _ORG_ID, "ict_provider")
    assert _calls_matching(spy, "INSERT INTO dora_organization_roles") == []


def test_one_organization_can_hold_both_initial_roles():
    spy = _SpyConn([_org_exists()])
    first = _write(add_organization_role, spy, _ORG_ID, "financial_entity")
    second = _write(add_organization_role, spy, _ORG_ID, "ict_provider")
    assert {first.role_type, second.role_type} == {"financial_entity", "ict_provider"}
    assert len(_calls_matching(spy, "INSERT INTO dora_organization_roles")) == 2


def test_role_on_unknown_organization_is_not_found():
    spy = _SpyConn()
    with pytest.raises(EntryNotFoundError):
        _write(add_organization_role, spy, _ORG_ID, "ict_provider")
    assert _calls_matching(spy, "INSERT INTO") == []


# ── Tenant guards ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", [None, ""])
@pytest.mark.parametrize(
    "call",
    [
        lambda spy, t: create_organization(spy, t, OrganizationInput(legal_name="X"), actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy, t: get_organization(spy, t, _ORG_ID),
        lambda spy, t: list_organizations(spy, t),
        lambda spy, t: update_organization(spy, t, _ORG_ID, OrganizationInput(legal_name="X"), actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy, t: add_organization_identifier(spy, t, _ORG_ID, "LEI", "X", actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy, t: list_organization_identifiers(spy, t, _ORG_ID),
        lambda spy, t: add_organization_role(spy, t, _ORG_ID, "ict_provider", actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy, t: list_organization_roles(spy, t, _ORG_ID),
    ],
    ids=["create", "get", "list", "update", "add_identifier", "list_identifiers", "add_role", "list_roles"],
)
def test_missing_tenant_fails_before_any_sql(call, missing):
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        call(spy, missing)
    assert spy.calls == []


@pytest.mark.parametrize(
    "call",
    [
        lambda spy: create_organization(spy, _TENANT_ID, OrganizationInput(legal_name="X"), actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy: get_organization(spy, _TENANT_ID, _ORG_ID),
        lambda spy: list_organizations(spy, _TENANT_ID),
        lambda spy: update_organization(spy, _TENANT_ID, _ORG_ID, OrganizationInput(legal_name="X"), actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy: add_organization_identifier(spy, _TENANT_ID, _ORG_ID, "LEI", "X", actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy: list_organization_identifiers(spy, _TENANT_ID, _ORG_ID),
        lambda spy: add_organization_role(spy, _TENANT_ID, _ORG_ID, "ict_provider", actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy: list_organization_roles(spy, _TENANT_ID, _ORG_ID),
    ],
    ids=["create", "get", "list", "update", "add_identifier", "list_identifiers", "add_role", "list_roles"],
)
def test_tenant_context_is_set_before_any_tenant_sql(call):
    # Every operation's FIRST statement must be the SET LOCAL. A statement
    # touching a dora_* table before it would run without the policy context.
    spy = _SpyConn([_org_exists()])
    call(spy)
    assert spy.calls, "operation issued no SQL at all"
    assert "SET LOCAL app.current_tenant_id" in str(spy.calls[0][0])
    first_tenant_sql = next(i for i, c in enumerate(spy.calls) if "dora_organization" in str(c[0]))
    assert first_tenant_sql > 0


def test_every_tenant_query_carries_an_explicit_tenant_predicate():
    # RLS is the safety net, not the primary enforcement (CLAUDE.md §3.3).
    spy = _SpyConn([_org_exists()])
    _write(create_organization, spy, OrganizationInput(legal_name="X"))
    get_organization(spy, _TENANT_ID, _ORG_ID)
    list_organizations(spy, _TENANT_ID)
    _write(add_organization_identifier, spy, _ORG_ID, "LEI", "X")
    list_organization_identifiers(spy, _TENANT_ID, _ORG_ID)
    _write(add_organization_role, spy, _ORG_ID, "ict_provider")
    list_organization_roles(spy, _TENANT_ID, _ORG_ID)
    for sql, params in spy.calls:
        text = str(sql)
        if "dora_organization" in text and text.lstrip().startswith(("SELECT", "UPDATE")):
            assert "tenant_id = :tenant_id" in text, text
            assert params["tenant_id"] == _TENANT_ID


# ── Ledger ──────────────────────────────────────────────────────────────────


def _ledger_calls(spy: _SpyConn) -> list[tuple]:
    return _calls_matching(spy, "INSERT INTO audit_log")


def test_create_appends_the_expected_ledger_vocabulary():
    spy = _SpyConn()
    created = _write(create_organization, spy, OrganizationInput(legal_name="Alpha Bank AG", country_code="de"))
    entries = _ledger_calls(spy)
    assert len(entries) == 1
    params = entries[0][1]
    assert params["action_type"] == ACTION_ORGANIZATION_CREATED
    assert params["object_type"] == OBJECT_TYPE_ORGANIZATION
    assert params["object_id"] == created.organization_id
    assert params["actor_id"] == str(_ACTOR_ID)
    assert params["actor_role"] == "compliance_lead"
    assert params["control_id"] is None


def test_ledger_entry_is_after_the_business_write_on_the_same_connection():
    spy = _SpyConn()
    _write(create_organization, spy, OrganizationInput(legal_name="X"))
    order = [str(c[0]) for c in spy.calls]
    insert_at = next(i for i, s in enumerate(order) if "INSERT INTO dora_organizations" in s)
    ledger_at = next(i for i, s in enumerate(order) if "INSERT INTO audit_log" in s)
    assert insert_at < ledger_at


def test_update_ledgers_before_and_after_state():
    spy = _SpyConn([_org_exists()])
    _write(update_organization, spy, _ORG_ID, OrganizationInput(legal_name="After Ltd", country_code="fr"))
    entries = _ledger_calls(spy)
    assert len(entries) == 1
    params = entries[0][1]
    assert params["action_type"] == ACTION_ORGANIZATION_UPDATED
    assert params["object_type"] == OBJECT_TYPE_ORGANIZATION
    assert '"legal_name": "Alpha Bank AG"' in params["before_state"]
    assert '"legal_name": "After Ltd"' in params["after_state"]
    assert '"country_code": "FR"' in params["after_state"]


def test_identifier_and_role_append_their_own_object_types():
    spy = _SpyConn([_org_exists()])
    ident = _write(add_organization_identifier, spy, _ORG_ID, "LEI", "X1")
    role = _write(add_organization_role, spy, _ORG_ID, "ict_provider")
    entries = _ledger_calls(spy)
    assert len(entries) == 2
    by_action = {e[1]["action_type"]: e[1] for e in entries}
    assert by_action[ACTION_ORGANIZATION_IDENTIFIER_ADDED]["object_type"] == OBJECT_TYPE_ORGANIZATION_IDENTIFIER
    assert by_action[ACTION_ORGANIZATION_IDENTIFIER_ADDED]["object_id"] == ident.organization_identifier_id
    assert by_action[ACTION_ORGANIZATION_ROLE_ADDED]["object_type"] == OBJECT_TYPE_ORGANIZATION_ROLE
    assert by_action[ACTION_ORGANIZATION_ROLE_ADDED]["object_id"] == role.organization_role_id


def test_rejected_input_never_reaches_the_ledger():
    spy = _SpyConn()
    with pytest.raises(ValueError):
        _write(create_organization, spy, OrganizationInput(legal_name=""))
    assert _ledger_calls(spy) == []


# ── Concurrency: the update path locks its read; nothing else locks ────────


_ROW_LOCK_CLAUSES = ("FOR UPDATE", "FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE")


def _row_locking_statements(spy: _SpyConn) -> list[str]:
    return [str(c[0]) for c in spy.calls if any(clause in str(c[0]) for clause in _ROW_LOCK_CLAUSES)]


def _position_of(statements: list[str], fragment: str) -> int:
    for index, statement in enumerate(statements):
        if fragment in statement:
            return index
    raise AssertionError(f"no statement containing {fragment!r} was issued")


def test_update_locks_the_row_on_its_read_then_writes_then_takes_the_ledger_lock():
    # Lock order is the contract: organisation row lock first, the tenant's
    # ledger advisory lock last. A read that came after the UPDATE, or a
    # ledger append that came before the row lock, would reopen the stale
    # before_state window or invert the order against every other writer.
    spy = _SpyConn([_org_exists()])
    _write(update_organization, spy, _ORG_ID, OrganizationInput(legal_name="After Ltd"))
    order = [str(c[0]) for c in spy.calls]
    locking_read = _position_of(order, "FOR NO KEY UPDATE")
    update_at = _position_of(order, "UPDATE dora_organizations")
    advisory_at = _position_of(order, "pg_advisory_xact_lock")
    assert locking_read < update_at < advisory_at
    assert order[locking_read].lstrip().startswith("SELECT")
    assert "FROM dora_organizations" in order[locking_read]
    assert "tenant_id = :tenant_id" in order[locking_read]
    assert _row_locking_statements(spy) == [order[locking_read]]


def test_update_reads_the_organisation_exactly_once_and_ledgers_what_the_locked_read_returned():
    # A plain pre-read for before_state followed by a locking read whose
    # result is discarded would satisfy the ordering test above. The spy
    # answers the locked read and any plain read with different names, and
    # only one read may happen at all.
    locked_row = _org_row(legal_name="Locked Read Ltd")
    plain_row = _org_row(legal_name="Plain Read Ltd")
    fragment, _ = _org_exists()
    spy = _SpyConn([("FOR NO KEY UPDATE", _SelectResult([locked_row])), (fragment, _SelectResult([plain_row]))])
    _write(update_organization, spy, _ORG_ID, OrganizationInput(legal_name="After Ltd"))
    reads = [c for c in spy.calls if str(c[0]).lstrip().startswith("SELECT") and "FROM dora_organizations" in str(c[0])]
    assert len(reads) == 1
    before = json.loads(_ledger_calls(spy)[0][1]["before_state"])
    assert before["legal_name"] == "Locked Read Ltd"


def test_the_lock_is_no_key_update_not_the_stronger_for_update():
    # FOR NO KEY UPDATE is what the UPDATE of non-key columns takes itself and
    # it lets a child row's FK check (FOR KEY SHARE) through; FOR UPDATE would
    # queue every identifier and role insert behind an amendment.
    spy = _SpyConn([_org_exists()])
    _write(update_organization, spy, _ORG_ID, OrganizationInput(legal_name="After Ltd"))
    statements = _row_locking_statements(spy)
    assert len(statements) == 1
    assert statements[0].rstrip().endswith("FOR NO KEY UPDATE")


@pytest.mark.parametrize(
    "call",
    [
        lambda spy: create_organization(spy, _TENANT_ID, OrganizationInput(legal_name="X"), actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy: get_organization(spy, _TENANT_ID, _ORG_ID),
        lambda spy: list_organizations(spy, _TENANT_ID),
        lambda spy: add_organization_identifier(spy, _TENANT_ID, _ORG_ID, "LEI", "X", actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy: list_organization_identifiers(spy, _TENANT_ID, _ORG_ID),
        lambda spy: add_organization_role(spy, _TENANT_ID, _ORG_ID, "ict_provider", actor_id=_ACTOR_ID, actor_role="vciso"),
        lambda spy: list_organization_roles(spy, _TENANT_ID, _ORG_ID),
    ],
    ids=["create", "get", "list", "add_identifier", "list_identifiers", "add_role", "list_roles"],
)
def test_no_other_operation_takes_a_row_lock(call):
    spy = _SpyConn([_org_exists()])
    call(spy)
    assert _row_locking_statements(spy) == []


def test_update_of_an_unknown_organization_returns_none_and_writes_nothing():
    spy = _SpyConn()
    assert _write(update_organization, spy, _ORG_ID, OrganizationInput(legal_name="X")) is None
    assert _calls_matching(spy, "UPDATE dora_organizations") == []
    assert _ledger_calls(spy) == []


def test_ledger_states_carry_utc_timestamps_whatever_zone_the_row_came_back_in():
    # psycopg2 hands timestamptz back in the session's zone. The ledger writes
    # UTC either way, so an update's before_state equals the prior entry's
    # after_state field for field and not only instant for instant.
    session_zone = timezone(timedelta(hours=2))
    row_in_session_zone = (
        _ORG_ID, _TENANT_ID, "Alpha Bank AG", "DE", True,
        _NOW.astimezone(session_zone), _NOW.astimezone(session_zone),
    )
    fragment, _ = _org_exists()
    spy = _SpyConn([(fragment, _SelectResult([row_in_session_zone]))])
    _write(update_organization, spy, _ORG_ID, OrganizationInput(legal_name="After Ltd"))
    before = json.loads(_ledger_calls(spy)[0][1]["before_state"])
    assert before["created_at"] == "2026-09-18T12:00:00+00:00"
    assert before["updated_at"] == "2026-09-18T12:00:00+00:00"


# ── Authorisation constant ──────────────────────────────────────────────────


def test_organization_write_roles_match_the_live_register_roles():
    # Filing authority — the same allow-list the v1 register uses, kept as its
    # own constant so the two can diverge deliberately rather than by accident.
    assert [r.value for r in ORGANIZATION_CAPABLE_ROLES] == ["compliance_lead", "vciso"]
