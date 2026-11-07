"""Server-free tests for the evaluation harness (fuzzytv.eval)."""

import json
from pathlib import Path

import pytest

from fuzzytv.eval.calls import bash_call, sql_call
from fuzzytv.eval.canonicalizers import LooseCanonicalizer, OracleCanonicalizer
from fuzzytv.eval.memory_backend import MemoryClient, MemoryStore
from fuzzytv.eval.runner import (
    CanonicalKeyedCall,
    Mode,
    RunConfig,
    run_mode,
    summarize,
    wilson_interval,
)
from fuzzytv.eval.sandboxes import (
    EvalCall,
    SandboxCounters,
    ShellState,
    run_shell,
    sqlite_sandbox_class,
)
from fuzzytv.eval.workloads import Perturbation, Workload, generate_fs, generate_sql
from fuzzytv.interfaces import CallAnalysis, CallContext

FAST = RunConfig(latency_scale=0.0)


def small(kind: str, seed: int = 3) -> Workload:
    generate = generate_sql if kind == "sql" else generate_fs
    return generate(seed=seed, tasks=2, rollouts=5)


# ---------------------------------------------------------- memory backend


def _client() -> MemoryClient:
    return type("Bound", (MemoryClient,), {"store": MemoryStore()})()


async def test_memory_insert_requires_parents_and_find_is_exact() -> None:
    client = _client()
    assert not (await client.insert("t", ["a", "b"], "v", None, 0)).value_inserted
    assert (await client.insert("t", ["a"], "va", None, 0)).value_inserted
    assert (await client.insert("t", ["a", "b"], "vb", None, 0)).value_inserted
    assert (await client.find_unmanaged("t", ["a", "b"])).value == "vb"
    assert (await client.find_unmanaged("t", ["a", "c"])).value is None
    assert (await client.find_unmanaged("other", ["a"])).value is None


async def test_memory_find_returns_deepest_sandbox_with_lease() -> None:
    client = _client()
    await client.insert("t", ["a"], "va", "s1", 0)
    await client.insert("t", ["a", "b"], "vb", "s2", 0)
    first = await client.insert("t", ["a", "b"], None, "s3", 0)
    assert not first.sandbox_inserted  # first sandbox wins
    async with await client.find("t", ["a", "b", "c"]) as found:
        assert found.value is None
        assert (found.sandbox_id, found.sandbox_path) == ("s2", ["a", "b"])
        assert client.store.outstanding_leases() == 1
    assert client.store.outstanding_leases() == 0
    # An insert without a value keeps the existing one.
    assert (await client.find_unmanaged("t", ["a", "b"])).value == "vb"


async def test_memory_fork_cost_ema() -> None:
    client = _client()
    assert await client.report_fork_cost("t", 100) == 100
    assert await client.report_fork_cost("t", 200) == 130


# ------------------------------------------------------------- sandboxes


def _shell() -> ShellState:
    return ShellState.from_initial(
        {"cwd": "/app", "files": {"/app/a.txt": "hello\n", "/app/d/x": "1\n"}}
    )


def test_shell_cat_ls_cd_and_paths() -> None:
    state = _shell()
    assert run_shell(state, "cat ./a.txt") == "exit=0\nhello\n"
    assert run_shell(state, "cat /app/a.txt") == run_shell(state, "cat a.txt")
    assert run_shell(state, "ls") == "exit=0\na.txt\nd\n"
    assert run_shell(state, "cd d") == "exit=0\n"
    assert state.cwd == "/app/d"
    assert run_shell(state, "cat ../a.txt") == "exit=0\nhello\n"
    assert run_shell(state, "cat nope").startswith("exit=1\n")


def test_shell_writes_and_unknown_commands() -> None:
    state = _shell()
    run_shell(state, "echo hi there > b.txt")
    run_shell(state, "echo more >> b.txt")
    assert state.files["/app/b.txt"] == "hi there\nmore\n"
    assert run_shell(state, "ls -l").splitlines()[1] == "-      6 a.txt"
    assert run_shell(state, "grep hi b.txt") == "exit=0\nhi there\n"
    assert run_shell(state, "nosuch").startswith("exit=127")
    assert run_shell(state, "cd d && ls") == "exit=0\nx\n"


async def test_sqlite_sandbox_fork_is_isolated() -> None:
    counters = SandboxCounters()
    cls = sqlite_sandbox_class(
        {"schema_sql": "CREATE TABLE t(v); INSERT INTO t VALUES (1);"}, counters
    )
    live = cls("task")
    await live.start()
    snapshot = await live.fork()
    await live.execute(EvalCall(sql_call("UPDATE t SET v = 2")))
    restored = cls("task", env_id=snapshot.get_id())
    await restored.start()
    assert "[[1]]" in await restored.execute(EvalCall(sql_call("SELECT v FROM t")))
    assert "[[2]]" in await live.execute(EvalCall(sql_call("SELECT v FROM t")))
    assert (await live.execute(EvalCall(sql_call("SELEC")))).startswith("exit=1")
    await live.stop()
    await restored.stop()
    assert not counters.live


# ------------------------------------------------------------- workloads


@pytest.mark.parametrize("kind", ["sql", "fs"])
def test_generation_is_deterministic_and_round_trips(kind: str, tmp_path: Path) -> None:
    first, second = small(kind), small(kind)
    assert first == second
    assert small(kind, seed=4) != first
    path = tmp_path / "w.json"
    first.save(path)
    assert Workload.load(path) == first
    assert first.total_calls == sum(len(r) for t in first.tasks for r in t.rollouts)


