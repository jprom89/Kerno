"""Static tests of migration 030: the runtime's own grant-profile lookup and its one-line grant addition.

What:  Runs migration 030's upgrade and downgrade against a recording op and
       checks every statement. The lookup is a fixed, argument-free, read-only
       SECURITY DEFINER SQL function with a pinned search_path that reads only
       the calling login's row of the owner-only assignment record, with
       EXECUTE revoked from PUBLIC. The replaced apply_runtime_grants is
       migration 029's own text with exactly one added line: the lookup's
       EXECUTE grant, right after the H1 grant in the array both profiles
       share. Only the recorded assignment is re-applied. The downgrade
       restores 029's procedure exactly and drops the lookup. Nothing else is
       granted, and no role, database setting, FORCE flag or policy changes.
Why:   PILOT-READY-003 (issue #20, gate G10). A pilot app must refuse to serve
       unless the database assigns its own login the pilot grants, without the
       app reading runtime_assignment, holding the owner's URL or naming a
       role. This migration's privileged diff must be exactly that one grant,
       provably. Nothing here connects to a database.
How:   pytest tests/unit/models/test_runtime_profile_lookup_migration.py -v
"""

from __future__ import annotations

import difflib
import re

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests import _database_safety as safety
from tests.unit.models.test_runtime_bootstrap_migration import (
    _GRANT_SHAPE,
    EXPECTED_FULL_ONLY_GRANTS,
    EXPECTED_SHARED_GRANTS,
    NEVER_GRANTED,
    SEARCH_PATH,
    _bodies,
    _load,
    _normalised,
    _outside_bodies,
    _RecordingOp,
)

_VERSIONS = safety.REPOSITORY_ROOT / "migrations" / "versions"
_MIGRATION_029 = _VERSIONS / "029_create_runtime_bootstrap_and_grant_mechanism.py"
_MIGRATION_030 = _VERSIONS / "030_add_session_runtime_profile_lookup.py"

REVISION = "e6f7g8h9"
DOWN_REVISION = "d5e6f7g8"
LOOKUP = "kerno_bootstrap.session_runtime_profile()"
H1_GRANT = "GRANT EXECUTE ON FUNCTION kerno_bootstrap.resolve_login(text, text) TO %I"
LOOKUP_GRANT = f"GRANT EXECUTE ON FUNCTION {LOOKUP} TO %I"
H1_GRANT_LINE = f"    '{H1_GRANT}',\n"
LOOKUP_GRANT_LINE = f"    '{LOOKUP_GRANT}',\n"
CREATE_APPLY = "CREATE PROCEDURE kerno_bootstrap.apply_runtime_grants("
REPLACE_APPLY = "CREATE OR REPLACE PROCEDURE kerno_bootstrap.apply_runtime_grants("

LOOKUP_BODY = "SELECT a.profile FROM kerno_bootstrap.runtime_assignment AS a WHERE a.role_name = session_user"
REAPPLY_BLOCK = (
    "DO $$ DECLARE recorded kerno_bootstrap.runtime_assignment%ROWTYPE; BEGIN "
    "SELECT * INTO recorded FROM kerno_bootstrap.runtime_assignment; IF FOUND THEN "
    "CALL kerno_bootstrap.apply_runtime_grants(recorded.role_name, recorded.profile); END IF; END $$;"
)


def _migration_030():
    """Import migration 030 by path, freshly, so a monkeypatched helper never leaks into another test."""
    return _load(_MIGRATION_030, "migration_030_under_test")


def _recorded(step_name: str, migration=None) -> list[str]:
    """Run one migration-030 step against a recording op and return its statements exactly as given."""
    migration = migration or _migration_030()
    recorder = _RecordingOp()
    migration.op = recorder
    getattr(migration, step_name)()
    return recorder.statements


def _procedure_029() -> str:
    """Migration 029's apply_runtime_grants text, loaded here independently of migration 030's own loader."""
    return _load(_MIGRATION_029, "migration_029_for_030_tests")._CREATE_APPLY_PROCEDURE


