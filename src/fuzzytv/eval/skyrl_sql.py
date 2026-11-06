"""Collect SkyRL-SQL rollouts as harness traces (the TVCache paper's SQL workload).

Reproduces the multi-turn text-to-SQL environment of NovaSky-AI/SkyRL
(`skyrl-gym/skyrl_gym/envs/sql/env.py` and `tools/sql.py`), with the training
recipe's settings (`examples/train/text_to_sql/run_skyrl_sql.sh`):

- data: NovaSky-AI/SkyRL-SQL-653-data-newfmt (653 training questions);
  databases from seeklhy/OmniSQL-datasets (SynSQL-2.5M and Spider).
- model Qwen/Qwen2.5-Coder-7B-Instruct, 5 samples per question, temperature
  0.6, top-p 0.95, stop at `</sql>` / `</solution>`, at most 6 turns, at most
  3000 generated tokens per turn, observations appended to the same assistant
  turn (`use_conversation_multi_turn=false`).
- tool: one `<sql>...</sql>` query per turn, executed in a rolled-back
  transaction (so the workload is read-only, as in the paper), shown as a
  pandas table inside `<observation>` with a turns-left reminder.

Collection is inference only (no training). `--passes k` re-samples every
question k times to approximate the paper's repeated epochs. Each pass is
appended to the same task, so later passes can hit entries from earlier ones.

    python -m fuzzytv.eval.skyrl_sql --data $D/train.parquet --db-root $DB \\
        --model Qwen/Qwen2.5-Coder-7B-Instruct --shard 0/4 --out shard0.json

Output: a harness trace (`Workload`, kind "skyrl-sql") plus `<out>.rollouts.jsonl`
with the full text, turns, solution and reward of every trajectory.
"""

import argparse
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fuzzytv.eval.calls import sql_call
from fuzzytv.eval.workloads import Step, Task, Workload

MODEL = "Qwen/Qwen2.5-Coder-7B-Instruct"
STOP = ["</sql>", "</solution>"]
MAX_TURNS = 6
TEMPERATURE = 0.6
TOP_P = 0.95
SAMPLES = 5
MAX_GENERATE = 3000
MAX_INPUT = 29000
SQL_TIMEOUT = 5
DB_SUBDIRS = {
    "synsql": "SynSQL-2.5M/databases",
    "spider": "spider/database",
    "bird": "bird/train/train_databases",
}
INVALID_ACTION = (
    "Your previous action is invalid. Follow the format of outputting thinking "
    "process and sql tool, and try again."
)


def db_file(db_root: str | Path, data: str, db_id: str) -> Path:
    return Path(db_root) / DB_SUBDIRS[data] / db_id / f"{db_id}.sqlite"


def parse_sql(action: str) -> str | None:
    match = re.search(r"<sql>(.*?)</sql>", action, re.DOTALL)
    return match.group(1) if match else None


def run_rolled_back(path: Path, sql: str, timeout: float = SQL_TIMEOUT) -> frozenset | str:
    """Execute like SkyRL's tool: a transaction that is always rolled back."""
    holder: dict[str, Any] = {}
    done = threading.Event()

    def worker() -> None:
        conn = None
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            holder["conn"] = conn
            conn.execute("BEGIN TRANSACTION;")
            holder["value"] = frozenset(conn.execute(sql).fetchall())
        except Exception as error:  # noqa: BLE001 - mirrors the reference tool
            holder["value"] = f"Error executing SQL: {error}, db file: {path}"
        finally:
            if conn is not None:
                try:
                    conn.rollback()
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    if not done.wait(timeout):
        conn = holder.get("conn")
        if conn is not None:
            conn.interrupt()
        done.wait()
        return f"SQL Timeout:\n{sql}"
    return holder["value"]


def skyrl_observation(path: Path, sql: str | None, turns_left: int) -> str:
    """The exact observation text SkyRL shows the model."""
    reminder = f"<reminder>You have {turns_left} turns left to complete the task.</reminder>"
    if sql is None:
        obs = INVALID_ACTION
    else:
        result = run_rolled_back(path, sql)
        if isinstance(result, frozenset):
            import pandas as pd

            frame = pd.DataFrame(result)
            obs = frame.to_string(index=False)
            if len(obs) > 9000:
                obs = "Truncated to 50 lines since returned response too long: " + (
                    frame.head(50).to_string(index=False)
                )
        else:
            obs = str(result)
    return f"\n\n<observation>{obs}\n{reminder}</observation>\n\n"


def solution_reward(text: str, gold_sql: str, path: Path) -> float:
    """Execution-match reward (format checks simplified; for sanity statistics)."""
    match = re.findall(r"<solution>(.*?)</solution>", text, re.DOTALL)
    if len(match) != 1:
        return -1.0
    predicted = run_rolled_back(path, match[0], timeout=30)
    gold = run_rolled_back(path, gold_sql, timeout=30)
    return float(isinstance(predicted, frozenset) and predicted == gold)


@dataclass
class Trajectory:
    task: int
    sample: int
    pass_index: int
    text: str
    turns: int = 0
    calls: list[str] = field(default_factory=list)
    done: bool = False
    stop_reason: str = ""


Generator = Callable[[Sequence[str]], list[str]]


