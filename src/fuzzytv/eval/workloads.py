"""Reproducible workload traces with oracle analyses.

A trace is a list of tasks. Each task has an initial state and a group of
rollouts (as in GRPO-style RL, where one prompt is sampled several times).
Rollouts follow a per-task plan with seeded perturbations that create the
cases fuzzy matching is about:

- identical prefixes (exact hits);
- spelling variants: SQL whitespace/case/alias, path spellings (canonical hits);
- inserted writes that are irrelevant to later reads (relevant-state hits);
- inserted writes that *are* relevant, and unknown commands (must not hit).

Every step stores an **oracle** analysis: what a canonicalizer following
docs/interfaces.md exactly would return. The generator constructs calls from
structured intents, so the oracle is known by construction, not by parsing.
It is an upper bound for the real canonicalizers in `fuzzytv.sql`/`fuzzytv.fs`.
"""

import json
import posixpath
import random
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fuzzytv.eval.calls import bash_call, sql_call
from fuzzytv.interfaces import (
    CWD,
    CallAnalysis,
    CallContext,
    fs_dir,
    fs_file,
    sql_table,
    unknown,
)

GENERATOR_VERSION = 1


# ------------------------------------------------------------- trace types


def analysis_to_json(analysis: CallAnalysis) -> dict[str, Any]:
    return {
        "canonical": analysis.canonical,
        "reads": sorted(analysis.reads),
        "writes": sorted(analysis.writes),
        "reads_unknown": analysis.reads_unknown,
        "writes_unknown": analysis.writes_unknown,
        "reason": analysis.reason,
    }


def analysis_from_json(data: dict[str, Any]) -> CallAnalysis:
    return CallAnalysis(
        canonical=data["canonical"],
        reads=frozenset(data["reads"]),
        writes=frozenset(data["writes"]),
        reads_unknown=data["reads_unknown"],
        writes_unknown=data["writes_unknown"],
        reason=data["reason"],
    )


@dataclass
class Step:
    call: dict[str, Any]
    cwd: str | None
    database: str | None
    oracle: dict[str, Any] | None  # None for collected (real) traces
    intent: str  # generator label, for analysis and plots only

    @property
    def ctx(self) -> CallContext:
        return CallContext(cwd=self.cwd, database=self.database)


@dataclass
class Task:
    task_id: str
    initial_state: dict[str, Any]
    rollouts: list[list[Step]]


@dataclass
class Workload:
    name: str
    kind: str  # "sql" | "fs"
    seed: int
    params: dict[str, Any]
    tasks: list[Task]
    generator_version: int = GENERATOR_VERSION

    @property
    def total_calls(self) -> int:
        return sum(len(r) for t in self.tasks for r in t.rollouts)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), separators=(",", ":")) + "\n", encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> "Workload":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        tasks = [
            Task(
                t["task_id"],
                t["initial_state"],
                [[Step(**s) for s in rollout] for rollout in t["rollouts"]],
            )
            for t in data.pop("tasks")
        ]
        return cls(tasks=tasks, **data)


@dataclass
class Perturbation:
    """Per-step probabilities used when sampling a rollout from a plan."""

    p_variant: float = 0.35  # use a non-default spelling
    p_noise: float = 0.3  # insert a noise intent before the step
    p_drop: float = 0.1  # skip the step
    p_stop: float = 0.06  # end the rollout after the step
    min_steps: int = 3


@dataclass
class Intent:
    """A call template: spellings share one meaning and one oracle analysis."""

    label: str
    # A spelling is either text, or (text, output_tag). Spellings whose output
    # differs (e.g. SQLite labels unaliased columns with the expression text
    # as typed) get different tags, so the oracle keeps them apart.
    spellings: list[str | tuple[str, str]]
    oracle: CallAnalysis
    effect: Callable[["_Sim"], None] = lambda sim: None