def _line_changes(before: str, after: str) -> list[tuple[str, int, list[str], list[str]]]:
    """Every line-level difference from before to after, as (tag, line index in after, removed lines, added lines)."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    changes = []
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag != "equal":
            removed = before_lines[before_start:before_end]
            changes.append((tag, after_start, removed, after_lines[after_start:after_end]))
    return changes


def _grant_array(procedure: str, name: str) -> list[str]:
    """The elements of one text[] grant array declared in an apply_runtime_grants text."""
    declaration = re.search(rf"{name} text\[\] := ARRAY\[(.*?)\];", procedure, re.DOTALL)
    assert declaration, name
    return re.findall(r"'((?:[^']|'')*)'", declaration.group(1))


def _with_lookup_after_h1(grants: list[str]) -> list[str]:
    """grants with the lookup's EXECUTE grant inserted right after the H1 grant."""
    extended = list(grants)
    extended.insert(extended.index(H1_GRANT) + 1, LOOKUP_GRANT)
    return extended


def _returning(text: str):
    """Return a stand-in for migration_029_apply_procedure that yields text."""
    def apply_procedure() -> str:
        """Return the scripted migration-029 text."""
        return text
    return apply_procedure


def _altered_029_texts() -> list:
    """029 procedure texts that are not the reviewed one, each of which the builders must refuse."""
    original = _procedure_029()
    return [
        pytest.param(original.replace(H1_GRANT_LINE, ""), id="H1 grant line missing"),
        pytest.param(original.replace(H1_GRANT_LINE, H1_GRANT_LINE * 2), id="H1 grant line twice"),
        pytest.param(original.replace(H1_GRANT_LINE, "\t" + H1_GRANT_LINE.lstrip()), id="H1 grant line re-indented"),
        pytest.param(original.replace(CREATE_APPLY, REPLACE_APPLY), id="already CREATE OR REPLACE"),
        pytest.param(original + original, id="procedure twice"),
        pytest.param("", id="empty"),
    ]


# ── Revision chain ──────────────────────────────────────────────────────────


