# Shared interfaces (v0)

Code: `src/fuzzytv/interfaces.py`. Tests: `tests/unit/test_interfaces.py`.

The split of responsibility:
**Kevin (`sql/`) and Etienne (`fs/`) determine what a call means and which state it reads and
writes. Harshaan (`state/`) decides whether a cached result is reusable.** A canonicalizer sees one
call and no history; the state tracker sees the history and builds cache keys.

## The contract

```python
class Canonicalizer(Protocol):
    def analyze(self, call: dict[str, Any], ctx: CallContext) -> CallAnalysis: ...
```

| Field of `CallAnalysis` | Meaning |
|---|---|
| `canonical` | Normalized call string. Equivalent calls give identical strings. `None` if it can't be canonicalized. Used only for cache keys, never executed. |
| `reads` | State-key names the output depends on. |
| `writes` | State-key names the call may change. |
| `reads_unknown` | The call may read things not listed (unsupported command, time, network). |
| `writes_unknown` | The call may write things not listed. The state tracker treats all state as unreliable afterwards. |
| `reason` | Why the call is not fuzzy-eligible, e.g. `"unsupported command: python"`. For logs and evaluation. |
| `fuzzy_eligible` (property) | Canonical, no unknowns, no writes. Only these calls may be served by a fuzzy hit. |

`unknown(reason)` is shorthand for "may read and write anything". It's the safe default.

`CallContext` carries `cwd` and `database`, either of which may be `None` (unknown).

## State-key names

Build these with the helpers `sql_table`, `fs_file`, `fs_dir` and `CWD`, never by hand.

| Name | Covers |
|---|---|
| `sql:table:<db>.<table>` | Rows **and** schema. DDL on a table is a write to this key. |
| `fs:file:<abs path>` | Contents and existence of a file. |
| `fs:dir:<abs path>` | Set of entry names in a directory. Creating or deleting a file writes its parent directory. |
| `fs:cwd` | The shell's current directory. `cd` writes it. |

## Decisions (2026-10-31)

- **A.** A schema change is a write to the same `sql:table` key as a data change.
- **B.** There are two unknown flags. A read-only but unsupported call (e.g. `grep`) sets only
  `reads_unknown`. It isn't eligible, but it doesn't make later calls in the rollout ineligible.
- **C.** With `cwd=None`, a relative path is unanalyzable, so set `reads_unknown` (and
  `writes_unknown` for writes).
- A call may both read and write (e.g. `UPDATE ... WHERE`). List both, honestly.
- State keys are strings.

## Worked examples (cwd = `/app`, database = `main`)

These are the target outputs for the canonicalizers. They are encoded, with more equivalent and
non-equivalent cases, in `tests/unit/test_canonicalizer_contract.py`. Those tests expect
`fuzzytv.sql.SqlCanonicalizer` and `fuzzytv.fs.FsCanonicalizer` (no-argument constructors) and
skip until they exist. One addition came from the evaluation harness: SQLite labels unaliased
result columns with the select-list text as typed, so `SUM(x)` and `sum(x)` are **not**
equivalent when output includes headers (see `docs/evaluation.md`).

| Call | canonical | reads | writes | flags / reason |
|---|---|---|---|---|
| `SELECT name FROM users WHERE id=1` | `SELECT name FROM users WHERE id = 1` | `sql:table:main.users` | — | eligible |
| `select name\nfrom users where id=1` | same as above | same | — | eligible |
| `UPDATE employees SET salary=1 WHERE id=1` | normalized | `sql:table:main.employees` | `sql:table:main.employees` | — |
| `SELECT * FROM a JOIN b ON …` | normalized | `…main.a`, `…main.b` | — | eligible |
| `SELECT NOW()` | — | — | — | `reads_unknown`, `"volatile"` |
| `ALTER TABLE users ADD c INT` | normalized | — | `sql:table:main.users` | — |
| unparseable SQL | `None` | — | — | `unknown("sql parse failed")` |
| `cat ./data/f.txt` | `cat /app/data/f.txt` | `fs:file:/app/data/f.txt` | — | eligible |
| `cat a.txt b.txt` | `cat /app/a.txt /app/b.txt` | both files | — | eligible |
| `ls /app` | `ls /app` | `fs:dir:/app` | — | eligible |
| `ls -l /app` | — | — | — | `reads_unknown`, `"unsupported flag -l"` |
| `echo hi > a.txt` | `echo hi > /app/a.txt` | — | `fs:file:/app/a.txt`, `fs:dir:/app` | — |
| `cd data` | `cd /app/data` | — | `fs:cwd` | — |
| `grep foo a.txt` | `None` | — | — | `reads_unknown`, `"unsupported command: grep"` |
| `cat *.txt` | — | — | — | `unknown("glob")` |
| `python run.py` | — | — | — | `unknown("unsupported command: python")` |

## Requirements from upstream (see `architecture.md`)

- The workload's `to_dict()` schema is unconfirmed (ask Bhaskar), so `call` is a plain dict for now.
- In the terminal workload the shell session persists, so `cd` mutates cwd.
- Terminal calls are likely raw bash strings, possibly multi-line scripts joined with `&&`.
  Anything other than a simple supported command must analyze to "unknown".
- Precondition for any fuzzy hit: the cached value must be the call's own output, not
  accumulated terminal/pane history. This is unverified for the terminal workload.

## Open questions

- Who dispatches a call to the SQL or fs canonicalizer? This depends on the `to_dict()` schema.
  Proposal used by the eval harness and acceptance tests (`fuzzytv/eval/calls.py`):
  `{"tool": "sql", "query": ...}` and `{"tool": "bash", "command": ...}`, dispatched on `tool`
  (`fuzzytv.eval.canonicalizers.RealCanonicalizer`).
- How does the state tracker learn the new cwd after `cd`? Probably from `canonical`, or we add a
  field later.
- Does Rohan's harness consume the same analysis objects to explain false hits? Yes: per-call
  records carry the executor's decision reason, and each trace stores the oracle analysis.