@dataclass
class _Sim:
    """Generator-side logical state (only what spellings depend on)."""

    cwd: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _sample_rollout(
    rng: random.Random,
    plan: list[Callable[[_Sim], Intent]],
    noise: list[Callable[[_Sim, random.Random], Intent]],
    sim_factory: Callable[[], _Sim],
    make_call: Callable[[str], dict[str, Any]],
    database: str | None,
    p: Perturbation,
) -> list[Step]:
    sim = sim_factory()
    steps: list[Step] = []

    def emit(intent: Intent) -> None:
        if len(intent.spellings) > 1 and rng.random() < p.p_variant:
            spelling = rng.choice(intent.spellings[1:])
        else:
            spelling = intent.spellings[0]
        oracle = analysis_to_json(intent.oracle)
        if isinstance(spelling, tuple):
            spelling, tag = spelling
            oracle["canonical"] = f"{oracle['canonical']} [output:{tag}]"
        steps.append(
            Step(
                call=make_call(spelling),
                cwd=sim.cwd,
                database=database,
                oracle=oracle,
                intent=intent.label,
            )
        )
        intent.effect(sim)

    for build in plan:
        if rng.random() < p.p_noise:
            emit(rng.choice(noise)(sim, rng))
        if rng.random() < p.p_drop:
            continue
        emit(build(sim))
        if len(steps) >= p.min_steps and rng.random() < p.p_stop:
            break
    return steps


# --------------------------------------------------------------------- SQL

DB = "main"
SQL_TABLES = ("users", "orders", "audit_log", "products")
DEPTS = ("eng", "ops", "sales")
LOG_MESSAGES = ("checked user", "reviewed orders", "audit pass")


def _t(name: str) -> str:
    return sql_table(DB, name)


def _sql_intent(
    label: str, spellings: list[str | tuple[str, str]], reads: set[str] = frozenset(),
    writes: set[str] = frozenset(),
) -> Intent:
    return Intent(
        label, spellings,
        CallAnalysis(canonical=label, reads=frozenset(reads), writes=frozenset(writes)),
    )


def sql_list_tables() -> Intent:
    return _sql_intent(
        "list_tables()",
        [
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            "select name from sqlite_master where type = 'table' order by name",
        ],
        reads={_t(t) for t in SQL_TABLES},
    )


def sql_describe(table: str) -> Intent:
    return _sql_intent(
        f"describe({table})",
        [f"PRAGMA table_info({table})", f"pragma table_info({table})"],
        reads={_t(table)},
    )


def sql_get_user(uid: int) -> Intent:
    return _sql_intent(
        f"get_user({uid})",
        [
            f"SELECT name, email, salary FROM users WHERE id = {uid}",
            f"select name, email, salary from users where id={uid}",
            f"SELECT name,email,salary\nFROM users\nWHERE id = {uid};",
            f"SELECT u.name, u.email, u.salary FROM users AS u WHERE u.id = {uid}",
        ],
        reads={_t("users")},
    )


def sql_user_orders(uid: int) -> Intent:
    return _sql_intent(
        f"user_orders({uid})",
        [
            f"SELECT id, amount FROM orders WHERE user_id = {uid} ORDER BY id",
            f"select id, amount from orders where user_id={uid} order by id",
            f"SELECT o.id, o.amount FROM orders o WHERE o.user_id = {uid} ORDER BY o.id",
        ],
        reads={_t("orders")},
    )


def sql_order_total(uid: int) -> Intent:
    return _sql_intent(
        f"order_total({uid})",
        [
            (f"SELECT SUM(amount) FROM orders WHERE user_id = {uid}", "SUM"),
            (f"select sum(amount) from orders where user_id = {uid}", "sum"),
            (f"SELECT  SUM(amount)  FROM  orders  WHERE  user_id={uid}", "SUM"),
        ],
        reads={_t("orders")},
    )


def sql_dept_salaries(dept: str) -> Intent:
    return _sql_intent(
        f"dept_salaries({dept})",
        [
            f"SELECT name, salary FROM users WHERE dept = '{dept}' ORDER BY salary DESC, id",
            f"select name, salary from users where dept='{dept}' order by salary desc, id",
        ],
        reads={_t("users")},
    )


def sql_spend_report() -> Intent:
    return _sql_intent(
        "spend_report()",
        [
            ("SELECT u.name, SUM(o.amount) FROM users u JOIN orders o ON o.user_id = u.id "
             "GROUP BY u.id ORDER BY u.id", "SUM(o.amount)"),
            ("select u.name, sum(o.amount) from users u join orders o on o.user_id = u.id "
             "group by u.id order by u.id", "sum(o.amount)"),
            ("SELECT users.name, SUM(orders.amount) FROM users JOIN orders "
             "ON orders.user_id = users.id GROUP BY users.id ORDER BY users.id",
             "SUM(orders.amount)"),
            ("SELECT u.name, SUM(o.amount) FROM users AS u JOIN orders AS o "
             "ON o.user_id = u.id GROUP BY u.id ORDER BY u.id", "SUM(o.amount)"),
        ],
        reads={_t("users"), _t("orders")},
    )


