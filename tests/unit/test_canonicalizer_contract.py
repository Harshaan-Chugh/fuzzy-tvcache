"""Acceptance tests for the SQL and filesystem canonicalizers.

Source of truth: the worked-examples table and decisions in docs/interfaces.md.
These tests skip until the implementations exist:

- `fuzzytv.sql.SqlCanonicalizer` (Kevin), receiving `sql_call(query)`.
- `fuzzytv.fs.FsCanonicalizer` (Etienne), receiving `bash_call(command)`.

Both must have no-argument constructors. The call-dict schema lives in
`fuzzytv.eval.calls`. Change it there if the team settles on another one.

Exact canonical strings are pinned only where docs/interfaces.md pins them.
Elsewhere the tests check equivalence (same canonical) and non-equivalence
(different canonical), so the implementer chooses the normal form.
Tests marked `stretch` cover later goals (alias normalization) and are
non-strict xfails until they pass.
"""

import importlib
from typing import Any

import pytest

from fuzzytv.eval.calls import bash_call, sql_call
from fuzzytv.interfaces import (
    CWD,
    CallAnalysis,
    CallContext,
    fs_dir,
    fs_file,
    sql_table,
)

APP = CallContext(cwd="/app", database="main")
DATA = CallContext(cwd="/app/data", database="main")
NO_CWD = CallContext(cwd=None, database="main")


def _load(module: str, name: str) -> Any:
    try:
        cls = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        pytest.skip(f"{module}.{name} is not implemented yet")
    return cls()


@pytest.fixture
def sql() -> Any:
    return _load("fuzzytv.sql", "SqlCanonicalizer")


@pytest.fixture
def fs() -> Any:
    return _load("fuzzytv.fs", "FsCanonicalizer")


def analyze_sql(sql: Any, query: str, ctx: CallContext = APP) -> CallAnalysis:
    result = sql.analyze(sql_call(query), ctx)
    assert isinstance(result, CallAnalysis)
    return result


def analyze_fs(fs: Any, command: str, ctx: CallContext = APP) -> CallAnalysis:
    result = fs.analyze(bash_call(command), ctx)
    assert isinstance(result, CallAnalysis)
    return result


USERS = sql_table("main", "users")
EMPLOYEES = sql_table("main", "employees")


# --------------------------------------------------------------------- SQL


def test_sql_select_pinned_canonical(sql: Any) -> None:
    a = analyze_sql(sql, "SELECT name FROM users WHERE id=1")
    assert a.canonical == "SELECT name FROM users WHERE id = 1"
    assert a.reads == {USERS}
    assert a.writes == frozenset()
    assert a.fuzzy_eligible


@pytest.mark.parametrize(
    "variant",
    [
        "select name\nfrom users where id=1",
        "SELECT  name  FROM  users  WHERE  id = 1",
        "SELECT name FROM users WHERE id = 1;",
        "  Select name From USERS Where id=1  ",
    ],
)
def test_sql_formatting_variants_are_equivalent(sql: Any, variant: str) -> None:
    base = analyze_sql(sql, "SELECT name FROM users WHERE id=1")
    other = analyze_sql(sql, variant)
    assert other.canonical == base.canonical
    assert other.reads == base.reads
    assert other.fuzzy_eligible


@pytest.mark.parametrize(
    "different",
    [
        "SELECT name FROM users WHERE id=2",
        "SELECT email FROM users WHERE id=1",
        "SELECT name FROM users WHERE id=1 OR id=2",
        "SELECT name FROM customers WHERE id=1",
    ],
)
def test_sql_semantic_differences_are_not_equivalent(sql: Any, different: str) -> None:
    base = analyze_sql(sql, "SELECT name FROM users WHERE id=1")
    assert analyze_sql(sql, different).canonical != base.canonical


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("SELECT SUM(amount) FROM orders", "select sum(amount) from orders"),
        ("SELECT name FROM users", "SELECT Name FROM users"),
        ("SELECT COUNT(*) FROM users", "SELECT count(*) FROM users"),
    ],
)
def test_sql_output_column_labels_are_preserved(sql: Any, first: str, second: str) -> None:
    # SQLite labels an unaliased result column with the select-list text as
    # typed, and the label is part of the returned value. Found by the
    # evaluation harness: treating these as equivalent produced false hits.
    assert analyze_sql(sql, first).canonical != analyze_sql(sql, second).canonical


def test_sql_string_literal_case_is_preserved(sql: Any) -> None:
    upper = analyze_sql(sql, "SELECT id FROM users WHERE name='Bob'")
    lower = analyze_sql(sql, "SELECT id FROM users WHERE name='bob'")
    assert upper.canonical != lower.canonical


def test_sql_update_reads_and_writes_its_table(sql: Any) -> None:
    a = analyze_sql(sql, "UPDATE employees SET salary=1 WHERE id=1")
    assert a.canonical is not None
    assert a.reads == {EMPLOYEES}
    assert a.writes == {EMPLOYEES}
    assert not a.fuzzy_eligible