def collect(
    tasks: list[dict[str, Any]],
    generate: Generator,
    render: Callable[[list[dict[str, str]]], str],
    db_root: str | Path,
    *,
    samples: int = SAMPLES,
    passes: int = 1,
    max_turns: int = MAX_TURNS,
    count_tokens: Callable[[str], int] | None = None,
    max_input: int = MAX_INPUT,
) -> list[Trajectory]:
    """Run every trajectory of a pass in lockstep, one batched generate per turn.

    Mirrors SQLEnv.step: the turn counter increments first; the episode ends on
    a complete <solution> or when turns reach max_turns (the last action is not
    executed); otherwise the <sql> is executed and the observation appended.
    """
    finished: list[Trajectory] = []
    for pass_index in range(passes):
        active = [
            Trajectory(task_index, sample, pass_index, render(task["prompt"]))
            for task_index, task in enumerate(tasks)
            for sample in range(samples)
        ]
        while active:
            outputs = generate([t.text for t in active])
            still_active = []
            for trajectory, action in zip(active, outputs, strict=True):
                trajectory.text += action
                trajectory.turns += 1
                task = tasks[trajectory.task]
                if "<solution>" in action and "</solution>" in action:
                    trajectory.done, trajectory.stop_reason = True, "solution"
                elif trajectory.turns >= max_turns:
                    trajectory.done, trajectory.stop_reason = True, "max_turns"
                else:
                    sql = parse_sql(action)
                    if sql is not None:
                        trajectory.calls.append(sql)
                    trajectory.text += skyrl_observation(
                        db_file(db_root, task["data"], task["db_id"]), sql,
                        max_turns - trajectory.turns,
                    )
                    if count_tokens is not None and count_tokens(trajectory.text) > max_input:
                        trajectory.done, trajectory.stop_reason = True, "max_input"
                (finished if trajectory.done else still_active).append(trajectory)
            active = still_active
    return finished


def to_workload(
    name: str, tasks: list[dict[str, Any]], trajectories: list[Trajectory],
    params: dict[str, Any],
) -> Workload:
    by_task: dict[int, list[Trajectory]] = {}
    for t in sorted(trajectories, key=lambda t: (t.task, t.pass_index, t.sample)):
        by_task.setdefault(t.task, []).append(t)
    out = []
    for index, task in enumerate(tasks):
        out.append(Task(
            task_id=f"skyrl-sql-{task['index']}",
            initial_state={"data": task["data"], "db_id": task["db_id"]},
            rollouts=[
                [Step(call=sql_call(q), cwd=None, database="main", oracle=None, intent="sql")
                 for q in t.calls]
                for t in by_task.get(index, [])
            ],
        ))
    # SkyRL executes every query in a rolled-back transaction: no call mutates
    # state, which is how the TVCache paper treats this workload.
    return Workload(name, "skyrl-sql", 0, {"will_mutate_state": False, **params}, out)


def load_tasks(parquet: Path, shard: int, shards: int, limit: int | None) -> list[dict[str, Any]]:
    import pandas as pd

    frame = pd.read_parquet(parquet)
    tasks = []
    for position, row in enumerate(frame.itertuples(index=False)):
        if position % shards != shard:
            continue
        tasks.append({
            "index": position,
            "prompt": [dict(m) for m in row.prompt],
            "data": row.data,
            "db_id": row.db_id,
            "gold_sql": row.reward_spec["ground_truth"],
        })
        if limit is not None and len(tasks) >= limit:
            break
    return tasks


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m fuzzytv.eval.skyrl_sql")
    parser.add_argument("--data", type=Path, required=True, help="train.parquet")
    parser.add_argument("--db-root", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS)
    parser.add_argument("--shard", default="0/1", help="i/N: every N-th question from i")
    parser.add_argument("--limit", type=int, help="at most this many questions")
    parser.add_argument("--tensor-parallel", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    shard, shards = (int(x) for x in args.shard.split("/"))

    from vllm import LLM, SamplingParams

    tasks = load_tasks(args.data, shard, shards, args.limit)
    llm = LLM(
        model=args.model, tensor_parallel_size=args.tensor_parallel,
        enable_prefix_caching=True, seed=args.seed + shard, max_model_len=32768,
    )
    tokenizer = llm.get_tokenizer()
    sampling = SamplingParams(
        temperature=TEMPERATURE, top_p=TOP_P, max_tokens=MAX_GENERATE, stop=STOP,
        include_stop_str_in_output=True,
    )

    def generate(prompts: Sequence[str]) -> list[str]:
        return [o.outputs[0].text for o in llm.generate(list(prompts), sampling)]

    def render(messages: list[dict[str, str]]) -> str:
        return tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)

    trajectories = collect(
        tasks, generate, render, args.db_root, samples=args.samples, passes=args.passes,
        max_turns=args.max_turns, count_tokens=lambda text: len(tokenizer.encode(text)),
    )
    params = {
        "model": args.model, "samples": args.samples, "passes": args.passes,
        "max_turns": args.max_turns, "temperature": TEMPERATURE, "top_p": TOP_P,
        "shard": args.shard, "seed": args.seed,
    }
    workload = to_workload(f"skyrl-sql-{shard}of{shards}", tasks, trajectories, params)
    workload.save(args.out)
    with open(str(args.out) + ".rollouts.jsonl", "w") as handle:
        for t in trajectories:
            task = tasks[t.task]
            reward = solution_reward(
                t.text, task["gold_sql"], db_file(args.db_root, task["data"], task["db_id"])
            )
            handle.write(json.dumps({
                **asdict(t), "index": task["index"], "db_id": task["db_id"], "reward": reward,
            }) + "\n")
    calls = sum(len(t.calls) for t in trajectories)
    print(f"{args.out}: {len(tasks)} questions, {len(trajectories)} rollouts, {calls} SQL calls")


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
