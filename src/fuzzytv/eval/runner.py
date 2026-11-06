"""Run a workload trace under one cache mode, validate, and record every call.

Mode kinds (docs/evaluation.md):

- `none`: execute every call in a per-rollout sandbox; no TVCache.
- `exact`: unmodified upstream `TvcacheToolExecutor`.
- `canonical`: upstream executor, but each call's key element is its
  canonical string (when it has one). The full stateful history is still
  required. The original call is what executes.
- `relevant_state`: `fuzzytv.cache.RelevantStateExecutor` (exact first, then
  relevant-state lookup).

Rollouts of a task run sequentially in trace order, sharing one cache, as
the rollouts of one prompt would in an RL step. Validation picks whole
rollouts at `sample_rate` (seeded). A picked rollout replays every original
call in an independent reference sandbox started from the same initial state,
and every returned value is compared with the reference value. Policy
and reference sandbox work are counted separately.
"""

import hashlib
import json
import random
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, field
from typing import Any

from tvclient.executor import TvcacheToolExecutor
from tvclient.sandbox import Sandbox
from tvclient.tool_call import ToolCall

from fuzzytv.cache.executor import RelevantStateExecutor
from fuzzytv.eval.canonicalizers import TimedCanonicalizer, make_canonicalizer
from fuzzytv.eval.memory_backend import MemoryStore, memory_backend
from fuzzytv.eval.sandboxes import SANDBOX_FACTORIES, EvalCall, SandboxCounters
from fuzzytv.eval.workloads import Step, Task, Workload
from fuzzytv.interfaces import CacheScope, CallAnalysis, CallContext

MODE_KINDS = ("none", "exact", "canonical", "relevant_state")


@dataclass(frozen=True)
class Mode:
    kind: str
    canonicalizer: str | None = None

    @property
    def name(self) -> str:
        return self.kind if self.canonicalizer is None else f"{self.kind}:{self.canonicalizer}"

    @classmethod
    def parse(cls, text: str) -> "Mode":
        kind, _, canon = text.partition(":")
        if kind not in MODE_KINDS:
            raise ValueError(f"unknown mode kind {kind!r}; expected one of {MODE_KINDS}")
        if kind in ("canonical", "relevant_state"):
            return cls(kind, canon or "oracle")
        if canon:
            raise ValueError(f"mode {kind!r} takes no canonicalizer")
        return cls(kind)


DEFAULT_MODES = [
    "none", "exact", "canonical:oracle", "relevant_state:oracle", "relevant_state:loose",
]


@dataclass
class CostModel:
    """Modeled seconds per physical operation (for projected tool time only)."""

    execute: float = 0.05
    start: float = 2.0
    restore: float = 1.0
    fork: float = 0.5
    lookup: float = 0.0005

    def latency(self, scale: float) -> dict[str, float]:
        return {
            "execute": self.execute * scale, "start": self.start * scale,
            "restore": self.restore * scale, "fork": self.fork * scale,
        }

    def seconds(self, counters: SandboxCounters, lookups: int) -> float:
        return (
            counters.executes * self.execute + counters.starts * self.start
            + counters.restores * self.restore + counters.forks * self.fork
            + lookups * self.lookup
        )


@dataclass
class RunConfig:
    backend: str = "memory"  # "memory" | "server"
    host: str = "127.0.0.1"
    port: int = 7878
    sample_rate: float = 1.0
    seed: int = 0
    snapshot: str = "adaptive"  # "always" | "never" | "adaptive" (upstream default)
    cost: CostModel = field(default_factory=CostModel)
    # Policy sandboxes sleep cost * latency_scale per operation, so adaptive
    # snapshotting sees realistic relative costs. 0 disables sleeping.
    latency_scale: float = 0.01
    keep_values: bool = False
    # Mark oracle read-only calls stateless (will_mutate_state() False), as a
    # workload whose tools declare reads would. Off = upstream default.
    read_only_hints: bool = False


@dataclass
class CallRecord:
    mode: str
    task_id: str
    rollout: int
    step: int
    intent: str
    source: str  # "exact" | "fuzzy" | "execute"
    executions: int  # physical executions this call triggered (incl. replay)
    seconds: float
    validated: bool
    matched: bool | None
    oracle_eligible: bool
    reason: str | None
    returned: str | None = None
    reference: str | None = None


class CanonicalKeyedCall(ToolCall):
    """Executes the original call; keys on its canonical form when known."""

    def __init__(
        self, call: dict[str, Any], analysis: CallAnalysis, mutating: bool = True
    ) -> None:
        self.call = call
        self.canonical = analysis.canonical
        self.mutating = mutating

    def will_mutate_state(self) -> bool:
        return self.mutating

    def to_dict(self) -> dict[str, Any]:
        return dict(self.call)

    def serialize(self) -> str:
        if self.canonical is None:
            return super().serialize()
        return json.dumps({"fuzzytv_canonical_v1": self.canonical}, sort_keys=True)


