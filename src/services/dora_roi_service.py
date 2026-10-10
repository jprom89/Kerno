"""dora_roi_service.py — Live DORA Register of Information service layer.

What:  Creates, updates, and retrieves DORARegisterEntry records for a tenant's
       live ICT third-party relationships. Also provides read access to global
       DORAReportingWindow reference data. All tenant-scoped operations enforce
       Row-Level Security via set_tenant_context before any query.

Why:   KER-106 (Document 14, part 1 of 3) establishes the live-register foundation
       for the DORA Register of Information. The RoI must be continuously maintained
       rather than assembled at export time. This service is the single write path
       for RoI entries. Documents 15 and 16 will add xBRL-CSV export and authority
       submission workflows on top of this foundation.

Concurrency and lock order — recorded decision (SEC-REMED-004):
    update_register_entry reads the row with SELECT ... FOR NO KEY UPDATE,
    with explicit tenant predicates, before capturing before_state. Under
    READ COMMITTED a plain read let two amendments capture the same old row;
    the second then ledgered a before_state that was not the state it
    replaced, and the hash chain verified regardless, because the tenant's
    advisory lock inside append_audit_entry is taken after the state was
    captured. With the locking read a concurrent amendment waits for the
    first to commit and is handed the committed row. That is the whole
    guarantee: an accurate audit trail. A stale user edit is NOT rejected; it
    waits, then wins. Rejecting it would need an optimistic token (an expected
    updated_at) the API does not have. FOR NO KEY UPDATE is the lock the
    UPDATE of these non-key columns takes anyway. The after_state is the row
    the UPDATE actually stored (RETURNING), and ledger timestamps are rendered
    in UTC, so a session's time zone cannot make two equal states differ.

    Per call, the row lock is taken before the ledger advisory lock. Per
    transaction that order does not hold: the advisory lock is
    transaction-scoped, so a caller-owned transaction that has already
    ledgered holds it while a later amendment waits on a row lock; if the
    session holding that row is queued for the advisory lock, PostgreSQL
    aborts one side with DeadlockDetected (40P01). The loser commits nothing,
    the chain stays valid, and the retry is the caller's. This is the KER-107
    "business write, then ledger" limitation every ledgered service shares;
    the UPDATE already took the same row lock before the ledger lock, so the
    locking read adds no new inversion. Read-only functions take no locks.
    The service owns no transaction: the lock lives until the caller commits
    or rolls back.

How to run or test:
    pytest tests/unit/services/test_dora_roi_service.py -v
    pytest tests/integration/test_sec_remed_004_register_audit.py -m integration -v
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime, timezone

from config.constants import MAX_EXIT_SUMMARY_LENGTH, RbacRole
from src.db.rls import set_tenant_context
from src.exceptions import TenantContextMissingError  # noqa: F401  re-exported
from src.services.audit_log import append_audit_entry
from src.models.dora_register_entry import (
    CRITICALITY_CRITICAL,
    CRITICALITY_HIGH,
    CRITICALITY_STANDARD,
    PROVIDER_TYPE_CLOUD,
    PROVIDER_TYPE_MANAGED_SERVICE,
    PROVIDER_TYPE_OTHER,
    PROVIDER_TYPE_SOFTWARE,
    PROVIDER_TYPE_TELECOM,
)

# Roles allowed to create or amend register entries. The register is a filed
# regulatory artefact, so the two roles accountable for what gets filed are the
# two that may write it.
REGISTER_CAPABLE_ROLES: tuple[RbacRole, ...] = (
    RbacRole.COMPLIANCE_LEAD,
    RbacRole.VCISO,
)

# KER-107 ledger vocabulary for register writes (KER-409). Named rather than
# inlined so the two write paths cannot drift into near-miss spellings that a
# ledger query would silently miss.
ACTION_REGISTER_ENTRY_CREATED = "register_entry_created"
ACTION_REGISTER_ENTRY_UPDATED = "register_entry_updated"
OBJECT_TYPE_REGISTER_ENTRY = "register_entry"

_ALLOWED_CRITICALITY_LEVELS = {CRITICALITY_CRITICAL, CRITICALITY_HIGH, CRITICALITY_STANDARD}
_ALLOWED_PROVIDER_TYPES = {
    PROVIDER_TYPE_CLOUD,
    PROVIDER_TYPE_SOFTWARE,
    PROVIDER_TYPE_MANAGED_SERVICE,
    PROVIDER_TYPE_TELECOM,
    PROVIDER_TYPE_OTHER,
}

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RegisterEntryInput:
    """Input data for creating or updating a DORARegisterEntry."""

    provider_name: str
    service_name: str
    provider_type: str
    criticality_level: str
    business_function: str
    data_types: list[str]
    countries_supported: list[str]
    contract_start_date: date | None
    contract_end_date: date | None
    exit_strategy_summary: str | None
    is_active: bool
    source_record_id: str | None


@dataclasses.dataclass(frozen=True)
class RegisterEntryOutput:
    """Return type for all DORARegisterEntry service methods."""

    register_entry_id: str
    tenant_id: str
    provider_name: str
    service_name: str
    provider_type: str
    criticality_level: str
    business_function: str
    data_types: list[str]
    countries_supported: list[str]
    contract_start_date: date | None
    contract_end_date: date | None
    exit_strategy_summary: str | None
    is_active: bool
    source_record_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclasses.dataclass(frozen=True)
class ReportingWindowOutput:
    """Return type for list_reporting_windows."""

    reporting_window_id: str
    authority_code: str
    authority_name: str
    member_state: str
    reporting_year: int
    submission_open_date: date
    submission_close_date: date
    notes: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_INSERT_ENTRY = """
