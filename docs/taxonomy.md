# Tool-call taxonomy (initial)

| Category | Examples | Fuzzy-eligible? | Notes |
|---|---|---|---|
| SQL read | `SELECT` on known tables | Yes, if parse succeeds | Depends on the referenced tables and views (and their underlying tables), plus the schema. |
| SQL write | `INSERT/UPDATE/DELETE` on known tables | No (execute) | Advances state of the written tables. Reads in `WHERE`/subqueries count too. |
| SQL DDL | `CREATE/ALTER/DROP` | No | Schema change. Treat as a write to the schema, possibly global. |
| SQL schema read | `PRAGMA table_info(t)` | Yes | Reads `t` (its schema changes only through DDL on `t`). |
| SQL txn/session | `BEGIN`, other `PRAGMA`s, `ATTACH`, `SET` | No | Unknown. Forces fallback. |
| FS read | `cat <files>`, `ls [path]` | Yes, without flags | Depends on cwd-resolved paths. `ls` depends on the directory entry set, and `-l` adds metadata. |
| FS write | `echo > f`, `echo >> f` | No (execute) | Known writes. `rm`, `mv`, `touch`, `mkdir` are currently unknown (see below). |
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
- Result column labels: SQLite names an unaliased result expression after its text as typed,
  from its first token up to the next comma or clause keyword (`sum( a )`, `COUNT(*) /* c */`).
  The canonical form appends that text (`… /* labels: COUNT(*) */`) for every unaliased
  non-column output, including CTE and `FROM` subquery outputs that reach the result through
  `*`. If the text cannot be matched to the AST, the statement keeps its original text.
- Supported: one `SELECT`/set-op/`WITH` query, `INSERT/UPDATE/DELETE`, and `CREATE TABLE/INDEX`,
  `DROP TABLE`, `ALTER TABLE`. Anything else, including views, triggers, temp or virtual tables,
  `SELECT INTO`, sequences, transactions, nested writes and multi-statement input, is
  `unknown`. `PRAGMA table_info`, `table_xinfo`, `index_list` and `foreign_key_list` on a
  default-database table are reads of that table, canonicalized as `PRAGMA table_info(t)`;
  every other `PRAGMA` is unknown. Tables qualified with any database other than the default (`main`) are unknown too.
- `CREATE INDEX` writes its table because an index can change unordered row order. Inside a
  `WITH` clause, names count as real tables, because Postgres resolves a CTE's own name there.
- Volatile calls (`NOW()`, `RANDOM()`, `CURRENT_*`, `'now'`, zero-argument `date()` etc.) and
  reads of system catalogs or an unknown database set `reads_unknown`.
- Assumptions: the initial database has no views, triggers or cascading foreign keys. The tool
  output doesn't echo query text in error messages (headers are handled above). Stored values
  passed to date functions are never `'now'`.
- Tests: `tests/unit/test_sql_canonicalizer.py` covers worked examples and edge cases.
  `tests/unit/test_sql_properties.py` runs randomized differential checks against SQLite, plus a
  117-statement surface corpus. Raise `FUZZYTV_SQL_FUZZ` for longer runs.

## Filesystem canonicalizer (`fuzzytv.fs.FsCanonicalizer`)

- A command is analyzed only if bash would read it as one simple command of literal words. Unquoted
  control operators, expansions, globs, `~`, `#`, `!`, backslashes, brace expansion, control
  characters and any redirect other than a trailing `echo … > target` or `>> target` are `unknown`.
  This check runs before the command name is looked at, so `ls>out` can't pass as a read.
- Supported: `cat <files>`, `ls [path]`, `echo [words]`, `echo [words] > file`, `echo [words] >> file`,
  `cd <path>`. There is no flag allowlist yet: `cat`/`ls` flags are `reads_unknown`.
- `grep`, `head`, `tail` and `wc` are `reads_unknown` only, so they don't disable later calls.
  Everything else is `unknown`, including `rm`, `mv`, `touch` and `mkdir`: `mv a b` writes `b/a` if
  `b` is a directory, which the command text can't show.
- Canonical form: resolved absolute paths, each word `shlex`-quoted, so `cat 'a /app/b'` and
  `cat a /app/b` stay distinct. It isn't guaranteed to be runnable.
- Only leading `..` is collapsed, against the cwd. `missing/../f` fails and `link/../f` can be another
  file, so `..` inside a path isn't resolved. A file operand ending in `/` or `.` is rejected, since
  `cat f/` fails where `cat f` doesn't. `/proc`, `/dev` and `/sys` are never eligible.
- `ls` with one operand: if the operand is the cwd or an ancestor of it, it must be a directory, so
  the call reads `fs:dir` and all spellings merge. Otherwise it also reads `fs:file` (`echo > x`
  must invalidate `ls x`) and keeps the operand as typed (`ls ./x` prints `./x` for a file), giving
  canonical forms like `ls /app/data # as ./data`. Several operands are `reads_unknown`, because
  `ls` prints a header spelled as typed.
- Assumptions and the known `cat` limitation are in `docs/state-tracking.md`.
- Tests: `tests/unit/test_fs_canonicalizer.py` covers edge cases. `tests/unit/test_fs_properties.py`
  runs randomized differential checks against real bash in a temp tree: equal fuzzy keys give equal
  output, and reported writes cover every change on disk. Raise `FUZZYTV_FS_FUZZ` for longer runs.