def sql_count(table: str) -> Intent:
    return _sql_intent(
        f"count({table})",
        [(f"SELECT COUNT(*) FROM {table}", "COUNT"), (f"select count(*) from {table}", "count"),
         (f"SELECT COUNT(*)\nFROM {table};", "COUNT")],
        reads={_t(table)},
    )


def sql_price(pid: int) -> Intent:
    return _sql_intent(
        f"price({pid})",
        [
            f"SELECT name, price FROM products WHERE id = {pid}",
            f"select name, price from products where id = {pid}",
        ],
        reads={_t("products")},
    )


def sql_log(message: str) -> Intent:
    return _sql_intent(
        f"log({message})",
        [
            f"INSERT INTO audit_log (msg) VALUES ('{message}')",
            f"insert into audit_log(msg) values ('{message}')",
        ],
        writes={_t("audit_log")},
    )


def sql_raise(uid: int, amount: int) -> Intent:
    return _sql_intent(
        f"raise({uid},{amount})",
        [
            f"UPDATE users SET salary = salary + {amount} WHERE id = {uid}",
            f"update users set salary=salary+{amount} where id={uid}",
        ],
        reads={_t("users")},
        writes={_t("users")},
    )


def sql_add_order(uid: int, amount: int) -> Intent:
    return _sql_intent(
        f"add_order({uid},{amount})",
        [
            f"INSERT INTO orders (user_id, amount) VALUES ({uid}, {amount})",
            f"insert into orders(user_id, amount) values ({uid},{amount})",
        ],
        writes={_t("orders")},
    )


def _sql_schema(rng: random.Random) -> str:
    names = ["ada", "bob", "cy", "dee", "eve", "fay", "gus", "hal"]
    lines = [
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT,"
        " dept TEXT, salary INTEGER);",
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER);",
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, msg TEXT);",
        "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price INTEGER);",
    ]
    for uid, name in enumerate(names, start=1):
        lines.append(
            f"INSERT INTO users VALUES ({uid}, '{name}', '{name}@example.com',"
            f" '{rng.choice(DEPTS)}', {rng.randrange(50, 150) * 1000});"
        )
    for oid in range(1, 25):
        lines.append(
            f"INSERT INTO orders VALUES ({oid}, {rng.randrange(1, len(names) + 1)},"
            f" {rng.randrange(5, 500)});"
        )
    for pid in range(1, 6):
        lines.append(f"INSERT INTO products VALUES ({pid}, 'p{pid}', {rng.randrange(1, 99)});")
    return "\n".join(lines)


def generate_sql(
    name: str = "sql", seed: int = 0, tasks: int = 6, rollouts: int = 8,
    perturbation: Perturbation | None = None,
) -> Workload:
    p = perturbation or Perturbation()
    rng = random.Random(seed)
    out: list[Task] = []
    for index in range(tasks):
        task_rng = random.Random(rng.random())
        uid = task_rng.randrange(1, 9)
        other = task_rng.choice([u for u in range(1, 9) if u != uid])
        dept = task_rng.choice(DEPTS)
        amount = task_rng.choice((500, 1000, 2500))
        body: list[Callable[[_Sim], Intent]] = [
            lambda s, u=uid: sql_get_user(u),
            lambda s, u=uid: sql_user_orders(u),
            lambda s, u=uid: sql_order_total(u),
            lambda s, d=dept: sql_dept_salaries(d),
            lambda s: sql_spend_report(),
        ]
        task_rng.shuffle(body)
        plan: list[Callable[[_Sim], Intent]] = [
            lambda s: sql_list_tables(),
            lambda s: sql_describe("users"),
            *body[:3],
            lambda s, u=uid: sql_log(f"reviewed {u}"),
            lambda s, u=uid, a=amount: sql_raise(u, a),
            lambda s, u=uid: sql_get_user(u),
            *body[3:],
        ]
        noise: list[Callable[[_Sim, random.Random], Intent]] = [
            lambda s, r: sql_log(r.choice(LOG_MESSAGES)),
            lambda s, r: sql_log(r.choice(LOG_MESSAGES)),
            lambda s, r: sql_count(r.choice(SQL_TABLES)),
            lambda s, r: sql_price(r.randrange(1, 4)),
            lambda s, r, o=other: sql_add_order(o, r.choice((10, 20))),
            lambda s, r, o=other: sql_raise(o, 100),
            lambda s, r, u=uid: sql_get_user(u),
            lambda s, r: sql_describe(r.choice(("orders", "products"))),
        ]
        out.append(
            Task(
                task_id=f"{name}-task{index}",
                initial_state={"schema_sql": _sql_schema(task_rng)},
                rollouts=[
                    _sample_rollout(task_rng, plan, noise, _Sim, sql_call, DB, p)
                    for _ in range(rollouts)
                ],
            )
        )
    return Workload(name, "sql", seed, {"tasks": tasks, "rollouts": rollouts,
                                        **asdict(p)}, out)