INSERT INTO dora_register_entries (
    register_entry_id, tenant_id, provider_name, service_name, provider_type,
    criticality_level, business_function, data_types, countries_supported,
    contract_start_date, contract_end_date, exit_strategy_summary, is_active,
    source_record_id, created_at, updated_at
) VALUES (
    :register_entry_id, :tenant_id, :provider_name, :service_name, :provider_type,
    :criticality_level, :business_function, :data_types, :countries_supported,
    :contract_start_date, :contract_end_date, :exit_strategy_summary, :is_active,
    :source_record_id, :created_at, :updated_at
)
"""

# The tenant predicate repeats what RLS and set_tenant_context already
# enforce, so the statement is correct on its own; RETURNING hands back the
# row as stored, which is what the ledger's after_state records.
_UPDATE_ENTRY = """
UPDATE dora_register_entries
SET provider_name = :provider_name,
    service_name = :service_name,
    provider_type = :provider_type,
    criticality_level = :criticality_level,
    business_function = :business_function,
    data_types = :data_types,
    countries_supported = :countries_supported,
    contract_start_date = :contract_start_date,
    contract_end_date = :contract_end_date,
    exit_strategy_summary = :exit_strategy_summary,
    is_active = :is_active,
    source_record_id = :source_record_id,
    updated_at = :updated_at
WHERE register_entry_id = :register_entry_id AND tenant_id = :tenant_id
RETURNING register_entry_id, tenant_id, provider_name, service_name, provider_type,
       criticality_level, business_function, data_types, countries_supported,
       contract_start_date, contract_end_date, exit_strategy_summary, is_active,
       source_record_id, created_at, updated_at
"""

_SELECT_ENTRY_BY_ID = """
SELECT register_entry_id, tenant_id, provider_name, service_name, provider_type,
       criticality_level, business_function, data_types, countries_supported,
       contract_start_date, contract_end_date, exit_strategy_summary, is_active,
       source_record_id, created_at, updated_at
FROM dora_register_entries
WHERE register_entry_id = :register_entry_id
"""

# The amendment path's own read; the getters keep the plain SELECT above and
# take no lock. See "Concurrency and lock order" in the module docstring.
_SELECT_ENTRY_FOR_AMENDMENT = """
SELECT register_entry_id, tenant_id, provider_name, service_name, provider_type,
       criticality_level, business_function, data_types, countries_supported,
       contract_start_date, contract_end_date, exit_strategy_summary, is_active,
       source_record_id, created_at, updated_at
FROM dora_register_entries
WHERE register_entry_id = :register_entry_id AND tenant_id = :tenant_id
FOR NO KEY UPDATE
"""

_BASE_SELECT_ENTRIES = """
SELECT register_entry_id, tenant_id, provider_name, service_name, provider_type,
       criticality_level, business_function, data_types, countries_supported,
       contract_start_date, contract_end_date, exit_strategy_summary, is_active,
       source_record_id, created_at, updated_at
