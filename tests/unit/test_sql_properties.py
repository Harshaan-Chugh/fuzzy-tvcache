"""Randomized differential tests of SqlCanonicalizer against a real SQLite database.

Scale up with FUZZYTV_SQL_FUZZ=<n> (default 300 queries / rollouts per test).
"""

import json
import os
import random
import sqlite3
from collections import defaultdict
from pathlib import Path

import pytest

from fuzzytv.interfaces import CallAnalysis, CallContext
from fuzzytv.sql import SqlCanonicalizer
from fuzzytv.state.tracker import StateTracker

N = int(os.environ.get("FUZZYTV_SQL_FUZZ", "300"))
CTX = CallContext(database="main")
CANON = SqlCanonicalizer()
SCHEMA = {
    "users": ["id", "name", "age", "dept"],
    "orders": ["id", "uid", "amt", "note"],
    "depts": ["id", "name"],
}
JOINS = {("users", "depts"): ("dept", "id"), ("orders", "users"): ("uid", "id")}
WORDS = ["a", "B", "ab", "Ab", "x%", "", None]


def make_db(seed: int) -> sqlite3.Connection:
    rng = random.Random(seed)
    conn = sqlite3.connect(":memory:", isolation_level=None)
    for table, cols in SCHEMA.items():
        conn.execute(f"CREATE TABLE {table} ({', '.join(cols)})")
        for i in range(rng.randint(0, 8)):
            row = [i] + [rng.choice([rng.randint(0, 5), rng.choice(WORDS)]) for _ in cols[1:]]
            conn.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})", row)
    return conn


def clone(conn: sqlite3.Connection) -> sqlite3.Connection:
    copy = sqlite3.connect(":memory:", isolation_level=None)
    conn.backup(copy)
    return copy


def run(conn: sqlite3.Connection, sql: str) -> str:
    try:
        return json.dumps(conn.execute(sql).fetchall(), default=repr)  # repr: BLOBs
    except sqlite3.Error as error:
        return f"ERR {type(error).__name__}"


def labels(conn: sqlite3.Connection, sql: str) -> tuple[str, ...] | None:
    """Result column names, which are part of the tool output."""
    try:
        return tuple(d[0] for d in conn.execute(sql).description or ())
    except sqlite3.Error:
        return None


def snapshot(conn: sqlite3.Connection) -> dict[str, str]:
    """Per table: columns, indexes and rows (schema compared structurally, not as text)."""
    def rows(sql: str) -> list:
        return conn.execute(sql).fetchall()

    state = {}
    for (table,) in rows("SELECT name FROM sqlite_master WHERE type = 'table'"):
        indexes = [i[1] for i in rows(f'PRAGMA index_list("{table}")')]
        state[table.lower()] = repr([
            rows(f'PRAGMA table_xinfo("{table}")'),
            sorted((i, rows(f'PRAGMA index_xinfo("{i}")')) for i in indexes),
            sorted(map(repr, rows(f'SELECT * FROM "{table}"'))),
        ])
    return state


