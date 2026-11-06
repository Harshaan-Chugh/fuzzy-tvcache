# Tool-call taxonomy (initial)

| Category | Examples | Fuzzy-eligible? | Notes |
|---|---|---|---|
| SQL read | `SELECT` on known tables | Yes, if parse succeeds | Depends on the referenced tables and views (and their underlying tables), plus the schema. |
| SQL write | `INSERT/UPDATE/DELETE` on known tables | No (execute) | Advances state of the written tables. Reads in `WHERE`/subqueries count too. |
| SQL DDL | `CREATE/ALTER/DROP` | No | Schema change. Treat as a write to the schema, possibly global. |
| SQL txn/session | `BEGIN`, `PRAGMA`, `ATTACH`, `SET` | No | Unknown. Forces fallback. |
| FS read | `cat <files>`, `ls [flags] <dirs>` | Yes, for supported flags | Depends on cwd-resolved paths. `ls` depends on the directory entry set, and `-l` adds metadata. |
| FS write | `echo > f`, `rm`, `mv`, `touch`, `mkdir` | No (execute) | Known writes. `mv` writes both paths and both parent dirs. |
| Arbitrary shell | pipes, scripts, `python x.py`, globs | No | Unknown reads and writes. Poisons state. |
| Volatile | `date`, `$RANDOM`, network | No | Not deterministic even with identical state. |
| Terminal test | `TestToolCall` | Exact only | Depends on the full state. |

Initial supported set: SQL `SELECT` (single statement, no functions with side effects), and
`cat`/`ls` with a small flag allowlist. Everything else is exact-only.

## SQL canonicalizer (`fuzzytv.sql.SqlCanonicalizer`)

- Canonical form is the SQLGlot AST re-rendered in the workload dialect (default `sqlite`).
  This removes whitespace, keyword-case, comment and operator-spelling (`<>`/`!=`) differences.
  Table names follow the dialect's case rules, and table aliases are renamed positionally
  (`_t0`, `_t1`, …) only when no alias is shadowed or reused.
- SQLGlot *rewrites* some constructs instead of reformatting them. Examples: `CAST(x AS BOOLEAN)`
  becomes `INTEGER`, `CAST(x AS DATE)` becomes `DATE(x)`, `INT` becomes `INTEGER PRIMARY KEY`,
  and `GREATEST` becomes `MAX`. Statements containing types, `GREATEST`/`LEAST`, or
  `CREATE TABLE … AS` (whose column names come from expression text) keep their original text.
- Not normalized, since these can change output or row order: column names and aliases,
  literals, predicate order, `ORDER BY`, and `main.` qualifiers (`CREATE INDEX … ON main.t`
  is an error in SQLite).
- Supported: one `SELECT`/set-op/`WITH` query, `INSERT/UPDATE/DELETE`, and `CREATE TABLE/INDEX`,
  `DROP TABLE`, `ALTER TABLE`. Anything else, including views, triggers, temp or virtual tables,
  `SELECT INTO`, sequences, transactions, `PRAGMA`, nested writes and multi-statement input, is
  `unknown`. Tables qualified with any database other than the default (`main`) are unknown too.
- `CREATE INDEX` writes its table because an index can change unordered row order. Inside a
  `WITH` clause, names count as real tables, because Postgres resolves a CTE's own name there.
- Volatile calls (`NOW()`, `RANDOM()`, `CURRENT_*`, `'now'`, zero-argument `date()` etc.) and
  reads of system catalogs or an unknown database set `reads_unknown`.
- Assumptions: the initial database has no views, triggers or cascading foreign keys. The tool
  output doesn't echo query text: unaliased expression headers and names in error messages are
  both exceptions. Stored values passed to date functions are never `'now'`.
- Tests: `tests/unit/test_sql_canonicalizer.py` covers worked examples and edge cases.
  `tests/unit/test_sql_properties.py` runs randomized differential checks against SQLite, plus a
  117-statement surface corpus. Raise `FUZZYTV_SQL_FUZZ` for longer runs.