FROM dora_register_entries"""

_BASE_SELECT_WINDOWS = """
SELECT reporting_window_id, authority_code, authority_name, member_state,
       reporting_year, submission_open_date, submission_close_date, notes, created_at
FROM dora_reporting_windows"""

_WINDOWS_ORDER = (
    " ORDER BY reporting_year DESC, submission_open_date ASC, authority_code ASC"
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_register_entry(
    conn, tenant_id, entry_input: RegisterEntryInput, *, actor_id, actor_role: str
) -> RegisterEntryOutput:
    """Validate, persist, and return a new DORARegisterEntry for the tenant.

    Guards tenant_id first, then normalizes and validates entry_input, then sets
    tenant context before the INSERT. Appends a KER-107 ledger entry naming the
    actor on the same connection, so the register line and the record of who
    added it commit or roll back together. Returns a RegisterEntryOutput built
    directly from the persisted values so no follow-up SELECT is needed.
    Raises TenantContextMissingError on bad tenant; ValueError on invalid input.
    """
    _guard_tenant(tenant_id)
    normalized = _normalize_and_validate(entry_input)
    set_tenant_context(conn, tenant_id)
    now = datetime.now(timezone.utc)
    entry_id = str(uuid.uuid4())
    conn.execute(_INSERT_ENTRY, _build_insert_params(entry_id, str(tenant_id), normalized, now))
    created = _output_from_input(entry_id, str(tenant_id), normalized, now, now)
    _record_register_ledger_entry(
        conn, tenant_id,
        actor_id=actor_id,
        actor_role=actor_role,
        action_type=ACTION_REGISTER_ENTRY_CREATED,
        register_entry_id=entry_id,
        before_state=None,
        after_state=_entry_to_ledger_state(created),
    )
    return created


def update_register_entry(
    conn, tenant_id, register_entry_id: str, entry_input: RegisterEntryInput,
    *, actor_id, actor_role: str,
) -> RegisterEntryOutput | None:
    """Update an existing DORARegisterEntry and return it as stored.

    Guards tenant_id first, then normalizes and validates entry_input, so bad
    input never reaches the database. Sets tenant context, then reads the row
    with a FOR NO KEY UPDATE lock, scoped to this tenant; if there is none,
    returns None and has written nothing. Otherwise updates it, records the
    locked row as before_state and the stored row as after_state in the
    KER-107 ledger, and returns the stored row. The lock is held until the
    caller's transaction ends, so a concurrent amendment's before_state is
    this one's committed result; that is accuracy, not stale-edit rejection.
    Raises TenantContextMissingError on bad tenant; ValueError on invalid input.
    """
    _guard_tenant(tenant_id)
    normalized = _normalize_and_validate(entry_input)
    set_tenant_context(conn, tenant_id)
    lookup = {"register_entry_id": register_entry_id, "tenant_id": str(tenant_id)}
    row = conn.execute(_SELECT_ENTRY_FOR_AMENDMENT, lookup).fetchone()
    if row is None:
        return None
    previous = _entry_row_to_output(row)
    params = _build_update_params(register_entry_id, normalized, datetime.now(timezone.utc))
    stored = conn.execute(_UPDATE_ENTRY, {**params, "tenant_id": str(tenant_id)}).fetchone()
    if stored is None:
        raise RuntimeError("the locked register entry was not updated")
    updated = _entry_row_to_output(stored)
    _record_register_ledger_entry(
        conn, tenant_id,
        actor_id=actor_id,
        actor_role=actor_role,
        action_type=ACTION_REGISTER_ENTRY_UPDATED,
        register_entry_id=register_entry_id,
        before_state=_entry_to_ledger_state(previous),
        after_state=_entry_to_ledger_state(updated),
    )
    return updated


def _record_register_ledger_entry(
    conn, tenant_id, *, actor_id, actor_role: str, action_type: str,
    register_entry_id: str, before_state: dict | None, after_state: dict,
) -> None:
    """Append the KER-107 ledger entry for one register write.

    Runs on the caller's connection and transaction so the ledger entry shares
    the fate of the row it describes. control_id is None because a register
    entry is a third-party provider record, not a control assessment.
    """
    append_audit_entry(
        conn,
        tenant_id,
        actor_id=actor_id,
        actor_role=actor_role,
        action_type=action_type,
        object_type=OBJECT_TYPE_REGISTER_ENTRY,
        object_id=register_entry_id,
        control_id=None,
        before_state=before_state,
        after_state=after_state,
    )


def _entry_to_ledger_state(entry: RegisterEntryOutput) -> dict:
    """Return a register entry as a dict the audit ledger can serialise.

    append_audit_entry json.dumps() its state fields, and json cannot encode a
    date or a datetime, so both are rendered as ISO 8601 strings. Timestamps
    are converted to UTC first: values read back from PostgreSQL carry the
    session's time zone, values generated here carry UTC, and one instant must
    read the same in both (SEC-REMED-004). Every register timestamp column is
    TIMESTAMPTZ, so a naive datetime here is a defect and raises TypeError.
    """
    state = dataclasses.asdict(entry)
    for field_name, value in state.items():
        if isinstance(value, datetime):
            state[field_name] = _utc_isoformat(value)
        elif isinstance(value, date):
            state[field_name] = value.isoformat()
    return state


def _utc_isoformat(value: datetime) -> str:
    """Return a timezone-aware datetime as an ISO 8601 string in UTC; refuse a naive one."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise TypeError("register timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def get_register_entry(
    conn, tenant_id, register_entry_id: str
) -> RegisterEntryOutput | None:
    """Return a single DORARegisterEntry by ID, or None if not found.

    Sets tenant context before querying; RLS ensures only the tenant's own
    entries are visible.
    Raises TenantContextMissingError if tenant_id is invalid.
    """
    set_tenant_context(conn, tenant_id)
    row = conn.execute(
        _SELECT_ENTRY_BY_ID, {"register_entry_id": register_entry_id}
    ).fetchone()
    return _entry_row_to_output(row) if row is not None else None