class Style:
    """Random surface spelling: case, whitespace, comments, aliases, operator spelling."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng

    def kw(self, word: str) -> str:
        return self.rng.choice([word.upper(), word.lower(), word.capitalize()])

    def table(self, name: str) -> str:
        return self.rng.choice([name, name.upper(), f'"{name}"', f'"{name.upper()}"'])

    def sp(self) -> str:
        return self.rng.choice([" ", "  ", "\n", " /* c */ ", "\t"])

    def alias(self, used: set[str]) -> str:
        while (name := self.rng.choice("pqrstuvwxyz") + self.rng.choice(["", "1", "_a"])) in used:
            pass
        used.add(name)
        return name

    def op(self, op: str) -> str:
        return self.rng.choice({"<>": ["<>", "!="], "=": ["=", "=="]}.get(op, [op]))


def gen_pred(rng: random.Random, col: str, st: Style) -> str:
    kind = rng.randrange(5)
    if kind == 0:
        return f"{col} {st.kw('IS')} {st.kw('NULL')}"
    if kind == 1:
        return f"{col} {st.kw('LIKE')} '{rng.choice(['a%', 'A%', '%b'])}'"
    if kind == 2:
        return f"{col} {st.kw('IN')} ({st.kw('SELECT')} id {st.kw('FROM')} {st.table('depts')})"
    op = rng.choice(["=", "<>", "<", ">="])
    value = rng.choice([rng.randint(0, 5), repr(rng.choice(WORDS[:-1]))])
    return f"{col}{st.sp()}{st.op(op)}{st.sp()}{value}"


def gen_select(rng: random.Random, st: Style) -> str:
    base = rng.choice(list(SCHEMA))
    sources = [base]
    join = next((pair for pair in JOINS if pair[0] == base), None)
    if join and rng.random() < 0.5:
        sources.append(join[1])
    used: set[str] = set()
    names = [st.alias(used) if rng.random() < 0.6 else src for src in sources]
    cols = [f"{names[i]}.{c}" for i, s in enumerate(sources) for c in SCHEMA[s] if rng.random() < 0.4]
    if not cols or rng.random() < 0.15:
        cols = ["*"]
    agg = rng.random() < 0.2 and cols != ["*"]
    if agg:
        cols = [cols[0], f"{st.kw('COUNT')}(*)"]
    parts = [f"{st.kw('SELECT')}{st.sp()}{', '.join(cols)}{st.sp()}{st.kw('FROM')} "]
    if len(sources) > 1:
        parts.append(_join(sources, names, st, rng))
    else:
        parts.append(_src(sources[0], names[0], st, rng))
    preds = [
        gen_pred(rng, f"{names[0]}.{rng.choice(SCHEMA[base])}", st) for _ in range(rng.randint(0, 2))
    ]
    if preds:
        parts.append(f"{st.sp()}{st.kw('WHERE')} " + f" {st.kw('AND')} ".join(preds))
    if agg:
        parts.append(f" {st.kw('GROUP BY')} {cols[0]}")
    if rng.random() < 0.4:
        parts.append(f" {st.kw('ORDER BY')} 1 {rng.choice(['', 'DESC', 'ASC'])}")
    if rng.random() < 0.3:
        parts.append(f" {st.kw('LIMIT')} {rng.randint(0, 3)}")
    sql = "".join(parts)
    if rng.random() < 0.15:
        sql = f"{st.kw('WITH')} c {st.kw('AS')} ({sql}) {st.kw('SELECT')} * {st.kw('FROM')} c"
    return sql


def _src(table: str, name: str, st: Style, rng: random.Random) -> str:
    if name == table:
        return table  # unaliased sources are qualified by bare name, so spell it plainly
    return f"{st.table(table)} {st.kw('AS') + ' ' if rng.random() < 0.5 else ''}{name}"


def _join(sources: list[str], names: list[str], st: Style, rng: random.Random) -> str:
    left, right = JOINS[(sources[0], sources[1])]
    kind = st.kw(rng.choice(["JOIN", "LEFT JOIN"]))
    return (f"{_src(sources[0], names[0], st, rng)} {kind} {_src(sources[1], names[1], st, rng)}"
            f" {st.kw('ON')} {names[0]}.{left} = {names[1]}.{right}")


def gen_write(rng: random.Random, st: Style) -> str:
    table, kw = rng.choice(list(SCHEMA)), st.kw
    col = rng.choice(SCHEMA[table][1:])
    val = rng.choice([rng.randint(0, 5), "'a'", "NULL"])
    return rng.choice([  # every option is rendered, then one is picked
        f"{st.kw('UPDATE')} {st.table(table)} {st.kw('SET')} {col}{st.sp()}={st.sp()}{val}"
        f" {st.kw('WHERE')} {gen_pred(rng, col, st)}",
        f"{st.kw('DELETE FROM')} {st.table(table)} {st.kw('WHERE')} {gen_pred(rng, col, st)}",
        f"{st.kw('INSERT INTO')} {st.table(table)} ({', '.join(SCHEMA[table])}) "
        f"{st.kw('VALUES')} ({', '.join(str(rng.randint(0, 9)) for _ in SCHEMA[table])})",
        f"{st.kw('INSERT INTO')} {st.table('depts')} {st.kw('SELECT')} id + 10, name"
        f" {st.kw('FROM')} {st.table('users')} {st.kw('WHERE')} {gen_pred(rng, 'age', st)}",
        f"{kw('CREATE INDEX')} {kw('IF NOT EXISTS')} i_{table}_{col}"
        f" {kw('ON')} {st.table(table)} ({col} DESC)",
        f"{kw('ALTER TABLE')} {st.table(table)} {kw('ADD COLUMN')} z{rng.randint(0, 3)}"
        f" {kw('DEFAULT')} {val}",
        f"{kw('CREATE TABLE')} {kw('IF NOT EXISTS')} copy_{table} {kw('AS')} {gen_select(rng, st)}",
        f"{kw('DROP TABLE')} {kw('IF EXISTS')} copy_{table}",
    ])


def corpus(seed: int, n: int) -> list[str]:
    rng = random.Random(seed)
    gens = [gen_select, gen_select, gen_write]
    return [rng.choice(gens)(rng, Style(rng)) for _ in range(n)]


def test_generated_sql_parses_and_runs():
    conn = make_db(0)
    queries = corpus(1, N)
    errors = [q for q in queries if run(clone(conn), q).startswith("ERR")]
    assert len(errors) < 0.1 * len(queries), errors[:5]  # generator sanity
    assert all(CANON.analyze({"sql": q}, CTX).canonical for q in queries)


def test_canonical_text_is_equivalent_to_original():
    """Canonical SQL executes to the same rows and the same database state."""
    for seed, sql in enumerate(corpus(2, N)):
        conn = make_db(seed)
        a, b = clone(conn), clone(conn)
        canonical = CANON.analyze({"sql": sql}, CTX).canonical
        assert run(a, sql) == run(b, canonical), (sql, canonical)
        assert snapshot(a) == snapshot(b), (sql, canonical)


def test_equal_canonicals_never_disagree():
    """The false-hit property: same canonical => same output on every database."""
    groups = defaultdict(set)
    for sql in corpus(3, N) + corpus(4, N):
        groups[CANON.analyze({"sql": sql}, CTX).canonical].add(sql)
    merged = [g for g in groups.values() if len(g) > 1]
    assert merged, "no surface variants collided; generator too sparse"
    for seed in range(5):
        conn = make_db(seed)
        for group in merged:
            outputs = {run(clone(conn), sql) for sql in group}
            assert len(outputs) == 1, group
            assert len({labels(clone(conn), sql) for sql in group}) == 1, group


def test_surface_variants_collapse():
    """Same query, different spelling: canonicals must match (hit-rate, not safety).

    Spellings that SQLite labels differently (`COUNT(*)` vs `count(*)`) return
    different column headers, so they must stay apart and are not counted.
    """
    misses = compared = 0
    conn = make_db(0)
    for seed in range(N):
        a, b = (gen_select(random.Random(seed), Style(random.Random(seed * 2 + k))) for k in (0, 1))
        if labels(clone(conn), a) != labels(clone(conn), b):
            continue
        compared += 1
        canonicals = {CANON.analyze({"sql": q}, CTX).canonical for q in (a, b)}
        misses += len(canonicals) != 1
    assert compared > N // 2
    assert misses == 0


def test_declared_dependencies_cover_real_ones():
    """Reads: dropping any undeclared table leaves output unchanged.
    Writes: every table whose rows, schema or indexes changed is declared."""
    for seed, sql in enumerate(corpus(5, N)):
        conn = make_db(seed)
        a = CANON.analyze({"sql": sql}, CTX)
        reads = {k.rsplit(".", 1)[1] for k in a.reads}
        writes = {k.rsplit(".", 1)[1] for k in a.writes}
        after = clone(conn)
        expected = run(after, sql)
        before_state, after_state = snapshot(conn), snapshot(after)
        changed = {t for t in before_state | after_state
                   if before_state.get(t) != after_state.get(t)}
        assert changed <= writes, (sql, changed, writes)
        for table in set(before_state) - reads - writes:
            perturbed = clone(conn)
            perturbed.execute(f'DROP TABLE "{table}"')
            assert run(perturbed, sql) == expected, (sql, table)


def test_sqlite_authorizer_agrees():
    """Every table SQLite itself touches is declared as read or written."""
    tables = set(SCHEMA) | {f"copy_{t}" for t in SCHEMA}
    touched: set[str] = set()

    def authorize(action, arg1, arg2, db, source):
        touched.update(n.lower() for n in (arg1, arg2) if n and n.lower() in tables)
        return sqlite3.SQLITE_OK

    for seed, sql in enumerate(corpus(6, N)):
        conn = make_db(seed)
        conn.set_authorizer(authorize)
        touched.clear()
        run(conn, sql)
        a = CANON.analyze({"sql": sql}, CTX)
        declared = {k.rsplit(".", 1)[1] for k in a.reads | a.writes}
        assert touched <= declared, (sql, touched, declared)


def test_rollouts_have_no_false_fuzzy_hits():
    """End to end with StateTracker: shared cache across rollouts, every hit verified."""
    rng = random.Random(7)
    reads, writes = range(12), range(100, 106)  # fixed shapes (seeds), random spelling
    initial = make_db(8)
    cache: dict[str, str] = {}
    exact: set[tuple[str, ...]] = set()
    hits = exact_hits = false_hits = lookups = 0
    for _ in range(N):
        conn, tracker, history = clone(initial), StateTracker("task"), []
        for _ in range(rng.randint(2, 8)):
            pool, gen = (writes, gen_write) if rng.random() < 0.3 else (reads, gen_select)
            sql = gen(random.Random(rng.choice(pool)), Style(rng))
            analysis = CANON.analyze({"sql": sql}, CTX)
            key = tracker.fuzzy_key(analysis)
            output = run(conn, sql)
            history.append(sql)
            exact_hits += tuple(history) in exact
            exact.add(tuple(history))
            if key is not None:
                lookups += 1
                if key in cache:
                    hits += 1
                    false_hits += cache[key] != output
                cache.setdefault(key, output)
            tracker.observe(analysis)
    print(f"\nrollouts={N} fuzzy lookups={lookups} hits={hits} exact-prefix hits={exact_hits}")
    assert false_hits == 0
    assert hits > exact_hits


@pytest.mark.parametrize("seed", range(5))
def test_garbage_never_raises(seed):
    rng = random.Random(seed)
    tokens = ("SELECT FROM WHERE ( ) , ; ' \" ` [ ] -- /* */ JOIN WITH AS users 1 x . * = "
              "INSERT VALUES").split()
    base = corpus(seed, 50)
    for i in range(N):
        sql = " ".join(rng.choice(tokens) for _ in range(rng.randint(0, 12)))
        if i % 2:
            s = base[i % 50]
            cut = rng.randrange(len(s) + 1)
            sql = s[:cut] + rng.choice(tokens) + s[cut + rng.randint(0, 5):]
        assert isinstance(CANON.analyze({"sql": sql}, CTX), CallAnalysis)


SURFACE = Path(__file__).with_name("sqlite_surface.sql").read_text().split("-- statements\n")


@pytest.mark.parametrize("sql", SURFACE[1].splitlines())
def test_sqlite_surface_canonical_is_equivalent(sql):
    """Casts, JSON, dates, windows, joins, upserts, DDL: the canonical runs identically."""
    canonical = CANON.analyze({"sql": sql}, CTX).canonical
    if canonical is None:
        pytest.skip("rejected as unknown")
    a, b = (sqlite3.connect(":memory:", isolation_level=None) for _ in range(2))
    for conn in (a, b):
        conn.executescript(SURFACE[0])
    assert run(a, sql) == run(b, canonical), canonical
    assert snapshot(a) == snapshot(b), canonical