def test_sql_join_reads_both_tables(sql: Any) -> None:
    a = analyze_sql(sql, "SELECT * FROM a JOIN b ON a.id = b.id")
    assert a.reads == {sql_table("main", "a"), sql_table("main", "b")}
    assert a.writes == frozenset()
    assert a.fuzzy_eligible


def test_sql_subquery_reads_inner_table(sql: Any) -> None:
    a = analyze_sql(
        sql, "SELECT name FROM users WHERE id IN (SELECT user_id FROM orders)"
    )
    assert a.reads == {USERS, sql_table("main", "orders")}
    assert a.fuzzy_eligible


def test_sql_volatile_function_is_not_eligible(sql: Any) -> None:
    a = analyze_sql(sql, "SELECT NOW()")
    assert a.reads_unknown
    assert not a.fuzzy_eligible
    assert a.reason is not None


def test_sql_ddl_writes_table(sql: Any) -> None:
    a = analyze_sql(sql, "ALTER TABLE users ADD c INT")
    assert a.canonical is not None
    assert USERS in a.writes
    assert not a.fuzzy_eligible


@pytest.mark.parametrize(
    "query",
    ["INSERT INTO users (id, name) VALUES (9, 'z')", "DELETE FROM users WHERE id = 9"],
)
def test_sql_dml_writes_table(sql: Any, query: str) -> None:
    a = analyze_sql(sql, query)
    assert USERS in a.writes
    assert not a.fuzzy_eligible


@pytest.mark.parametrize("query", ["SELEC name FROM", "", "SELECT (((", "\x00"])
def test_sql_unparseable_is_unknown(sql: Any, query: str) -> None:
    a = analyze_sql(sql, query)
    assert a.canonical is None
    assert a.reads_unknown and a.writes_unknown
    assert a.reason is not None


def test_sql_multiple_statements_are_not_eligible(sql: Any) -> None:
    a = analyze_sql(sql, "SELECT 1; DROP TABLE users")
    assert not a.fuzzy_eligible
    assert a.writes_unknown or USERS in a.writes


def test_sql_unknown_database_is_not_eligible(sql: Any) -> None:
    a = analyze_sql(sql, "SELECT name FROM users", CallContext(cwd="/app"))
    assert not a.fuzzy_eligible


def test_sql_database_is_part_of_state_key(sql: Any) -> None:
    a = analyze_sql(sql, "SELECT name FROM users", CallContext(database="other"))
    assert a.reads == {sql_table("other", "users")}


@pytest.mark.xfail(reason="stretch: safe table-alias normalization", strict=False)
def test_sql_stretch_alias_normalization(sql: Any) -> None:
    base = analyze_sql(sql, "SELECT name FROM users WHERE id = 1")
    aliased = analyze_sql(sql, "SELECT u.name FROM users AS u WHERE u.id = 1")
    assert aliased.canonical == base.canonical


# -------------------------------------------------------------- filesystem


def test_fs_cat_relative_path_pinned(fs: Any) -> None:
    a = analyze_fs(fs, "cat ./data/f.txt")
    assert a.canonical == "cat /app/data/f.txt"
    assert a.reads == {fs_file("/app/data/f.txt")}
    assert a.fuzzy_eligible


@pytest.mark.parametrize(
    "variant", ["cat data/f.txt", "cat /app/data/f.txt", "cat  ./data/f.txt", "cat ./data//f.txt"]
)
def test_fs_cat_path_spellings_are_equivalent(fs: Any, variant: str) -> None:
    assert analyze_fs(fs, variant).canonical == "cat /app/data/f.txt"


def test_fs_cat_same_text_different_cwd_differs(fs: Any) -> None:
    in_app = analyze_fs(fs, "cat f.txt", APP)
    in_data = analyze_fs(fs, "cat f.txt", DATA)
    assert in_app.canonical != in_data.canonical
    assert in_data.reads == {fs_file("/app/data/f.txt")}


def test_fs_cat_multiple_files_in_order(fs: Any) -> None:
    a = analyze_fs(fs, "cat a.txt b.txt")
    assert a.canonical == "cat /app/a.txt /app/b.txt"
    assert a.reads == {fs_file("/app/a.txt"), fs_file("/app/b.txt")}
    swapped = analyze_fs(fs, "cat b.txt a.txt")
    assert swapped.canonical != a.canonical


def test_fs_ls_absolute(fs: Any) -> None:
    a = analyze_fs(fs, "ls /app")
    assert a.canonical == "ls /app"
    assert a.reads == {fs_dir("/app")}
    assert a.fuzzy_eligible


@pytest.mark.parametrize("variant", ["ls", "ls .", "ls ./", "ls /app/"])
def test_fs_ls_spellings_are_equivalent(fs: Any, variant: str) -> None:
    assert analyze_fs(fs, variant).canonical == "ls /app"