def list_register_entries(
    conn, tenant_id, criticality_level: str | None = None
) -> list[RegisterEntryOutput]:
    """Return all DORARegisterEntry rows for the tenant, optionally filtered.

    Results are ordered by updated_at DESC, then provider_name ASC.
    Pass criticality_level to restrict results to one criticality class.
    Raises TenantContextMissingError if tenant_id is invalid.
    """
    set_tenant_context(conn, tenant_id)
    sql, params = _build_list_query(criticality_level=criticality_level)
    rows = conn.execute(sql, params).fetchall()
    return [_entry_row_to_output(row) for row in rows]


def list_active_register_entries(conn, tenant_id) -> list[RegisterEntryOutput]:
    """Return only active (is_active=true) DORARegisterEntry rows for the tenant.

    Results are ordered by updated_at DESC, then provider_name ASC.
    Raises TenantContextMissingError if tenant_id is invalid.
    """
    set_tenant_context(conn, tenant_id)
    sql, params = _build_list_query(active_only=True)
    rows = conn.execute(sql, params).fetchall()
    return [_entry_row_to_output(row) for row in rows]


def list_reporting_windows(
    conn, reporting_year: int | None = None
) -> list[ReportingWindowOutput]:
    """Return global DORAReportingWindow records, optionally filtered by year.

    This method queries global reference data and deliberately does NOT call
    set_tenant_context. Reporting windows are platform-wide (no tenant_id column)
    and are readable by any authenticated user without RLS filtering.
    Results are ordered by reporting_year DESC, then submission_open_date ASC,
    then authority_code ASC.
    """
    sql, params = _build_windows_query(reporting_year)
    rows = conn.execute(sql, params).fetchall()
    return [_window_row_to_output(row) for row in rows]


# ---------------------------------------------------------------------------
# Private helpers — tenant guard
# ---------------------------------------------------------------------------


def _guard_tenant(tenant_id) -> None:
    """Raise TenantContextMissingError immediately if tenant_id is falsey.

    Must be the first call in any function that writes or reads tenant data.
    Provides a fast, consistent failure before input validation or DB access so
    a caller with a bad tenant always receives TenantContextMissingError, never
    a ValueError from input validation. set_tenant_context provides additional
    UUID-format validation for non-empty values.
    """
    if not tenant_id:
        raise TenantContextMissingError("tenant_id is required for register entry operations")