# --------------------------------------------------------------- terminal

APP = "/app"
NOTES_LINES = ("check config", "rerun tests", "ask reviewer")


def _spellings(sim: _Sim, path: str, *, is_dir: bool) -> list[str]:
    """Default (relative) spelling first, then equivalent alternatives."""
    rel = posixpath.relpath(path, sim.cwd)
    options = ["." if rel == "." else rel]
    if rel == ".":
        options += ["./", path]
    else:
        if not rel.startswith(".."):
            options.append("./" + rel)
        options.append(path)
        if is_dir:
            options.append(rel + "/")
    seen: list[str] = []
    for option in options:
        if option not in seen:
            seen.append(option)
    return seen


def fs_ls(sim: _Sim, path: str) -> Intent:
    spellings = [f"ls {s}" for s in _spellings(sim, path, is_dir=True)]
    if path == sim.cwd:
        spellings.insert(0, "ls")
    return Intent(
        f"ls {path}", spellings,
        CallAnalysis(f"ls {path}", reads=frozenset({fs_dir(path)})),
    )


def fs_ls_long(sim: _Sim, path: str) -> Intent:
    return Intent(
        f"ls -l {path}",
        [f"ls -l {s}" for s in _spellings(sim, path, is_dir=True)],
        CallAnalysis(None, reads_unknown=True, reason="unsupported flag -l"),
    )


def fs_cat(sim: _Sim, path: str) -> Intent:
    return Intent(
        f"cat {path}",
        [f"cat {s}" for s in _spellings(sim, path, is_dir=False)],
        CallAnalysis(f"cat {path}", reads=frozenset({fs_file(path)})),
    )


def fs_cd(sim: _Sim, path: str) -> Intent:
    def effect(s: _Sim) -> None:
        s.cwd = path

    return Intent(
        f"cd {path}",
        [f"cd {s}" for s in _spellings(sim, path, is_dir=True) if s not in (".", "./")],
        CallAnalysis(f"cd {path}", writes=frozenset({CWD})),
        effect,
    )


def fs_echo(sim: _Sim, path: str, text: str, *, append: bool) -> Intent:
    op = ">>" if append else ">"
    return Intent(
        f"echo {op} {path}",
        [f"echo {text} {op} {s}" for s in _spellings(sim, path, is_dir=False)],
        CallAnalysis(
            f"echo {text} {op} {path}",
            writes=frozenset({fs_file(path), fs_dir(posixpath.dirname(path))}),
        ),
    )


def fs_grep(sim: _Sim, pattern: str, path: str) -> Intent:
    return Intent(
        f"grep {path}",
        [f"grep {pattern} {s}" for s in _spellings(sim, path, is_dir=False)],
        CallAnalysis(None, reads_unknown=True, reason="unsupported command: grep"),
    )


def fs_python(sim: _Sim, path: str) -> Intent:
    return Intent(
        f"python {path}",
        [f"python {s}" for s in _spellings(sim, path, is_dir=False)],
        unknown("unsupported command: python"),
    )


