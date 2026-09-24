"""Unit tests for dora_contract_service — normalisation, guards, conflicts, locking, ledger.

No database. A spy connection records every execute() so the tests can assert
what SQL was issued, in what order, with what parameters — and that nothing
was issued when validation refuses the input. Constraints, RLS, the composite
foreign keys and real concurrency are proven against PostgreSQL in
tests/security/test_dora_contract_isolation.py and
tests/integration/test_dora_v2_002a_*.py.

Run: pytest tests/unit/services/test_dora_contract_service.py -v
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from src.exceptions import DORAContractConflictError, EntryNotFoundError, TenantContextMissingError
from src.models.dora_contract import CONTRACT_TEXT_TRIM_CHARACTERS
from src.services.dora_contract_service import (
    ACTION_CONTRACT_CREATED,
    ACTION_CONTRACT_PARTY_ADDED,
    ACTION_CONTRACT_UPDATED,
    CONTRACT_CAPABLE_ROLES,
    OBJECT_TYPE_CONTRACT,
    OBJECT_TYPE_CONTRACT_PARTY,
    ContractInput,
    ContractUpdate,
    add_contract_party,
    create_contract,
    get_contract,
    list_contract_parties,
    list_contracts,
    list_contracts_for_organization,
    update_contract,
)

_TENANT_ID = "a0000000-0000-4000-a000-000000000001"
_CONTRACT_ID = "c0000000-0000-4000-c000-00000000000c"
_ORG_ID = "e0000000-0000-4000-e000-00000000000e"
_ACTOR_ID = uuid.UUID("d0000000-0000-4000-d000-000000000004")
_NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
_ROW_LOCK_CLAUSES = ("FOR UPDATE", "FOR NO KEY UPDATE", "FOR SHARE", "FOR KEY SHARE")

_CONTRACT_BY_ID = "FROM dora_contracts\nWHERE tenant_id = :tenant_id\n  AND contract_id = :contract_id"
_ORG_BY_ID = "FROM dora_organizations"
_ICT_PROVIDER_ROLE = "FROM dora_organization_roles"
_INSERT_CONTRACT = "INSERT INTO dora_contracts"
_INSERT_PARTY = "INSERT INTO dora_contract_parties"


class _NullResult:
    """Simulates a statement that returns no row."""

    def fetchone(self):
        return None

    def fetchall(self) -> list:
        return []


class _SelectResult:
    """Simulates a result returning fixed row tuples."""

    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list:
        return self._rows


class _SpyConn:
    """Records execute() calls; answers with the first configured response whose fragment matches."""

    def __init__(self, responses: list[tuple[str, object]] | None = None) -> None:
        self.calls: list[tuple] = []
        self._responses = responses or []

    def execute(self, sql, params=None) -> object:
        self.calls.append((sql, params))
        for fragment, result in self._responses:
            if fragment in str(sql):
                if isinstance(result, Exception):
                    raise result
                return result
        return _NullResult()


def _contract_row(reference: str = "MSA-2024-01", display_name: str | None = "Hosting MSA") -> tuple:
    return (_CONTRACT_ID, _TENANT_ID, reference, display_name, date(2024, 1, 1), None, True, _NOW, _NOW)


def _inserted() -> _SelectResult:
    return _SelectResult([("returned-id",)])


def _contract_exists(row: tuple | None = None) -> tuple[str, object]:
    return (_CONTRACT_BY_ID, _SelectResult([row or _contract_row()]))


def _party_world(*, provider_role: bool) -> list[tuple[str, object]]:
    responses = [_contract_exists(), (_ORG_BY_ID, _SelectResult([(_ORG_ID,)])), (_INSERT_PARTY, _inserted())]
    if provider_role:
        responses.append((_ICT_PROVIDER_ROLE, _SelectResult([("role-id",)])))
    return responses


def _calls_matching(spy: _SpyConn, fragment: str) -> list[tuple]:
    return [c for c in spy.calls if fragment in str(c[0])]


def _ledger_calls(spy: _SpyConn) -> list[tuple]:
    return _calls_matching(spy, "INSERT INTO audit_log")


def _write(fn, spy, *args, **kwargs):
    return fn(spy, _TENANT_ID, *args, actor_id=_ACTOR_ID, actor_role="compliance_lead", **kwargs)


def _create(spy, **fields):
    return _write(create_contract, spy, ContractInput(**{"contract_reference": "MSA-1", **fields}))


def _update(spy, contract_id=_CONTRACT_ID, **fields):
    values = {"display_name": None, "contract_start_date": None, "contract_end_date": None, "is_active": True}
    return _write(update_contract, spy, contract_id, ContractUpdate(**{**values, **fields}))


def _position_of(statements: list[str], fragment: str) -> int:
    for index, statement in enumerate(statements):
        if fragment in statement:
            return index
    raise AssertionError(f"no statement containing {fragment!r} was issued")


def _row_locking_statements(spy: _SpyConn) -> list[str]:
    return [str(c[0]) for c in spy.calls if any(clause in str(c[0]) for clause in _ROW_LOCK_CLAUSES)]


# ── Contract reference: the one whitespace policy ───────────────────────────


@pytest.mark.parametrize("space", list(CONTRACT_TEXT_TRIM_CHARACTERS), ids=lambda c: f"U+{ord(c):04X}")
def test_each_policy_character_is_trimmed_from_both_ends_of_the_reference(space):
    spy = _SpyConn([(_INSERT_CONTRACT, _inserted())])
    created = _create(spy, contract_reference=f"{space}MSA-1{space}")
    assert created.contract_reference == "MSA-1"


def test_interior_whitespace_case_and_punctuation_are_preserved():
    spy = _SpyConn([(_INSERT_CONTRACT, _inserted())])
    created = _create(spy, contract_reference=" \t-Msa 2024/01\tv.2- \n")
    assert created.contract_reference == "-Msa 2024/01\tv.2-"


def test_the_reference_is_persisted_in_its_canonical_form():
    spy = _SpyConn([(_INSERT_CONTRACT, _inserted())])
    _create(spy, contract_reference="\n MSA-1 \r\n")
    assert _calls_matching(spy, _INSERT_CONTRACT)[0][1]["contract_reference"] == "MSA-1"


@pytest.mark.parametrize("missing", [None, "", " ", "\t\n", " 　"])
def test_a_missing_reference_is_refused_before_any_sql_and_never_invented(missing):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="contract_reference is required"):
        _create(spy, contract_reference=missing)
    assert spy.calls == []


@pytest.mark.parametrize("not_text", [123, b"MSA-1", ["MSA-1"]])
def test_a_non_text_reference_is_refused_rather_than_coerced(not_text):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="contract_reference must be text"):
        _create(spy, contract_reference=not_text)
    assert spy.calls == []


def test_display_name_is_trimmed_and_blank_becomes_none():
    spy = _SpyConn([(_INSERT_CONTRACT, _inserted())])
    assert _create(spy, display_name="  Cloud hosting \n").display_name == "Cloud hosting"
    assert _create(spy, display_name=" \t ").display_name is None
    assert _create(spy).display_name is None


# ── Dates and the active flag ───────────────────────────────────────────────


def test_an_end_date_before_the_start_date_is_refused_before_any_sql():
    spy = _SpyConn()
    with pytest.raises(ValueError, match="before contract_start_date"):
        _create(spy, contract_start_date=date(2025, 1, 2), contract_end_date=date(2025, 1, 1))
    assert spy.calls == []


@pytest.mark.parametrize(
    "start, end",
    [(None, None), (date(2025, 1, 1), None), (None, date(2025, 1, 1)), (date(2025, 1, 1), date(2025, 1, 1))],
)
def test_absent_dates_and_equal_dates_are_accepted(start, end):
    spy = _SpyConn([(_INSERT_CONTRACT, _inserted())])
    created = _create(spy, contract_start_date=start, contract_end_date=end)
    assert (created.contract_start_date, created.contract_end_date) == (start, end)


@pytest.mark.parametrize("not_a_date", [datetime(2025, 1, 1, tzinfo=timezone.utc), "2025-01-01", 20250101])
def test_a_date_field_accepts_only_a_date(not_a_date):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="contract_start_date must be a date"):
        _create(spy, contract_start_date=not_a_date)
    assert spy.calls == []


@pytest.mark.parametrize("not_bool", [None, 1, "false"])
def test_is_active_accepts_only_a_boolean(not_bool):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="is_active"):
        _create(spy, is_active=not_bool)
    assert spy.calls == []


# ── Duplicates: the named unique constraint decides ─────────────────────────


def test_contract_insert_names_its_own_unique_constraint_as_the_conflict_arbiter():
    spy = _SpyConn([(_INSERT_CONTRACT, _inserted())])
    _create(spy)
    sql = str(_calls_matching(spy, _INSERT_CONTRACT)[0][0])
    assert "ON CONFLICT ON CONSTRAINT uq_dora_contracts_tenant_reference DO NOTHING" in sql
    assert "RETURNING" in sql


def test_a_contract_insert_that_returns_no_row_is_a_conflict_and_is_not_ledgered():
    spy = _SpyConn()
    with pytest.raises(DORAContractConflictError, match="already exists"):
        _create(spy, contract_reference="MSA-1")
    assert len(_calls_matching(spy, _INSERT_CONTRACT)) == 1
    assert _ledger_calls(spy) == []


def test_a_driver_error_from_the_contract_insert_propagates_unrelabelled():
    class ForeignKeyViolation(Exception):
        pass

    spy = _SpyConn([(_INSERT_CONTRACT, ForeignKeyViolation("tenants FK"))])
    with pytest.raises(ForeignKeyViolation):
        _create(spy)
    assert _ledger_calls(spy) == []


def test_party_insert_names_its_own_unique_constraint_as_the_conflict_arbiter():
    spy = _SpyConn(_party_world(provider_role=False))
    _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, "recipient_signatory")
    sql = str(_calls_matching(spy, _INSERT_PARTY)[0][0])
    assert "ON CONFLICT ON CONSTRAINT uq_dora_contract_parties_tenant_tuple DO NOTHING" in sql


def test_a_party_insert_that_returns_no_row_is_a_conflict_and_is_not_ledgered():
    responses = [r for r in _party_world(provider_role=False) if r[0] != _INSERT_PARTY]
    spy = _SpyConn(responses)
    with pytest.raises(DORAContractConflictError, match="already recorded"):
        _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, "recipient_signatory")
    assert _ledger_calls(spy) == []


def test_a_driver_error_from_the_party_insert_propagates_unrelabelled():
    class UniqueViolation(Exception):
        pass

    responses = [(_INSERT_PARTY, UniqueViolation("pkey"))] + _party_world(provider_role=False)
    spy = _SpyConn(responses)
    with pytest.raises(UniqueViolation):
        _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, "recipient_signatory")
    assert _ledger_calls(spy) == []


def test_the_conflict_error_is_not_a_value_error():
    # A handler for bad input must not be able to absorb a conflict.
    assert not issubclass(DORAContractConflictError, ValueError)


# ── Parties: references and provider roles ──────────────────────────────────


@pytest.mark.parametrize("bad", ["signatory", "consumer", "Recipient_Signatory", "", None])
def test_an_unsupported_party_role_is_refused_before_any_sql(bad):
    spy = _SpyConn()
    with pytest.raises(ValueError, match="party_role must be one of"):
        _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, bad)
    assert spy.calls == []


def test_a_recipient_signatory_needs_no_organisation_role_and_none_is_queried():
    spy = _SpyConn(_party_world(provider_role=False))
    added = _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, "recipient_signatory")
    assert added.party_role == "recipient_signatory"
    assert _calls_matching(spy, _ICT_PROVIDER_ROLE) == []


@pytest.mark.parametrize("role", ["provider_signatory", "intragroup_provider_signatory"])
def test_a_provider_signatory_without_the_ict_provider_role_is_refused_and_the_role_is_not_assigned(role):
    spy = _SpyConn(_party_world(provider_role=False))
    with pytest.raises(ValueError, match="ict_provider"):
        _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, role)
    assert _calls_matching(spy, "INSERT INTO") == []
    assert _calls_matching(spy, "UPDATE") == []


@pytest.mark.parametrize("role", ["provider_signatory", "intragroup_provider_signatory"])
def test_a_provider_signatory_holding_the_ict_provider_role_is_recorded(role):
    spy = _SpyConn(_party_world(provider_role=True))
    added = _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, role)
    assert added.party_role == role
    role_query = _calls_matching(spy, _ICT_PROVIDER_ROLE)[0]
    assert role_query[1]["role_type"] == "ict_provider"
    assert "is_active = TRUE" in str(role_query[0])


def test_adding_a_party_writes_only_the_party_row_and_its_ledger_entry():
    # Signing is not consuming: nothing else — no organisation role, no
    # service usage, no consumer — is created as a side effect.
    spy = _SpyConn(_party_world(provider_role=True))
    _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, "provider_signatory")
    writes = [str(c[0]) for c in spy.calls if str(c[0]).lstrip().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert len(writes) == 2
    assert _INSERT_PARTY in writes[0]
    assert "INSERT INTO audit_log" in writes[1]


def test_a_party_on_an_unknown_contract_is_not_found_and_nothing_is_written():
    spy = _SpyConn([(_ORG_BY_ID, _SelectResult([(_ORG_ID,)]))])
    with pytest.raises(EntryNotFoundError, match="contract"):
        _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, "recipient_signatory")
    assert _calls_matching(spy, "INSERT INTO") == []


def test_a_party_naming_an_unknown_organisation_is_not_found_and_nothing_is_written():
    spy = _SpyConn([_contract_exists()])
    with pytest.raises(EntryNotFoundError, match="organisation"):
        _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, "recipient_signatory")
    assert _calls_matching(spy, "INSERT INTO") == []


# ── Identifiers: canonical everywhere; malformed names nothing ──────────────


def test_ids_are_canonical_in_sql_in_the_result_and_in_the_ledger():
    spy = _SpyConn(_party_world(provider_role=False))
    added = add_contract_party(
        spy, _TENANT_ID.upper(), _CONTRACT_ID.upper(), uuid.UUID(_ORG_ID), "recipient_signatory",
        actor_id=_ACTOR_ID, actor_role="vciso",
    )
    params = _calls_matching(spy, _INSERT_PARTY)[0][1]
    assert (params["tenant_id"], params["contract_id"], params["organization_id"]) == (_TENANT_ID, _CONTRACT_ID, _ORG_ID)
    assert (added.tenant_id, added.contract_id, added.organization_id) == (_TENANT_ID, _CONTRACT_ID, _ORG_ID)
    after = json.loads(_ledger_calls(spy)[0][1]["after_state"])
    assert (after["tenant_id"], after["contract_id"], after["organization_id"]) == (_TENANT_ID, _CONTRACT_ID, _ORG_ID)


@pytest.mark.parametrize("malformed", ["not-a-uuid", "", None, "c0000000-0000-4000-c000-00000000000"])
def test_a_malformed_contract_id_names_nothing_and_reaches_no_sql(malformed):
    spy = _SpyConn()
    assert get_contract(spy, _TENANT_ID, malformed) is None
    assert list_contract_parties(spy, _TENANT_ID, malformed) == []
    assert _update(spy, contract_id=malformed) is None
    with pytest.raises(EntryNotFoundError):
        _write(add_contract_party, spy, malformed, _ORG_ID, "recipient_signatory")
    assert spy.calls == []


@pytest.mark.parametrize("malformed", ["not-a-uuid", "", None])
def test_a_malformed_organisation_id_names_nothing_and_reaches_no_sql(malformed):
    spy = _SpyConn()
    assert list_contracts_for_organization(spy, _TENANT_ID, malformed) == []
    with pytest.raises(EntryNotFoundError):
        _write(add_contract_party, spy, _CONTRACT_ID, malformed, "recipient_signatory")
    assert spy.calls == []


# ── Tenant guards ───────────────────────────────────────────────────────────

_ALL_OPERATIONS = [
    lambda spy, t: create_contract(spy, t, ContractInput(contract_reference="X"), actor_id=_ACTOR_ID, actor_role="vciso"),
    lambda spy, t: get_contract(spy, t, _CONTRACT_ID),
    lambda spy, t: list_contracts(spy, t),
    lambda spy, t: update_contract(spy, t, _CONTRACT_ID, ContractUpdate(None, None, None, True), actor_id=_ACTOR_ID, actor_role="vciso"),
    lambda spy, t: add_contract_party(spy, t, _CONTRACT_ID, _ORG_ID, "recipient_signatory", actor_id=_ACTOR_ID, actor_role="vciso"),
    lambda spy, t: list_contract_parties(spy, t, _CONTRACT_ID),
    lambda spy, t: list_contracts_for_organization(spy, t, _ORG_ID),
]
_OPERATION_IDS = ["create", "get", "list", "update", "add_party", "list_parties", "list_for_org"]


@pytest.mark.parametrize("missing", [None, "", "   ", "not-a-uuid", "a0000000-0000-1000-a000-000000000001"])
@pytest.mark.parametrize("call", _ALL_OPERATIONS, ids=_OPERATION_IDS)
def test_a_missing_or_invalid_tenant_fails_before_any_sql(call, missing):
    spy = _SpyConn()
    with pytest.raises(TenantContextMissingError):
        call(spy, missing)
    assert spy.calls == []


@pytest.mark.parametrize("call", _ALL_OPERATIONS, ids=_OPERATION_IDS)
def test_tenant_context_is_the_first_statement_of_every_operation(call):
    spy = _SpyConn(_party_world(provider_role=False) + [(_INSERT_CONTRACT, _inserted())])
    call(spy, _TENANT_ID)
    assert spy.calls, "operation issued no SQL at all"
    assert "SET LOCAL app.current_tenant_id" in str(spy.calls[0][0])
    assert spy.calls[0][1] == [_TENANT_ID]


@pytest.mark.parametrize("call", _ALL_OPERATIONS, ids=_OPERATION_IDS)
def test_every_tenant_read_and_update_carries_an_explicit_tenant_predicate(call):
    spy = _SpyConn(_party_world(provider_role=True) + [(_INSERT_CONTRACT, _inserted())])
    call(spy, _TENANT_ID)
    for sql, params in spy.calls:
        text = str(sql)
        if "dora_" in text and text.lstrip().startswith(("SELECT", "UPDATE")):
            assert "tenant_id = :tenant_id" in text, text
            assert params["tenant_id"] == _TENANT_ID


# ── Update: immutable identity, locking read, provenance ────────────────────


def test_the_update_input_has_no_reference_tenant_or_id_field():
    assert {f.name for f in dataclasses.fields(ContractUpdate)} == {
        "display_name", "contract_start_date", "contract_end_date", "is_active",
    }
    with pytest.raises(TypeError):
        ContractUpdate(display_name=None, contract_start_date=None, contract_end_date=None,
                       is_active=True, contract_reference="NEW-REF")


def test_the_update_statement_cannot_touch_reference_tenant_or_id():
    spy = _SpyConn([_contract_exists()])
    _update(spy, display_name="Renamed")
    sql = str(_calls_matching(spy, "UPDATE dora_contracts")[0][0])
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    for column in ("contract_reference", "tenant_id", "contract_id"):
        assert column not in set_clause


def test_the_update_keeps_the_stored_reference_and_changes_only_the_amendable_fields():
    spy = _SpyConn([_contract_exists(_contract_row(reference="MSA-2024-01"))])
    updated = _update(spy, display_name=" New name ", contract_end_date=date(2027, 1, 1), is_active=False)
    assert updated.contract_reference == "MSA-2024-01"
    assert updated.display_name == "New name"
    assert updated.contract_start_date is None
    assert updated.contract_end_date == date(2027, 1, 1)
    assert updated.is_active is False


def test_an_update_with_an_end_before_the_start_is_refused_before_any_sql():
    spy = _SpyConn([_contract_exists()])
    with pytest.raises(ValueError, match="before contract_start_date"):
        _update(spy, contract_start_date=date(2025, 6, 1), contract_end_date=date(2025, 5, 31))
    assert spy.calls == []


def test_update_locks_the_row_then_writes_then_takes_the_ledger_lock():
    spy = _SpyConn([_contract_exists()])
    _update(spy, display_name="After")
    order = [str(c[0]) for c in spy.calls]
    locking_read = _position_of(order, "FOR NO KEY UPDATE")
    update_at = _position_of(order, "UPDATE dora_contracts")
    advisory_at = _position_of(order, "pg_advisory_xact_lock")
    assert locking_read < update_at < advisory_at
    assert "FROM dora_contracts" in order[locking_read]
    assert _row_locking_statements(spy) == [order[locking_read]]
    assert order[locking_read].rstrip().endswith("FOR NO KEY UPDATE")


def test_update_reads_the_contract_once_and_ledgers_what_the_locked_read_returned():
    locked = _contract_row(display_name="Locked Read Name")
    plain = _contract_row(display_name="Plain Read Name")
    spy = _SpyConn([("FOR NO KEY UPDATE", _SelectResult([locked])), (_CONTRACT_BY_ID, _SelectResult([plain]))])
    _update(spy, display_name="After")
    reads = [c for c in spy.calls if str(c[0]).lstrip().startswith("SELECT") and "FROM dora_contracts" in str(c[0])]
    assert len(reads) == 1
    before = json.loads(_ledger_calls(spy)[0][1]["before_state"])
    assert before["display_name"] == "Locked Read Name"


def test_update_of_a_contract_this_tenant_lacks_returns_none_and_writes_nothing():
    spy = _SpyConn()
    assert _update(spy) is None
    assert _calls_matching(spy, "UPDATE dora_contracts") == []
    assert _ledger_calls(spy) == []


@pytest.mark.parametrize("call", [c for i, c in enumerate(_ALL_OPERATIONS) if _OPERATION_IDS[i] != "update"],
                         ids=[i for i in _OPERATION_IDS if i != "update"])
def test_no_other_operation_takes_a_row_lock(call):
    spy = _SpyConn(_party_world(provider_role=True) + [(_INSERT_CONTRACT, _inserted())])
    call(spy, _TENANT_ID)
    assert _row_locking_statements(spy) == []


# ── Ledger ──────────────────────────────────────────────────────────────────


def test_create_ledgers_the_created_contract_after_the_insert():
    spy = _SpyConn([(_INSERT_CONTRACT, _inserted())])
    created = _create(spy, contract_reference="MSA-1", contract_start_date=date(2024, 3, 1))
    order = [str(c[0]) for c in spy.calls]
    assert _position_of(order, _INSERT_CONTRACT) < _position_of(order, "INSERT INTO audit_log")
    params = _ledger_calls(spy)[0][1]
    assert params["action_type"] == ACTION_CONTRACT_CREATED
    assert params["object_type"] == OBJECT_TYPE_CONTRACT
    assert params["object_id"] == created.contract_id
    assert params["actor_id"] == str(_ACTOR_ID)
    assert params["control_id"] is None
    assert params["before_state"] is None
    after = json.loads(params["after_state"])
    assert after["contract_reference"] == "MSA-1"
    assert after["contract_start_date"] == "2024-03-01"
    assert after["contract_end_date"] is None


def test_update_ledgers_before_and_after_state():
    spy = _SpyConn([_contract_exists()])
    _update(spy, display_name="After", is_active=False)
    params = _ledger_calls(spy)[0][1]
    assert params["action_type"] == ACTION_CONTRACT_UPDATED
    before = json.loads(params["before_state"])
    after = json.loads(params["after_state"])
    assert (before["display_name"], after["display_name"]) == ("Hosting MSA", "After")
    assert (before["is_active"], after["is_active"]) == (True, False)
    assert before["contract_reference"] == after["contract_reference"] == "MSA-2024-01"


def test_party_ledger_uses_the_party_vocabulary():
    spy = _SpyConn(_party_world(provider_role=False))
    added = _write(add_contract_party, spy, _CONTRACT_ID, _ORG_ID, "recipient_signatory")
    params = _ledger_calls(spy)[0][1]
    assert params["action_type"] == ACTION_CONTRACT_PARTY_ADDED
    assert params["object_type"] == OBJECT_TYPE_CONTRACT_PARTY
    assert params["object_id"] == added.contract_party_id
    assert json.loads(params["after_state"])["party_role"] == "recipient_signatory"


def test_ledger_timestamps_are_utc_whatever_zone_the_row_came_back_in():
    session_zone = timezone(timedelta(hours=2))
    row = (_CONTRACT_ID, _TENANT_ID, "MSA-1", None, None, None, True,
           _NOW.astimezone(session_zone), _NOW.astimezone(session_zone))
    spy = _SpyConn([(_CONTRACT_BY_ID, _SelectResult([row]))])
    _update(spy)
    before = json.loads(_ledger_calls(spy)[0][1]["before_state"])
    assert before["created_at"] == "2026-09-24T12:00:00+00:00"
    assert before["updated_at"] == "2026-09-24T12:00:00+00:00"


def test_refused_input_never_reaches_the_ledger():
    spy = _SpyConn()
    with pytest.raises(ValueError):
        _create(spy, contract_reference="")
    assert _ledger_calls(spy) == []


# ── Authorisation constant ──────────────────────────────────────────────────


def test_contract_write_roles_are_the_filing_authority_roles():
    assert [r.value for r in CONTRACT_CAPABLE_ROLES] == ["compliance_lead", "vciso"]