def _oracle_read_only(step: Step) -> bool:
    oracle = step.oracle
    return oracle is not None and not oracle["writes"] and not oracle["writes_unknown"]


def _initial_state_id(task: Task) -> str:
    return hashlib.sha256(
        json.dumps(task.initial_state, sort_keys=True).encode()
    ).hexdigest()


def _executor_options(config: RunConfig) -> dict[str, Any]:
    options: dict[str, Any] = {"host": config.host, "port": config.port}
    if config.snapshot == "adaptive":
        options["adaptive"] = True
        # Upstream seeds its fork-cost estimate with 1 real second. Scale it
        # like the simulated latencies so the policy prices replay correctly.
        if config.latency_scale > 0:
            options["initial_fork_cost_seconds"] = config.cost.fork * config.latency_scale
    elif config.snapshot in ("always", "never"):
        options["adaptive"] = False
        options["fixed_threshold_seconds"] = 1e-12 if config.snapshot == "always" else 1e12
    else:
        raise ValueError(f"unknown snapshot policy {config.snapshot!r}")
    return options


@contextmanager
def _backend(config: RunConfig) -> Iterator[MemoryStore | None]:
    if config.backend == "memory":
        with memory_backend() as store:
            yield store
    elif config.backend == "server":
        with nullcontext():
            yield None
    else:
        raise ValueError(f"unknown backend {config.backend!r}")


async def _reference_values(
    sandbox_class: type[Sandbox], task_id: str, steps: list[Step]
) -> list[str]:
    reference = sandbox_class(task_id)
    await reference.start()
    try:
        return [await reference.execute(EvalCall(step.call)) for step in steps]
    finally:
        await reference.stop()


@dataclass
class ModeResult:
    mode: str
    records: list[CallRecord]
    counters: SandboxCounters
    reference_counters: SandboxCounters
    analysis_calls: int
    analysis_seconds: float
    lookups: int | None
    outstanding_leases: int | None
    wall_seconds: float
    reasons: Counter


async def run_mode(workload: Workload, mode: Mode, config: RunConfig) -> ModeResult:
    counters = SandboxCounters(latency=config.cost.latency(config.latency_scale))
    reference_counters = SandboxCounters()
    timed = (
        TimedCanonicalizer(make_canonicalizer(mode.canonicalizer, workload))
        if mode.canonicalizer else None
    )
    records: list[CallRecord] = []
    reasons: Counter = Counter()
    run_id = uuid.uuid4().hex[:12]
    factory = SANDBOX_FACTORIES[workload.kind]
    started = time.perf_counter()

    with _backend(config) as store:
        for task in workload.tasks:
            sandbox_class = factory(task.initial_state, counters)
            reference_class = factory(task.initial_state, reference_counters)
            task_key = f"fuzzytv-eval/{run_id}/{mode.name}/{task.task_id}"
            for rollout_index, steps in enumerate(task.rollouts):
                sample = random.Random(
                    f"{config.seed}/{task.task_id}/{rollout_index}"
                ).random() < config.sample_rate
                reference = (
                    await _reference_values(reference_class, task_key, steps)
                    if sample else None
                )
                values = await _run_rollout(
                    mode, config, task, task_key, steps, sandbox_class, timed,
                    counters, rollout_index, records, reasons, workload.params,
                )
                for record, value, ref in zip(
                    records[-len(steps):], values, reference or [None] * len(steps),
                    strict=True,
                ):
                    record.validated = ref is not None
                    record.matched = None if ref is None else value == ref
                    if config.keep_values or (ref is not None and value != ref):
                        record.returned, record.reference = value, ref
        lookups = store.finds if store is not None else None
        leases = store.outstanding_leases() if store is not None else None

    return ModeResult(
        mode=mode.name, records=records, counters=counters,
        reference_counters=reference_counters,
        analysis_calls=timed.calls if timed else 0,
        analysis_seconds=timed.seconds if timed else 0.0,
        lookups=lookups, outstanding_leases=leases,
        wall_seconds=time.perf_counter() - started, reasons=reasons,
    )