def test_fs_ls_unsupported_flag(fs: Any) -> None:
    a = analyze_fs(fs, "ls -l /app")
    assert a.reads_unknown
    assert not a.fuzzy_eligible
    assert a.reason is not None


def test_fs_echo_redirect_writes_file_and_parent(fs: Any) -> None:
    a = analyze_fs(fs, "echo hi > a.txt")
    assert a.canonical == "echo hi > /app/a.txt"
    assert a.writes == {fs_file("/app/a.txt"), fs_dir("/app")}
    assert not a.fuzzy_eligible


def test_fs_cd_writes_cwd(fs: Any) -> None:
    a = analyze_fs(fs, "cd data")
    assert a.canonical == "cd /app/data"
    assert a.writes == {CWD}
    assert not a.fuzzy_eligible


def test_fs_grep_is_read_only_unknown(fs: Any) -> None:
    a = analyze_fs(fs, "grep foo a.txt")
    assert a.canonical is None
    assert a.reads_unknown
    assert not a.writes_unknown
    assert not a.fuzzy_eligible


@pytest.mark.parametrize(
    "command",
    [
        "cat *.txt",
        "python run.py",
        "cat a.txt && rm a.txt",
        "cat a.txt; ls",
        "cat a.txt | wc -l",
        "cat $HOME/a.txt",
        "cat $(echo a.txt)",
        "cat ~/a.txt",
        "",
    ],
)
def test_fs_unsupported_shell_is_unknown(fs: Any, command: str) -> None:
    a = analyze_fs(fs, command)
    assert not a.fuzzy_eligible
    assert a.reason is not None


def test_fs_python_may_write_anything(fs: Any) -> None:
    a = analyze_fs(fs, "python run.py")
    assert a.writes_unknown


@pytest.mark.parametrize("command", ["cat a.txt", "ls", "ls data"])
def test_fs_relative_path_without_cwd_is_unknown(fs: Any, command: str) -> None:
    a = analyze_fs(fs, command, NO_CWD)
    assert a.reads_unknown
    assert not a.fuzzy_eligible


def test_fs_absolute_path_without_cwd_is_fine(fs: Any) -> None:
    a = analyze_fs(fs, "cat /app/a.txt", NO_CWD)
    assert a.reads == {fs_file("/app/a.txt")}
    assert a.fuzzy_eligible


def test_fs_relative_write_without_cwd_is_unknown(fs: Any) -> None:
    a = analyze_fs(fs, "echo hi > a.txt", NO_CWD)
    assert a.writes_unknown


def test_fs_quoted_paths(fs: Any) -> None:
    a = analyze_fs(fs, "cat 'my file.txt'")
    if a.fuzzy_eligible:
        assert a.reads == {fs_file("/app/my file.txt")}
        assert analyze_fs(fs, 'cat "my file.txt"').canonical == a.canonical


# ------------------------------------------------- shared contract rules


BAD_CALLS: list[Any] = [
    {},
    {"tool": "sql"},
    {"tool": "bash"},
    {"tool": "sql", "query": None},
    {"tool": "bash", "command": 42},
    {"unexpected": "shape"},
]


@pytest.mark.parametrize("call", BAD_CALLS)
@pytest.mark.parametrize("which", ["sql", "fs"])
def test_malformed_calls_fail_closed(
    request: pytest.FixtureRequest, which: str, call: Any
) -> None:
    canonicalizer = request.getfixturevalue(which)
    a = canonicalizer.analyze(call, APP)
    assert isinstance(a, CallAnalysis)
    assert not a.fuzzy_eligible


@pytest.mark.parametrize(
    ("which", "call"),
    [
        ("sql", sql_call("SELECT name FROM users WHERE id=1")),
        ("sql", sql_call("UPDATE users SET name='x'")),
        ("fs", bash_call("cat ./a.txt")),
        ("fs", bash_call("echo hi > a.txt")),
    ],
)
def test_analysis_is_deterministic_and_does_not_mutate_input(
    request: pytest.FixtureRequest, which: str, call: dict[str, Any]
) -> None:
    canonicalizer = request.getfixturevalue(which)
    before = dict(call)
    first = canonicalizer.analyze(call, APP)
    second = canonicalizer.analyze(call, APP)
    assert first == second
    assert call == before


@pytest.mark.parametrize(
    "command", ["cat a.txt", "ls data", "echo hi > out.txt", "cd ..", "cat ../x"]
)
def test_fs_state_keys_are_absolute_and_normalized(fs: Any, command: str) -> None:
    a = analyze_fs(fs, command, DATA)
    for key in a.reads | a.writes:
        if key.startswith(("fs:file:", "fs:dir:")):
            path = key.split(":", 2)[2]
            assert path.startswith("/")
            assert "/./" not in path and "//" not in path
            assert not path.endswith("/") or path == "/"
