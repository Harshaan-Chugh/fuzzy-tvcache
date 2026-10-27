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