async def _run_rollout(
    mode: Mode, config: RunConfig, task: Task, task_key: str, steps: list[Step],
    sandbox_class: type[Sandbox], timed: TimedCanonicalizer | None,
    counters: SandboxCounters, rollout_index: int, records: list[CallRecord],
    reasons: Counter, workload_params: dict[str, Any],
) -> list[str]:
    values: list[str] = []

    def record(step_index: int, step: Step, source: str, executions: int,
               seconds: float, reason: str | None) -> None:
        oracle = step.oracle
        records.append(CallRecord(
            mode=mode.name, task_id=task.task_id, rollout=rollout_index,
            step=step_index, intent=step.intent, source=source,
            executions=executions, seconds=seconds, validated=False, matched=None,
            oracle_eligible=oracle is not None and (
                oracle["canonical"] is not None and not oracle["writes"]
                and not oracle["reads_unknown"] and not oracle["writes_unknown"]
            ),
            reason=reason,
        ))
        if source == "execute" and reason:
            reasons[reason] += 1

    if mode.kind == "none":
        sandbox = sandbox_class(task_key)
        await sandbox.start()
        try:
            for index, step in enumerate(steps):
                start = time.perf_counter()
                values.append(await sandbox.execute(EvalCall(step.call)))
                record(index, step, "execute", 1, time.perf_counter() - start, None)
        finally:
            await sandbox.stop()
        return values

    options = _executor_options(config)
    current: dict[str, CallContext] = {}
    if mode.kind == "relevant_state":
        assert timed is not None
        executor: TvcacheToolExecutor = RelevantStateExecutor(
            sandbox_class, EvalCall, task_key,
            canonicalizer=timed,
            scope=CacheScope(
                _initial_state_id(task), f"{mode.canonicalizer}-eval-v1"
            ),
            context_provider=lambda: current["ctx"],
            call_local_results=True,
            **options,
        )
    else:
        executor = TvcacheToolExecutor(sandbox_class, EvalCall, task_key, **options)

    # Collected traces may declare every call stateless (SkyRL-SQL, as in the paper).
    workload_mutates = bool(workload_params.get("will_mutate_state", True))
    history: list[ToolCall] = []
    async with executor:
        for index, step in enumerate(steps):
            current["ctx"] = step.ctx
            mutating = workload_mutates and not (
                config.read_only_hints and _oracle_read_only(step)
            )
            if mode.kind == "canonical":
                assert timed is not None
                analysis = timed.analyze(step.call, step.ctx)
                history.append(CanonicalKeyedCall(step.call, analysis, mutating))
            else:
                history.append(EvalCall(step.call, mutating))
            before = executor.total_executions
            start = time.perf_counter()
            value = await executor.lookup_or_execute(list(history))
            seconds = time.perf_counter() - start
            executions = executor.total_executions - before
            if isinstance(executor, RelevantStateExecutor):
                decision = executor.last_decision
                source, reason = decision.source, decision.reason
            else:
                source = "exact" if executions == 0 else "execute"
                reason = None
            values.append(value)
            record(index, step, source, executions, seconds, reason)
    return values


# ------------------------------------------------------------------ metrics


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return (0.0, 1.0)
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(result: ModeResult, workload: Workload, config: RunConfig) -> dict[str, Any]:
    records = result.records
    calls = len(records)
    count = Counter(r.source for r in records)
    validated = [r for r in records if r.validated]
    fuzzy_validated = [r for r in validated if r.source == "fuzzy"]
    false_fuzzy = sum(not r.matched for r in fuzzy_validated)
    false_exact = sum(r.source == "exact" and not r.matched for r in validated)
    false_execute = sum(r.source == "execute" and not r.matched for r in validated)
    hits = count["exact"] + count["fuzzy"]
    low, high = wilson_interval(false_fuzzy, len(fuzzy_validated))
    eligible = sum(r.oracle_eligible for r in records)
    lookups = result.lookups
    return {
        "workload": workload.name,
        "kind": workload.kind,
        "mode": result.mode,
        "calls": calls,
        "exact_hits": count["exact"],
        "fuzzy_hits": count["fuzzy"],
        "misses": count["execute"],
        "hit_rate": hits / calls if calls else 0.0,
        "fuzzy_hit_rate": count["fuzzy"] / calls if calls else 0.0,
        "oracle_eligible_calls": eligible,
        "validated_calls": len(validated),
        "validated_fuzzy_hits": len(fuzzy_validated),
        "false_fuzzy_hits": false_fuzzy,
        "false_hit_rate": false_fuzzy / len(fuzzy_validated) if fuzzy_validated else 0.0,
        "false_hit_rate_ci95": [low, high],
        "false_exact_hits": false_exact,
        "mismatched_executions": false_execute,
        "physical_executions": result.counters.executes,
        "sandbox_starts": result.counters.starts,
        "sandbox_restores": result.counters.restores,
        "sandbox_forks": result.counters.forks,
        "leaked_sandboxes": len(result.counters.live),
        "outstanding_leases": result.outstanding_leases,
        "reference_executions": result.reference_counters.executes,
        "cache_lookups": lookups,
        "analysis_calls": result.analysis_calls,
        "analysis_seconds": result.analysis_seconds,
        "analysis_us_per_call": (
            1e6 * result.analysis_seconds / result.analysis_calls
            if result.analysis_calls else 0.0
        ),
        "policy_seconds": sum(r.seconds for r in records),
        "wall_seconds": result.wall_seconds,
        "modeled_tool_seconds": config.cost.seconds(result.counters, lookups or 0),
        "top_miss_reasons": result.reasons.most_common(8),
        "backend": config.backend,
        "snapshot": config.snapshot,
        "latency_scale": config.latency_scale,
        "sample_rate": config.sample_rate,
        "read_only_hints": config.read_only_hints,
        "seed": config.seed,
        "cost_model": asdict(config.cost),
    }


def record_rows(result: ModeResult, workload: Workload) -> Iterator[dict[str, Any]]:
    for record in result.records:
        yield {"workload": workload.name, **asdict(record)}