# ---------------------------------------------------------------------------
# Normalization and validation
# ---------------------------------------------------------------------------


def _normalize_and_validate(entry_input: RegisterEntryInput) -> RegisterEntryInput:
    """Return a normalized copy of entry_input, then validate it.

    Normalization trims whitespace and caps exit_strategy_summary.
    Validation raises ValueError for any rule violation.
    """
    normalized = _normalize_entry_input(entry_input)
    _validate_entry_input(normalized)
    return normalized


def _normalize_entry_input(entry_input: RegisterEntryInput) -> RegisterEntryInput:
    """Return a copy of entry_input with strings trimmed and exit_summary capped.

    provider_name, service_name, and business_function are stripped of leading
    and trailing whitespace. exit_strategy_summary is stripped and capped to
    MAX_EXIT_SUMMARY_LENGTH; an all-whitespace summary becomes None.
    """
    exit_summary = entry_input.exit_strategy_summary
    if exit_summary is not None:
        exit_summary = exit_summary.strip()
        exit_summary = exit_summary[:MAX_EXIT_SUMMARY_LENGTH] if exit_summary else None
    return dataclasses.replace(
        entry_input,
        provider_name=entry_input.provider_name.strip(),
        service_name=entry_input.service_name.strip(),
        business_function=entry_input.business_function.strip(),
        exit_strategy_summary=exit_summary,
    )


def _validate_entry_input(entry_input: RegisterEntryInput) -> None:
    """Raise ValueError if any field of entry_input violates the allowed rules.

    Checks: required strings non-empty, provider_type and criticality_level in
    allowed sets, data_types and countries_supported non-empty lists of non-empty
    strings, and contract date ordering if both dates are present.
    """
    if not entry_input.provider_name:
        raise ValueError("provider_name must not be empty")
    if not entry_input.service_name:
        raise ValueError("service_name must not be empty")
    if not entry_input.business_function:
        raise ValueError("business_function must not be empty")
    if entry_input.provider_type not in _ALLOWED_PROVIDER_TYPES:
        raise ValueError(f"provider_type '{entry_input.provider_type}' is not allowed")
    if entry_input.criticality_level not in _ALLOWED_CRITICALITY_LEVELS:
        raise ValueError(f"criticality_level '{entry_input.criticality_level}' is not allowed")
    if not entry_input.data_types or any(not s for s in entry_input.data_types):
        raise ValueError("data_types must be a non-empty list of non-empty strings")
    if not entry_input.countries_supported or any(not s for s in entry_input.countries_supported):
        raise ValueError("countries_supported must be a non-empty list of non-empty strings")
    if (
        entry_input.contract_start_date is not None
        and entry_input.contract_end_date is not None
        and entry_input.contract_end_date < entry_input.contract_start_date
    ):
        raise ValueError("contract_end_date must not be before contract_start_date")


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------


def _build_list_query(
    criticality_level: str | None = None, active_only: bool = False
) -> tuple[str, dict]:
    """Build the list-entries SELECT with optional criticality and active filters.

    Returns (sql_string, params_dict). Ordering is always updated_at DESC,
    provider_name ASC as specified in §4.6.
    """
    clauses: list[str] = []
    params: dict = {}
    if criticality_level is not None:
        clauses.append("criticality_level = :criticality_level")
        params["criticality_level"] = criticality_level
    if active_only:
        clauses.append("is_active = TRUE")
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    order_sql = " ORDER BY updated_at DESC, provider_name ASC"
    return _BASE_SELECT_ENTRIES + where_sql + order_sql, params


def _build_windows_query(reporting_year: int | None) -> tuple[str, dict]:
    """Build the reporting-windows SELECT with an optional year filter.

    Returns (sql_string, params_dict). Ordering is reporting_year DESC,
    submission_open_date ASC, authority_code ASC as specified in §4.6.
    """
    if reporting_year is None:
        return _BASE_SELECT_WINDOWS + _WINDOWS_ORDER, {}
    where = " WHERE reporting_year = :reporting_year"
    return _BASE_SELECT_WINDOWS + where + _WINDOWS_ORDER, {"reporting_year": reporting_year}


# ---------------------------------------------------------------------------
# Parameter builders
# ---------------------------------------------------------------------------