def fs_compound(sim: _Sim, path: str) -> Intent:
    rel = _spellings(sim, path, is_dir=True)[0]
    return Intent(
        "compound", [f"cd {rel} && ls", f"cd {path} && ls"],
        unknown("compound command"),
        lambda s: setattr(s, "cwd", path),
    )


def _fs_initial(rng: random.Random) -> dict[str, Any]:
    level = rng.randrange(1, 5)
    return {
        "cwd": APP,
        "dirs": [APP, f"{APP}/src", f"{APP}/data"],
        "files": {
            f"{APP}/README.md": "# demo\nrun: python src/main.py\n",
            f"{APP}/config.json": f'{{"debug": false, "level": {level}}}\n',
            f"{APP}/notes.txt": "TODO check config\n",
            f"{APP}/src/main.py": "import util\nprint(util.f())\n# TODO refactor\n",
            f"{APP}/src/util.py": f"def f():\n    return {rng.randrange(1, 9)}\n",
            f"{APP}/data/a.csv": "id,v\n1,10\n2,20\n",
            f"{APP}/data/b.csv": f"id,v\n3,{rng.randrange(10, 99)}\n",
        },
    }


def generate_fs(
    name: str = "fs", seed: int = 0, tasks: int = 6, rollouts: int = 8,
    perturbation: Perturbation | None = None,
) -> Workload:
    p = perturbation or Perturbation()
    rng = random.Random(seed)
    src, data = f"{APP}/src", f"{APP}/data"
    config, notes = f"{APP}/config.json", f"{APP}/notes.txt"
    out: list[Task] = []
    for index in range(tasks):
        task_rng = random.Random(rng.random())
        new_level = task_rng.randrange(5, 9)
        note = task_rng.choice(NOTES_LINES)
        explore: list[Callable[[_Sim], Intent]] = [
            lambda s: fs_cat(s, f"{APP}/README.md"),
            lambda s: fs_ls(s, data),
            lambda s: fs_cat(s, f"{data}/a.csv"),
            lambda s: fs_cat(s, notes),
        ]
        task_rng.shuffle(explore)
        plan: list[Callable[[_Sim], Intent]] = [
            lambda s: fs_ls(s, APP),
            *explore[:2],
            lambda s: fs_cat(s, config),
            lambda s: fs_cd(s, src),
            lambda s: fs_ls(s, src),
            lambda s: fs_cat(s, f"{src}/main.py"),
            lambda s: fs_grep(s, "TODO", f"{src}/main.py"),
            lambda s: fs_cat(s, f"{src}/util.py"),
            lambda s: fs_cd(s, APP),
            lambda s, n=note: fs_echo(s, notes, n, append=True),
            lambda s: fs_cat(s, config),
            lambda s, lv=new_level: fs_echo(
                s, config, f'{{"debug":true,"level":{lv}}}', append=False
            ),
            lambda s: fs_cat(s, config),
            lambda s: fs_python(s, f"{src}/main.py"),
            *explore[2:],
            lambda s: fs_cat(s, config),
        ]
        noise: list[Callable[[_Sim, random.Random], Intent]] = [
            lambda s, r: fs_echo(s, notes, r.choice(NOTES_LINES), append=True),
            lambda s, r: fs_echo(s, notes, r.choice(NOTES_LINES), append=True),
            lambda s, r: fs_cat(s, r.choice((config, notes, f"{APP}/README.md"))),
            lambda s, r: fs_ls(s, r.choice((APP, src, data))),
            lambda s, r: fs_echo(s, f"{data}/c.csv", "id,v", append=False),
            lambda s, r: fs_ls_long(s, data),
            lambda s, r: fs_echo(s, config, '{"debug":true}', append=False),
            lambda s, r: fs_compound(s, src) if s.cwd == APP else fs_ls(s, s.cwd),
        ]
        out.append(
            Task(
                task_id=f"{name}-task{index}",
                initial_state=_fs_initial(task_rng),
                rollouts=[
                    _sample_rollout(
                        task_rng, plan, noise, lambda: _Sim(cwd=APP), bash_call, None, p
                    )
                    for _ in range(rollouts)
                ],
            )
        )
    return Workload(name, "fs", seed, {"tasks": tasks, "rollouts": rollouts,
                                       **asdict(p)}, out)


GENERATORS = {"sql": generate_sql, "fs": generate_fs}