def test_migration_030_sits_on_029_and_is_the_single_head():
    migration = _migration_030()
    identity = (migration.revision, migration.down_revision, migration.branch_labels, migration.depends_on)
    assert identity == (REVISION, DOWN_REVISION, None, None)
    config = Config()
    config.set_main_option("script_location", str(safety.REPOSITORY_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [REVISION]
    assert script.get_revision(REVISION).down_revision == DOWN_REVISION


def test_upgrade_runs_the_lookup_the_replaced_procedure_and_the_reapply_and_nothing_else():
    prefixes = [
        f"CREATE FUNCTION {LOOKUP}", f"{REPLACE_APPLY}p_role name, p_profile text)", "DO $$",
    ]
    statements = [_normalised(sql) for sql in _recorded("upgrade")]
    assert len(statements) == len(prefixes)
    for statement, prefix in zip(statements, prefixes):
        assert statement.startswith(prefix), prefix


# ── The lookup ──────────────────────────────────────────────────────────────


def test_the_lookup_is_an_argument_free_pinned_security_definer_sql_function_revoked_from_public():
    statement = _recorded("upgrade")[0]
    assert _normalised(_outside_bodies(statement)) == (
        f"CREATE FUNCTION {LOOKUP} RETURNS text LANGUAGE sql STABLE SECURITY DEFINER {SEARCH_PATH} AS ; "
        f"REVOKE ALL ON FUNCTION {LOOKUP} FROM PUBLIC;"
    )
    assert re.findall(r"session_runtime_profile\(([^)]*)\)", statement) == ["", ""]
    assert "OR REPLACE" not in statement


def test_the_lookup_reads_only_the_calling_logins_row_of_the_assignment_record():
    statement = _recorded("upgrade")[0]
    assert _bodies(statement) == [LOOKUP_BODY]
    body = _bodies(statement)[0]
    assert re.findall(r"\b(?:FROM|JOIN)\s+([\w.]+)", body) == ["kerno_bootstrap.runtime_assignment"]
    # session_user is the authenticated login; inside SECURITY DEFINER current_user is the owner.
    assert body.endswith("WHERE a.role_name = session_user") and "current_user" not in body
    for forbidden in (
        r"\bJOIN\b", r"\bUNION\b", r"\b(INSERT|UPDATE|DELETE|TRUNCATE)\b", r"\bset_config\b", r"\bcurrent_setting\b",
        r"\bEXECUTE\b", r"\bformat\s*\(", r"\bplpgsql\b", r"\bGRANT\b", r"\bpg_authid\b", r"\bpassword\b",
        r"\btenant", r"\bpublic\.", r"\$\d",
    ):
        assert not re.search(forbidden, statement, re.IGNORECASE), forbidden


# ── The replaced apply_runtime_grants: 029's text plus one line ─────────────


def test_the_replaced_procedure_is_029s_text_as_create_or_replace_with_only_the_lookup_grant_added_after_h1():
    procedure = _recorded("upgrade")[1]
    original = _procedure_029()
    assert original.count(CREATE_APPLY) == 1
    changes = _line_changes(original.replace(CREATE_APPLY, REPLACE_APPLY), procedure)
    assert [(tag, removed, added) for tag, _, removed, added in changes] == [("insert", [], [LOOKUP_GRANT_LINE])]
    inserted_at = changes[0][1]
    assert procedure.splitlines(keepends=True)[inserted_at - 1] == H1_GRANT_LINE
    assert procedure.count(LOOKUP_GRANT) == 1


def test_against_029s_untouched_text_the_only_changes_are_create_or_replace_and_the_one_grant():
    procedure = _recorded("upgrade")[1]
    original = _procedure_029()
    create_line = next(line for line in original.splitlines(keepends=True) if line.startswith(CREATE_APPLY))
    changes = [(tag, removed, added) for tag, _, removed, added in _line_changes(original, procedure)]
    assert changes == [
        ("replace", [create_line], [create_line.replace(CREATE_APPLY, REPLACE_APPLY)]),
        ("insert", [], [LOOKUP_GRANT_LINE]),
    ]


def test_the_lookup_grant_is_in_the_shared_array_so_both_profiles_get_it_and_nothing_else_changes():
    procedure = _recorded("upgrade")[1]
    shared = _grant_array(procedure, "shared_grants")
    full_only = _grant_array(procedure, "full_only_grants")
    assert shared == _with_lookup_after_h1(EXPECTED_SHARED_GRANTS)
    assert full_only == EXPECTED_FULL_ONLY_GRANTS
    original = _procedure_029()
    assert shared == _with_lookup_after_h1(_grant_array(original, "shared_grants"))
    assert full_only == _grant_array(original, "full_only_grants")
    body = _bodies(procedure)[0]
    # The shared loop runs for every profile; only the full-only loop is conditional.
    assert (
        "CALL kerno_bootstrap.revoke_runtime_grants(p_role); FOREACH grant_sql IN ARRAY shared_grants LOOP "
        "EXECUTE format(grant_sql, p_role); END LOOP; IF p_profile = 'full' THEN "
        "FOREACH grant_sql IN ARRAY full_only_grants LOOP"
    ) in body


def test_the_added_grant_is_a_plain_execute_on_the_lookup_and_no_grant_names_a_forbidden_object():
    shape = _GRANT_SHAPE.match(LOOKUP_GRANT)
    assert (shape.group("privileges"), shape.group("object")) == ("EXECUTE", f"FUNCTION {LOOKUP}")
    procedure = _recorded("upgrade")[1]
    every_grant = _grant_array(procedure, "shared_grants") + _grant_array(procedure, "full_only_grants")
    for name in NEVER_GRANTED:
        for grant in every_grant:
            assert not re.search(rf"\b{name}\b", grant), grant


def test_revoke_runtime_grants_needs_no_change_because_it_already_withdraws_every_kerno_bootstrap_function():
    revoke_procedure = _load(_MIGRATION_029, "migration_029_revoke_for_030_tests")._CREATE_REVOKE_PROCEDURE
    assert "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA kerno_bootstrap FROM %I" in revoke_procedure
    assert LOOKUP.startswith("kerno_bootstrap.")
    assert not any("revoke_runtime_grants(p_role name)" in sql for sql in _recorded("upgrade"))


# ── Re-applying the recorded assignment ─────────────────────────────────────


def test_the_reapply_block_calls_apply_runtime_grants_only_for_the_recorded_role_and_profile():
    block = _normalised(_recorded("upgrade")[2])
    assert block == REAPPLY_BLOCK
    calls = re.findall(r"\bCALL\s+([\w.]+)\(([^)]*)\)", block)
    assert calls == [("kerno_bootstrap.apply_runtime_grants", "recorded.role_name, recorded.profile")]
    assert not re.search(r"'(full|pilot)'|\bkerno_test(_app)?\b", block)


# ── Nothing else: no top-level grant, role, setting, FORCE or policy ────────


def test_upgrade_executes_no_grant_outside_the_replaced_procedure_body():
    statements = _recorded("upgrade")
    for statement in statements:
        assert not re.search(r"\bGRANT\b", _outside_bodies(statement)), _normalised(statement)[:80]
    assert [sql for sql in statements if re.search(r"\bGRANT\b", sql)] == [statements[1]]


@pytest.mark.parametrize("step_name", ["upgrade", "downgrade"])
def test_neither_step_touches_a_role_a_database_setting_force_or_a_policy(step_name):
    text = " ".join(_normalised(sql) for sql in _recorded(step_name))
    for forbidden in (
        r"\b(CREATE|ALTER|DROP)\s+(ROLE|USER|DATABASE)\b", r"\bFORCE\s+ROW\s+LEVEL\s+SECURITY\b", r"\bNO\s+FORCE\b",
        r"\bPOLICY\b", r"\bALTER\s+(TABLE|FUNCTION|PROCEDURE|SCHEMA)\b", r"\bOWNER\s+TO\b",
        r"\b(ENABLE|DISABLE)\s+(ROW\s+LEVEL\s+SECURITY|TRIGGER)\b", r"\bRESET\b", r"\bSET\s+ROLE\b",
        r"\bALTER\s+DEFAULT\s+PRIVILEGES\b", r"\bDROP\s+OWNED\b", r"\bREASSIGN\b", r"\bTRUNCATE\b", r"\bkerno_dev\b",
        r"\bkerno_test(_app)?\b", r"\bREVOKE\s+CONNECT\b",
    ):
        assert not re.search(forbidden, text, re.IGNORECASE), forbidden


# ── Downgrade ───────────────────────────────────────────────────────────────


def test_downgrade_restores_029s_procedure_exactly_then_drops_the_lookup():
    statements = _recorded("downgrade")
    assert len(statements) == 2
    assert statements[0] == _procedure_029().replace(CREATE_APPLY, REPLACE_APPLY)
    assert "session_runtime_profile" not in statements[0]
    assert _normalised(statements[1]) == f"DROP FUNCTION {LOOKUP};"


# ── The 029 text the builders accept ────────────────────────────────────────


def test_the_029_loader_returns_029s_text_unchanged_and_the_replacing_form_differs_only_in_its_first_keyword():
    migration = _migration_030()
    original = _procedure_029()
    assert migration.migration_029_apply_procedure() == original
    assert migration.migration_029_apply_procedure_replacing() == original.replace(CREATE_APPLY, REPLACE_APPLY)


@pytest.mark.parametrize("altered", _altered_029_texts())
def test_the_builders_refuse_a_029_text_other_than_the_reviewed_one(monkeypatch, altered):
    migration = _migration_030()
    monkeypatch.setattr(migration, "migration_029_apply_procedure", _returning(altered))
    for builder in (migration.migration_029_apply_procedure_replacing, migration.apply_procedure_with_lookup_grant):
        with pytest.raises(RuntimeError, match="not the reviewed one"):
            builder()
    downgrade_recorder = _RecordingOp()
    migration.op = downgrade_recorder
    with pytest.raises(RuntimeError, match="not the reviewed one"):
        migration.downgrade()
    assert downgrade_recorder.statements == []
    upgrade_recorder = _RecordingOp()
    migration.op = upgrade_recorder
    with pytest.raises(RuntimeError, match="not the reviewed one"):
        migration.upgrade()
    # The text is checked before the first statement, so nothing at all runs.
    assert upgrade_recorder.statements == []
