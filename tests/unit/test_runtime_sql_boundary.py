"""Structural test: no application SQL reads users or a webhook signing secret directly.

What:  Scans every string literal in src/**/*.py, except docstrings, and checks
       that none reads the users table, none reads signing_secret from
       webhook_registrations, and that the two kerno_bootstrap helpers are
       named only by auth_service.py and webhook_service.py.
Why:   C2 (PILOT-READY-002, docs/pilot_readiness_kickoff.md §5 item 3). The
       runtime login holds no grant on users and none on signing_secret, so
       such a query would fail in production while passing every owner-run
       test. All application SQL lives in string literals, so every
       non-docstring literal is treated as possible SQL and matched without
       regard to case; comments never reach the AST. A prose string that
       trips a rule is a cheap, visible failure; a missed query is not.
How:   pytest tests/unit/test_runtime_sql_boundary.py -v
"""

from __future__ import annotations

import ast
import re

import pytest

from tests import _database_safety as safety

_SOURCE_ROOT = safety.REPOSITORY_ROOT / "src"

_READS_USERS = re.compile(r"\b(?:FROM|JOIN)\s+(?:public\.)?users\b", re.IGNORECASE)
_READS_REGISTRATIONS = re.compile(r"\b(?:FROM|JOIN)\s+(?:public\.)?webhook_registrations\b", re.IGNORECASE)
_SELECTS_EVERY_COLUMN = re.compile(r"\bSELECT\s+(?:\w+\.)?\*", re.IGNORECASE)
_BOOTSTRAP_OBJECT = re.compile(r"\bkerno_bootstrap\.(\w+)", re.IGNORECASE)

BOOTSTRAP_HELPER_CALLERS = {
    ("src/services/auth_service.py", "resolve_login"),
    ("src/services/webhook_service.py", "webhook_verification"),
}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The ids of the module, class and function docstring nodes in tree."""
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            docstrings.add(id(first.value))
    return docstrings


def _render_f_string(node: ast.JoinedStr) -> str:
    """An f-string's literal text with {} in place of each formatted field."""
    parts = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            parts.append(part.value)
        else:
            parts.append("{}")
    return "".join(parts)


def string_literals(source: str) -> list[str]:
    """Every string literal in source except docstrings; an f-string counts as one literal."""
    tree = ast.parse(source)
    skipped = _docstring_nodes(tree)
    literals = []
    # ast.walk is breadth-first, so an f-string is seen before the constants inside it.
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skipped.update(id(part) for part in node.values)
            literals.append(_render_f_string(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skipped:
            literals.append(node.value)
    return literals


def src_string_literals() -> dict[str, list[str]]:
    """The non-docstring string literals of every module under src/, by repository-relative path."""
    literals = {}
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(safety.REPOSITORY_ROOT).as_posix()
        literals[relative] = string_literals(path.read_text(encoding="utf-8"))
    return literals


def reads_users(sql: str) -> bool:
    """True when sql reads the users table directly."""
    return bool(_READS_USERS.search(sql))


def reads_signing_secret(sql: str) -> bool:
    """True when sql reads webhook_registrations naming signing_secret or every column, or returns signing_secret."""
    lowered = sql.lower()
    if "webhook_registrations" not in lowered:
        return False
    if _READS_REGISTRATIONS.search(sql) and ("signing_secret" in lowered or _SELECTS_EVERY_COLUMN.search(sql)):
        return True
    returned = lowered.partition("returning")[2]
    return "signing_secret" in returned or "*" in returned


def _offenders(rule) -> list[tuple[str, str]]:
    """Every (path, literal) in src/ that the rule flags."""
    offenders = []
    for path, literals in src_string_literals().items():
        for literal in literals:
            if rule(literal):
                offenders.append((path, " ".join(literal.split())))
    return offenders


def _bootstrap_objects_named() -> set[tuple[str, str]]:
    """Every (path, object) for each kerno_bootstrap object a literal in src/ names."""
    named = set()
    for path, literals in src_string_literals().items():
        for literal in literals:
            for name in _BOOTSTRAP_OBJECT.findall(literal):
                named.add((path, name.lower()))
    return named


# ── The boundary over src/ ──────────────────────────────────────────────────


def test_no_application_sql_reads_the_users_table():
    assert _offenders(reads_users) == []


def test_no_application_sql_reads_a_webhook_signing_secret():
    assert _offenders(reads_signing_secret) == []


def test_the_bootstrap_helpers_are_named_only_by_their_two_callers():
    helpers = {name for _, name in BOOTSTRAP_HELPER_CALLERS}
    named = {(path, name) for path, name in _bootstrap_objects_named() if name in helpers}
    assert named == BOOTSTRAP_HELPER_CALLERS


def test_no_other_kerno_bootstrap_object_is_named_in_application_code():
    # The procedures and the assignment record are owner-only; the runtime can
    # never use them, so application code has no reason to name them.
    assert _bootstrap_objects_named() == BOOTSTRAP_HELPER_CALLERS


# ── The scanner and the rules themselves ────────────────────────────────────


def test_the_scanner_skips_docstrings_and_comments_and_keeps_every_other_literal():
    source = (
        '"""Module docstring: SELECT * FROM users."""\n'
        "# SELECT * FROM users\n"
        "class Reader:\n"
        '    """Class docstring: FROM users."""\n'
        "    def read(self):\n"
        '        """Method docstring: JOIN users."""\n'
        '        return "SELECT email FROM users"\n'
    )
    assert string_literals(source) == ["SELECT email FROM users"]


def test_the_scanner_reads_an_f_string_as_one_literal():
    source = 'QUERY = f"SELECT {columns} FROM public.users WHERE id = {user_id}"\n'
    assert string_literals(source) == ["SELECT {} FROM public.users WHERE id = {}"]
    assert reads_users(string_literals(source)[0])


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT user_id FROM users WHERE email = :email", True),
        ("select u.email from public.users u", True),
        ("SELECT t.tenant_slug FROM tenants t JOIN users u ON u.tenant_id = t.tenant_id", True),
        ("SELECT user_id, tenant_id FROM kerno_bootstrap.resolve_login(:tenant_slug, :email)", False),
        ("SELECT * FROM users_archive", False),
        ("the login holds no grant on users", False),
    ],
)
def test_the_users_rule(sql, expected):
    assert reads_users(sql) is expected


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT tenant_id, signing_secret, is_active FROM webhook_registrations WHERE id = :id", True),
        ("SELECT * FROM webhook_registrations", True),
        ("SELECT r.* FROM webhook_registrations AS r", True),
        ("SELECT id FROM webhook_registrations WHERE signing_secret = :guess", True),
        ("UPDATE webhook_registrations SET signing_secret = :new WHERE id = :id RETURNING signing_secret", True),
        ("UPDATE webhook_registrations SET signing_secret = :new WHERE id = :id RETURNING *", True),
        ("UPDATE webhook_registrations SET signing_secret = :new WHERE id = :id RETURNING id", False),
        ("INSERT INTO webhook_registrations (id, tenant_id, source_system, signing_secret) VALUES (:a, :b, :c, :d)",
         False),
        ("SELECT id, tenant_id, source_system, created_at, is_active FROM webhook_registrations WHERE id = :id", False),
        ("SELECT tenant_id, signing_secret, is_active FROM kerno_bootstrap.webhook_verification(:id)", False),
    ],
)
def test_the_signing_secret_rule(sql, expected):
    assert reads_signing_secret(sql) is expected
