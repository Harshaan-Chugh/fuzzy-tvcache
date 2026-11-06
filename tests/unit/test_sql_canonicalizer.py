"""SqlCanonicalizer: worked examples from docs/interfaces.md, equivalences, fail-closed cases."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from fuzzytv.interfaces import CallContext, sql_table
from fuzzytv.sql import SqlCanonicalizer

CTX = CallContext(cwd="/app", database="main")


def T(name: str) -> str:
    return sql_table("main", name)


def analyze(sql: str, ctx: CallContext = CTX):
    return SqlCanonicalizer().analyze({"op": "sql", "sql": sql}, ctx)


def test_select_example():
    a = analyze("SELECT name FROM users WHERE id=1")
    assert a.canonical == "SELECT name FROM users WHERE id = 1"
    assert a.reads == {T("users")} and not a.writes and a.fuzzy_eligible


def test_update_reads_and_writes():
    a = analyze("UPDATE employees SET salary=1 WHERE id=1")
    assert a.reads == a.writes == {T("employees")}
    assert not a.fuzzy_eligible and not a.reads_unknown


def test_join_reads_both_tables():
    a = analyze("SELECT * FROM a JOIN b ON a.id = b.id")
    assert a.reads == {T("a"), T("b")} and a.fuzzy_eligible


def test_alter_writes_table_only():
    a = analyze("ALTER TABLE users ADD c INT")
    assert a.canonical and not a.reads and a.writes == {T("users")}


@pytest.mark.parametrize("sql,reads,writes", [
    ("INSERT INTO logs SELECT * FROM events", {"logs", "events"}, {"logs"}),
    ("DELETE FROM users WHERE id IN (SELECT uid FROM bans)", {"users", "bans"}, {"users"}),
    ("CREATE TABLE t2 AS SELECT * FROM t1", {"t1"}, {"t2"}),
    ("CREATE INDEX i ON users(name)", set(), {"users"}),
    ("DROP TABLE users", set(), {"users"}),
    ("ALTER TABLE users RENAME TO people", set(), {"users", "people"}),
    ("ALTER TABLE users DROP COLUMN age", set(), {"users"}),
    ("WITH c AS (SELECT * FROM users) SELECT * FROM c", {"users"}, set()),
    # Inside a WITH clause a name may be the real table (Postgres), so it counts as a read.
    ("WITH users AS (SELECT * FROM users) SELECT * FROM users", {"users"}, set()),
    ("SELECT * FROM main.Users", {"users"}, set()),
])
def test_dependencies(sql, reads, writes):
    a = analyze(sql)
    assert (a.reads, a.writes) == ({T(n) for n in reads}, {T(n) for n in writes})
    assert not a.reads_unknown and not a.writes_unknown


def test_main_is_the_default_database_under_any_name():
    ctx = CallContext(database="chinook")
    assert analyze("UPDATE main.t SET a = 1", ctx).writes == analyze("SELECT * FROM t", ctx).reads


def test_table_name_case_follows_dialect():
    for a, b in [('SELECT * FROM "Users"', 'select * from "USERS"'),
                 ("SELECT * FROM MAIN.Users", "SELECT * FROM main.users")]:
        assert analyze(a).canonical == analyze(b).canonical
    def pg(sql: str) -> str | None:
        return SqlCanonicalizer("postgres").analyze({"sql": sql}, CTX).canonical

    assert pg('SELECT * FROM "Items"') != pg("SELECT * FROM Items")
    assert pg("SELECT * FROM ITEMS") == pg("SELECT * FROM items")


@pytest.mark.parametrize("sql", [
    "SELECT NOW()", "SELECT RANDOM()", "SELECT datetime('now')", "SELECT CURRENT_TIMESTAMP",
    "SELECT date()", "SELECT julianday()", "SELECT strftime('%Y')", "SELECT hex(randomblob(2))",
])
def test_volatile_reads_unknown(sql):
    a = analyze(sql)
    assert a.reads_unknown and a.reason == "volatile" and not a.fuzzy_eligible


@pytest.mark.parametrize("sql,reason", [
    ("SELEC name FRM users", "sql parse failed"),
    ("SELECT 1; SELECT 2", "expected exactly one sql statement"),
    ("PRAGMA table_info(users)", "unsupported statement"),
    ("BEGIN", "unsupported statement"),
    ("CREATE VIEW v AS SELECT * FROM users", "unsupported ddl"),
    ("CREATE TEMP TABLE x(a INT)", "temporary"),
    ("CREATE VIRTUAL TABLE f USING fts5(x)", "virtual"),
    ("INSERT INTO t SELECT * FROM temp.x", "qualified table"),
    ("INSERT INTO t SELECT * FROM other.x", "qualified table"),
    ("SELECT * INTO t2 FROM users", "SELECT INTO"),
    ("SELECT nextval('s')", "sequence function"),
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "nested write"),
])
def test_fail_closed(sql, reason):
    a = analyze(sql)
    assert a.canonical is None and a.writes_unknown and reason in a.reason


def test_catalog_or_unknown_db_read_is_read_only_unknown():
    for a in (analyze("SELECT * FROM sqlite_master"),
              analyze("SELECT * FROM users", CallContext())):
        assert a.reads_unknown and not a.writes_unknown and not a.fuzzy_eligible


def test_missing_sql_text():
    assert SqlCanonicalizer().analyze({"op": "read"}, CTX).writes_unknown


# Equivalent pairs: same canonical string AND same SQLite output.
EQUIVALENT = [
    ("select name\nfrom users where id=1", "SELECT name FROM users WHERE id = 1"),
    ("SELECT name FROM users -- comment\nWHERE id=1", "SELECT  name FROM USERS WHERE id=1"),
    ("SELECT u.name FROM users u WHERE u.id <> 2", "SELECT x.name FROM users AS x WHERE x.id != 2"),
    ("SELECT u.*, o.amt FROM users u JOIN orders o ON o.uid = u.id",
     "SELECT a.*, b.amt FROM users AS a JOIN orders AS b ON b.uid = a.id"),
]
DIFFERENT = [
    ("SELECT name FROM users WHERE id = 1", "SELECT name FROM users WHERE id = 2"),
    ("SELECT name FROM users", "SELECT name FROM users ORDER BY name"),
    ("SELECT name FROM users WHERE name = 'a'", "SELECT name FROM users WHERE name = 'A'"),
    ("SELECT u.name FROM users u", "SELECT name AS u FROM users"),
]


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE users(id INT, name TEXT);"
        "INSERT INTO users VALUES (1,'b'),(2,'a'),(3,'A');"
        "CREATE TABLE orders(uid INT, amt INT);"
        "INSERT INTO orders VALUES (1,10),(2,20),(1,5);"
    )
    return lambda sql: json.dumps(conn.execute(sql).fetchall())


@pytest.mark.parametrize("left,right", EQUIVALENT)
def test_equivalent_calls_share_canonical_and_output(db, left, right):
    assert analyze(left).canonical == analyze(right).canonical
    assert db(left) == db(right)


@pytest.mark.parametrize("left,right", DIFFERENT)
def test_different_calls_keep_distinct_canonicals(left, right):
    assert analyze(left).canonical != analyze(right).canonical


@pytest.mark.parametrize("sql", [
    "SELECT u.id FROM users u WHERE EXISTS (SELECT 1 FROM orders u WHERE u.uid = 1)",
    "SELECT u.id FROM users u JOIN (SELECT 1 AS id) AS u ON u.id = 1",
    "SELECT _t0 FROM users u",
    "WITH u AS (SELECT 1 AS x) SELECT * FROM users u",
])
def test_ambiguous_aliases_are_left_alone(sql):
    assert "users AS u" in analyze(sql).canonical


async def test_executor_reuses_select_across_unrelated_update(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    def sql(op: str, text: str):
        return workload.call(op, sql=text)

    async def replay(trace: list) -> tuple[str, str]:
        async with workload.make_executor("task", canonicalizer=SqlCanonicalizer()) as executor:
            for i in range(len(trace)):
                result = await executor.lookup_or_execute(trace[: i + 1])
            return executor.last_decision.source, result

    hello = sql("sql_write", "UPDATE items SET value='hello'")
    await replay([hello, sql("sql", "SELECT value FROM items")])
    unrelated = sql("sql_write", "update notes set value='x'")
    relevant = sql("sql_write", "UPDATE items SET value='world'")
    assert await replay([hello, unrelated, sql("sql", "select value\n  from ITEMS")]) == (
        "fuzzy", json.dumps([["hello"]]))
    assert await replay([hello, relevant, sql("sql", "select value from items")]) == (
        "execute", json.dumps([["world"]]))


@pytest.mark.parametrize("sql", [
    "SELECT a+1 /*x*/ , a+2  /* y */FROM t",
    "SELECT a+1 -- z\n FROM t",
    "SELECT DISTINCT sum( a ) FROM t",
    "SELECT (a+1), count(*) FROM t UNION SELECT 1, 2",
    "SELECT 1 + 1",
    "WITH c AS (SELECT a FROM t) SELECT COUNT(*) FROM c",
])
def test_label_suffix_matches_sqlite_column_names(sql):
    from fuzzytv.sql import _select_lists

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(a)")
    expected = [d[0] for d in conn.execute(sql).description]
    assert expected in _select_lists(sql, "sqlite")


def test_cte_labels_reaching_output_keep_statement_verbatim():
    canon, ctx = SqlCanonicalizer(), CallContext(database="main")
    a = canon.analyze({"sql": "WITH c AS (SELECT Count(*) FROM t) SELECT * FROM c"}, ctx)
    b = canon.analyze({"sql": "WITH c AS (SELECT count(*) FROM t) SELECT * FROM c"}, ctx)
    assert a.canonical != b.canonical
    assert a.reads == b.reads == {"sql:table:main.t"}
