"""SkyRL-SQL collection loop and real-trace replay, with a scripted fake model."""

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

pytest.importorskip("pandas")

from fuzzytv.eval.cli import main as cli_main  # noqa: E402
from fuzzytv.eval.runner import Mode, RunConfig, run_mode, summarize  # noqa: E402
from fuzzytv.eval.sandboxes import SKYRL_DB_ROOT_ENV  # noqa: E402
from fuzzytv.eval.skyrl_sql import (  # noqa: E402
    INVALID_ACTION,
    collect,
    db_file,
    parse_sql,
    skyrl_observation,
    response,
    solution_reward,
    to_workload,
)
from fuzzytv.eval.workloads import Workload  # noqa: E402


@pytest.fixture
def db_root(tmp_path: Path) -> Path:
    path = db_file(tmp_path, "synsql", "farm")
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE animals(id INTEGER, species TEXT);"
            "INSERT INTO animals VALUES (1,'pig'),(2,'pig'),(3,'cow');"
        )
    return tmp_path


TASK = {
    "index": 7,
    "prompt": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
    "data": "synsql",
    "db_id": "farm",
    "gold_sql": "SELECT COUNT(*) FROM animals WHERE species = 'pig'",
}


def render(messages: list[dict[str, str]]) -> str:
    return "".join(f"<{m['role']}>{m['content']}" for m in messages) + "<assistant>"


def scripted(script: list[str]):
    """Each trajectory's k-th generation is script[k] (by turn count in text)."""

    def generate(prompts: Sequence[str]) -> list[str]:
        return [script[p.count("<observation>")] for p in prompts]

    return generate


def test_parse_sql() -> None:
    assert parse_sql("<think>x</think><sql>SELECT 1</sql>") == "SELECT 1"
    assert parse_sql("<sql>\nSELECT\n1\n</sql>") == "\nSELECT\n1\n"
    assert parse_sql("no tool") is None


def test_observation_matches_skyrl_format(db_root: Path) -> None:
    path = db_file(db_root, "synsql", "farm")
    obs = skyrl_observation(path, "SELECT COUNT(*) FROM animals", 4)
    assert obs.startswith("\n\n<observation>")
    assert obs.endswith(
        "\n<reminder>You have 4 turns left to complete the task.</reminder></observation>\n\n"
    )
    assert " 3" in obs
    assert INVALID_ACTION in skyrl_observation(path, None, 2)
    assert "Error executing SQL" in skyrl_observation(path, "SELEC", 2)


def test_writes_are_rolled_back(db_root: Path) -> None:
    path = db_file(db_root, "synsql", "farm")
    skyrl_observation(path, "DELETE FROM animals", 3)
    assert " 3" in skyrl_observation(path, "SELECT COUNT(*) FROM animals", 3)


def test_collect_follows_env_turn_rules(db_root: Path) -> None:
    script = [
        "<think>a</think><sql>SELECT COUNT(*) FROM animals</sql>",
        "<think>b</think>I forgot the tool",
        "<think>c</think><sql>select count(*) from animals where species='pig'</sql>",
        "<think>d</think><solution>SELECT COUNT(*) FROM animals WHERE species = 'pig'</solution>",
    ]
    trajectories = collect([TASK], scripted(script), render, db_root, samples=3, passes=2)
    assert len(trajectories) == 6
    assert {(t.sample, t.pass_index) for t in trajectories} == {
        (s, p) for s in range(3) for p in range(2)
    }
    t = trajectories[0]
    assert t.stop_reason == "solution" and t.turns == 4
    assert t.calls == [
        "SELECT COUNT(*) FROM animals",
        "select count(*) from animals where species='pig'",
    ]
    assert "You have 5 turns left" in t.text and "You have 3 turns left" in t.text
    assert INVALID_ACTION in t.text
    gold, path = TASK["gold_sql"], db_file(db_root, "synsql", "farm")
    assert solution_reward(response(t), gold, path) == 1.0
    # The prompt's own example <solution> must not count.
    example = [{"role": "user", "content": "<solution>SELECT 1</solution>"}]
    with_example = {**TASK, "prompt": example}
    (u,) = collect([with_example], scripted(script), render, db_root, samples=1)
    assert solution_reward(response(u), gold, path) == 1.0
    assert solution_reward(u.text, gold, path) == -1.0


def test_max_turns_does_not_execute_last_action(db_root: Path) -> None:
    script = ["<sql>SELECT 1</sql>"] * 10
    (t,) = collect([TASK], scripted(script), render, db_root, samples=1, max_turns=3)
    assert t.stop_reason == "max_turns" and t.turns == 3
    assert t.calls == ["SELECT 1", "SELECT 1"]  # third action never executed


def test_input_limit_stops(db_root: Path) -> None:
    script = ["<sql>SELECT * FROM animals</sql>"] * 10
    (t,) = collect([TASK], scripted(script), render, db_root, samples=1,
                   count_tokens=len, max_input=150)
    assert t.stop_reason == "max_input"


async def test_collected_trace_replays_in_harness(
    db_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = [
        "<sql>SELECT species FROM animals</sql>",
        "<sql>SELECT COUNT(*) FROM animals</sql>",
        "<solution>SELECT 1</solution>",
    ]
    trajectories = collect([TASK], scripted(script), render, db_root, samples=4)
    workload = to_workload("toy", [TASK], trajectories, {"model": "fake"})
    path = tmp_path / "toy.json"
    workload.save(path)
    workload = Workload.load(path)
    assert workload.params["will_mutate_state"] is False
    assert workload.tasks[0].task_id == "skyrl-sql-7"
    assert workload.total_calls == 8

    monkeypatch.setenv(SKYRL_DB_ROOT_ENV, str(db_root))
    config = RunConfig(latency_scale=0.0)
    exact = summarize(await run_mode(workload, Mode.parse("exact"), config), workload, config)
    # Stateless calls: rollouts 2-4 repeat rollout 1's queries exactly.
    assert exact["exact_hits"] == 6 and exact["misses"] == 2
    assert exact["false_exact_hits"] == 0 and exact["mismatched_executions"] == 0
    assert exact["validated_calls"] == 8
    with pytest.raises(RuntimeError, match="no oracle"):
        await run_mode(workload, Mode.parse("canonical:oracle"), config)


def test_merge_combines_shards(db_root: Path, tmp_path: Path) -> None:
    script = ["<sql>SELECT 1</sql>", "<solution>SELECT 1</solution>"]
    second = {**TASK, "index": 8}
    paths = []
    for i, task in enumerate((TASK, second)):
        trajectories = collect([task], scripted(script), render, db_root, samples=2)
        part = to_workload(f"part{i}", [task], trajectories, {"shard": f"{i}/2"})
        paths.append(tmp_path / f"part{i}.json")
        part.save(paths[-1])
    out = tmp_path / "merged.json"
    cli_main(["merge", *map(str, paths), "-o", str(out)])
    merged = Workload.load(out)
    assert [t.task_id for t in merged.tasks] == ["skyrl-sql-7", "skyrl-sql-8"]
    assert "shard" not in merged.params and merged.params["will_mutate_state"] is False