@pytest.mark.parametrize("kind", ["sql", "fs"])
def test_oracle_is_consistent_per_call_and_context(kind: str) -> None:
    OracleCanonicalizer(generate_sql(seed=9) if kind == "sql" else generate_fs(seed=9))


def test_generated_calls_use_shared_schema() -> None:
    sql_step = small("sql").tasks[0].rollouts[0][0]
    fs_step = small("fs").tasks[0].rollouts[0][0]
    assert set(sql_step.call) == set(sql_call("x"))
    assert set(fs_step.call) == set(bash_call("x"))
    assert fs_step.cwd == "/app" and sql_step.database == "main"


def test_perturbation_zero_gives_identical_rollouts() -> None:
    quiet = Perturbation(p_variant=0, p_noise=0, p_drop=0, p_stop=0)
    workload = generate_fs(seed=1, tasks=1, rollouts=3, perturbation=quiet)
    rollouts = workload.tasks[0].rollouts
    assert rollouts[0] == rollouts[1] == rollouts[2]


# ---------------------------------------------------------- canonicalizers


def test_loose_drops_read_dependencies() -> None:
    class Inner:
        def analyze(self, call: dict, ctx: CallContext) -> CallAnalysis:
            return CallAnalysis("c", reads=frozenset({"fs:file:/a"}), reads_unknown=True)

    loose = LooseCanonicalizer(Inner()).analyze({}, CallContext())
    assert loose.fuzzy_eligible and not loose.reads


def test_canonical_keyed_call_executes_original() -> None:
    call = bash_call("cat ./a")
    keyed = CanonicalKeyedCall(call, CallAnalysis("cat /app/a"), mutating=False)
    assert keyed.to_dict() == call
    assert json.loads(keyed.serialize()) == {"fuzzytv_canonical_v1": "cat /app/a"}
    assert not keyed.will_mutate_state()
    fallback = CanonicalKeyedCall(call, CallAnalysis(None))
    assert fallback.serialize() == EvalCall(call).serialize()


# ------------------------------------------------------------------ runner


def test_mode_parse() -> None:
    assert Mode.parse("relevant_state").name == "relevant_state:oracle"
    assert Mode.parse("exact").canonicalizer is None
    with pytest.raises(ValueError):
        Mode.parse("exact:oracle")
    with pytest.raises(ValueError):
        Mode.parse("fuzzy")


@pytest.mark.parametrize("kind", ["sql", "fs"])
@pytest.mark.parametrize("hints", [False, True])
async def test_all_modes_are_clean_and_ordered(kind: str, hints: bool) -> None:
    workload = small(kind)
    config = RunConfig(latency_scale=0.0, read_only_hints=hints)
    summaries = {}
    for name in ("none", "exact", "canonical:oracle", "relevant_state:oracle"):
        result = await run_mode(workload, Mode.parse(name), config)
        s = summarize(result, workload, config)
        summaries[name] = s
        assert s["calls"] == workload.total_calls
        assert s["validated_calls"] == s["calls"]  # sample_rate 1.0
        assert s["false_exact_hits"] == 0
        assert s["false_fuzzy_hits"] == 0  # oracle is spec-correct
        assert s["mismatched_executions"] == 0
        assert s["leaked_sandboxes"] == 0
        assert s["outstanding_leases"] in (0, None)
    assert summaries["none"]["hit_rate"] == 0
    assert summaries["exact"]["fuzzy_hits"] == 0
    assert summaries["canonical:oracle"]["fuzzy_hits"] == 0
    assert (
        summaries["exact"]["hit_rate"]
        <= summaries["canonical:oracle"]["hit_rate"]
    )
    assert summaries["relevant_state:oracle"]["fuzzy_hits"] > 0


async def test_validator_detects_false_hits_of_loose_matching() -> None:
    workload = generate_fs(seed=1, tasks=2, rollouts=8)
    result = await run_mode(workload, Mode.parse("relevant_state:loose"), FAST)
    s = summarize(result, workload, FAST)
    assert s["false_fuzzy_hits"] > 0
    bad = [r for r in result.records if r.matched is False]
    assert all(r.source == "fuzzy" for r in bad)
    assert all(r.returned is not None and r.reference is not None for r in bad)


async def test_sampling_validates_whole_rollouts_deterministically() -> None:
    workload = small("sql")
    config = RunConfig(latency_scale=0.0, sample_rate=0.4, seed=7)
    first = await run_mode(workload, Mode.parse("exact"), config)
    second = await run_mode(workload, Mode.parse("exact"), config)
    picked = {(r.task_id, r.rollout) for r in first.records if r.validated}
    assert picked == {(r.task_id, r.rollout) for r in second.records if r.validated}
    assert 0 < len(picked) < 10
    for record in first.records:
        assert record.validated == ((record.task_id, record.rollout) in picked)
    s = summarize(first, workload, config)
    assert s["reference_executions"] == s["validated_calls"]


def test_wilson_interval() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(0, 200)
    assert low == 0.0 and 0 < high < 0.02
    low, high = wilson_interval(50, 100)
    assert low < 0.5 < high


@pytest.mark.parametrize("name", ["none", "exact", "relevant_state:oracle"])
async def test_rollouts_without_tool_calls_are_skipped(name: str) -> None:
    # A model can answer without calling a tool; replay must not misalign records.
    workload = small("sql")
    workload.tasks[0].rollouts.insert(1, [])
    workload.tasks[1].rollouts.append([])
    result = await run_mode(workload, Mode.parse(name), FAST)
    s = summarize(result, workload, FAST)
    assert s["calls"] == workload.total_calls
    assert s["validated_calls"] == s["calls"]
    assert s["false_exact_hits"] == s["false_fuzzy_hits"] == 0