def _build_insert_params(
    entry_id: str, tenant_id: str, entry_input: RegisterEntryInput, now: datetime
) -> dict:
    """Assemble the parameter dict for the INSERT statement."""
    return {
        "register_entry_id": entry_id,
        "tenant_id": tenant_id,
        "provider_name": entry_input.provider_name,
        "service_name": entry_input.service_name,
        "provider_type": entry_input.provider_type,
        "criticality_level": entry_input.criticality_level,
        "business_function": entry_input.business_function,
        "data_types": list(entry_input.data_types),
        "countries_supported": list(entry_input.countries_supported),
        "contract_start_date": entry_input.contract_start_date,
        "contract_end_date": entry_input.contract_end_date,
        "exit_strategy_summary": entry_input.exit_strategy_summary,
        "is_active": entry_input.is_active,
        "source_record_id": entry_input.source_record_id,
        "created_at": now,
        "updated_at": now,
    }


def _build_update_params(
    register_entry_id: str, entry_input: RegisterEntryInput, now: datetime
) -> dict:
    """Assemble the parameter dict for the UPDATE statement."""
    return {
        "register_entry_id": register_entry_id,
        "provider_name": entry_input.provider_name,
        "service_name": entry_input.service_name,
        "provider_type": entry_input.provider_type,
        "criticality_level": entry_input.criticality_level,
        "business_function": entry_input.business_function,
        "data_types": list(entry_input.data_types),
        "countries_supported": list(entry_input.countries_supported),
        "contract_start_date": entry_input.contract_start_date,
        "contract_end_date": entry_input.contract_end_date,
        "exit_strategy_summary": entry_input.exit_strategy_summary,
        "is_active": entry_input.is_active,
        "source_record_id": entry_input.source_record_id,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# Row-to-dataclass converters
# ---------------------------------------------------------------------------


def _output_from_input(
    entry_id: str,
    tenant_id: str,
    entry_input: RegisterEntryInput,
    created_at: datetime,
    updated_at: datetime,
) -> RegisterEntryOutput:
    """Build a RegisterEntryOutput directly from normalized input and timestamps.

    Used by create_register_entry, where both timestamps are now, to avoid a
    SELECT after the INSERT. update_register_entry does not use it: an
    amendment returns and ledgers the row its UPDATE ... RETURNING stored.
    """
    return RegisterEntryOutput(
        register_entry_id=entry_id,
        tenant_id=tenant_id,
        provider_name=entry_input.provider_name,
        service_name=entry_input.service_name,
        provider_type=entry_input.provider_type,
        criticality_level=entry_input.criticality_level,
        business_function=entry_input.business_function,
        data_types=list(entry_input.data_types),
        countries_supported=list(entry_input.countries_supported),
        contract_start_date=entry_input.contract_start_date,
        contract_end_date=entry_input.contract_end_date,
        exit_strategy_summary=entry_input.exit_strategy_summary,
        is_active=entry_input.is_active,
        source_record_id=entry_input.source_record_id,
        created_at=created_at,
        updated_at=updated_at,
    )


def _entry_row_to_output(row) -> RegisterEntryOutput:
    """Map a dora_register_entries SELECT result row (by position) to RegisterEntryOutput."""
    return RegisterEntryOutput(
        register_entry_id=str(row[0]),
        tenant_id=str(row[1]),
        provider_name=row[2],
        service_name=row[3],
        provider_type=row[4],
        criticality_level=row[5],
        business_function=row[6],
        data_types=list(row[7]) if row[7] is not None else [],
        countries_supported=list(row[8]) if row[8] is not None else [],
        contract_start_date=row[9],
        contract_end_date=row[10],
        exit_strategy_summary=row[11],
        is_active=row[12],
        source_record_id=row[13],
        created_at=row[14],
        updated_at=row[15],
    )


def _window_row_to_output(row) -> ReportingWindowOutput:
    """Map a dora_reporting_windows SELECT result row (by position) to ReportingWindowOutput."""
    return ReportingWindowOutput(
        reporting_window_id=str(row[0]),
        authority_code=row[1],
        authority_name=row[2],
        member_state=row[3],
        reporting_year=row[4],
        submission_open_date=row[5],
        submission_close_date=row[6],
        notes=row[7],
        created_at=row[8],
    )
