"""Server-free tests using the real upstream executor and controlled sandboxes."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from tvclient.executor import TvcacheToolExecutor

from fuzzytv.cache.index import FuzzyIndex, RESERVED_PREFIX
from fuzzytv.cache.validation import ReferenceValidator
from fuzzytv.interfaces import CacheScope, CallAnalysis, CallContext


async def test_four_rollouts_with_independent_validation(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    outcomes = {}
    keys = {}
    validated_fuzzy_hits = 0
    call = workload.call
    for label in ("A", "B", "C", "D"):
        trace = [
            call("write", path="config.json", text="world" if label == "C" else "hello")
        ]
        if label == "B":
            trace.append(call("write", path="notes.txt", text="unrelated"))
        if label == "D":
            trace.append(call("unknown"))
        trace.append(call("read", path="config.json", style=label))
        if label == "D":
            trace.append(call("read", path="config.json", style="D-again"))
        reference = workload.sandbox("shared-task")
        await reference.start()
        validator = ReferenceValidator(reference)
        try:
            async with workload.make_executor("shared-task", snapshot=True) as executor:
                for index in range(len(trace)):
                    history = trace[: index + 1]
                    before_sandbox = executor.rollout_sandbox
                    before_history = executor.sandbox_history.copy()
                    value = await executor.lookup_or_execute(history)
                    if executor.last_decision.source == "fuzzy":
                        assert before_sandbox is not None
                        assert executor.rollout_sandbox is before_sandbox
                        assert executor.sandbox_history == before_history
                    await validator.check(history, value, executor.last_decision)
                    if history[-1].op == "read":
                        outcomes.setdefault(label, []).append(
                            (executor.last_decision.source, value)
                        )
                        keys[label] = executor.last_decision.fuzzy_key
                if label == "D":
                    assert executor.tracker.fuzzy_disabled
            validated_fuzzy_hits += validator.validated_fuzzy_hits
            assert validator.false_fuzzy_hits == 0
        finally:
            await reference.stop()
    assert outcomes["A"] == [("execute", "hello")]
    assert outcomes["B"] == [("fuzzy", "hello")]
    assert outcomes["C"] == [("execute", "world")]
    assert outcomes["D"] == [
        ("execute", "unknown script"), ("execute", "unknown script")
    ]
    assert keys["A"] == keys["B"]
    assert keys["A"] != keys["C"]
    assert keys["D"] is None
    assert validated_fuzzy_hits == 1
    assert all(count == 0 for count in memory_cache.leases.values())
    assert not workload.live


async def test_exact_hit_precedes_secondary_lookup(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    trace = [workload.call("read", path="config.json")]
    async with workload.make_executor("task") as first:
        assert await first.lookup_or_execute(trace) == "initial"
    async with workload.make_executor("task") as second:
        async def forbidden_lookup(key: str) -> str | None:
            raise AssertionError("secondary lookup must not run on an exact hit")
        second.index.get = forbidden_lookup
        assert await second.lookup_or_execute(trace) == "initial"
        assert second.last_decision.source == "exact"
        assert second.total_executions == 0
        assert second.total_calls == 1


async def test_exact_hit_populates_secondary_index(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    call = workload.call
    read = call("read", path="config.json")
    async with TvcacheToolExecutor(
        workload.sandbox, call, "task",
        adaptive=False, fixed_threshold_seconds=1_000_000,
    ) as baseline:
        assert await baseline.lookup_or_execute([read]) == "initial"
    async with workload.make_executor("task") as first:
        assert await first.lookup_or_execute([read]) == "initial"
        assert first.last_decision.source == "exact"
        key = first.last_decision.fuzzy_key
        assert await first.index.get(key) == "initial"
    async with workload.make_executor("task") as second:
        write = call("write", path="notes.txt", text="unrelated")
        await second.lookup_or_execute([write])
        assert await second.lookup_or_execute([write, read]) == "initial"
        assert second.last_decision.source == "fuzzy"


async def test_fuzzy_hit_leaves_sandbox_unchanged_then_replay_works(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    call = workload.call
    write = call("write", path="config.json", text="hello")
    seed_read = call("read", path="config.json", mutating=True, style="seed")
    async with workload.make_executor("task") as seed:
        await seed.lookup_or_execute([write])
        await seed.lookup_or_execute([write, seed_read])
    read = call("read", path="config.json", mutating=True, style="request")
    notes = call("write", path="notes.txt", text="changed")
    reference = workload.sandbox("task")
    await reference.start()
    validator = ReferenceValidator(reference)
    try:
        async with workload.make_executor("task") as executor:
            for trace in ([write], [write, read]):
                value = await executor.lookup_or_execute(trace)
                await validator.check(trace, value, executor.last_decision)
            assert executor.last_decision.source == "fuzzy"
            assert executor.rollout_sandbox is None
            assert executor.sandbox_history == []
            assert executor.total_executions == 0
            config_version = executor.tracker.versions["fs:file:/app/config.json"]
            trace = [write, read, notes]
            value = await executor.lookup_or_execute(trace)
            await validator.check(trace, value, executor.last_decision)
            assert executor.total_executions == 3
            assert executor.total_calls == 3
            assert executor.sandbox_history == [item.serialize() for item in trace]
            assert executor.tracker.versions["fs:file:/app/config.json"] == config_version
            root = workload.roots[executor.rollout_sandbox.get_id()]
            assert (root / "config.json").read_text() == "hello"
            assert (root / "notes.txt").read_text() == "changed"
    finally:
        await reference.stop()


async def test_unconfirmed_output_semantics_disable_secondary_use(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    async with workload.make_executor("task", call_local_results=False) as executor:
        value = await executor.lookup_or_execute(
            [workload.call("read", path="config.json")]
        )
        assert value == "initial"
        assert executor.last_decision.fuzzy_key is None
        assert "not confirmed" in executor.last_decision.reason
        assert not any(
            task.startswith(RESERVED_PREFIX) for task, path in memory_cache.entries
        )


@pytest.mark.parametrize("failure", ["analysis", "context", "invalid-result"])
async def test_analysis_or_context_failure_falls_back(
    workload: SimpleNamespace, memory_cache: SimpleNamespace, failure: str,
) -> None:
    class BrokenCanonicalizer:
        def analyze(self, call: dict, ctx: CallContext) -> None:
            if failure == "invalid-result":
                return None
            raise ValueError("injected analysis failure")

    def broken_context() -> CallContext:
        raise ValueError("injected context failure")

    options = (
        {"canonicalizer": BrokenCanonicalizer()}
        if failure != "context" else {"context_provider": broken_context}
    )
    async with workload.make_executor("task", **options) as executor:
        assert await executor.lookup_or_execute(
            [workload.call("read", path="config.json")]
        ) == "initial"
        assert executor.tracker.fuzzy_disabled
        assert executor.last_decision.fuzzy_key is None


async def test_unsupported_read_only_call_preserves_later_fuzzy_reuse(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    class ReadOnlyUnsupportedCanonicalizer(workload.canonicalizer):
        def analyze(self, call: dict, ctx: CallContext) -> CallAnalysis:
            if call.get("style") == "unsupported":
                return CallAnalysis(None, reads_unknown=True, reason="unsupported read")
            return super().analyze(call, ctx)

    call = workload.call
    async with workload.make_executor("task") as seed:
        await seed.lookup_or_execute([call("read", path="config.json", style="seed")])
    async with workload.make_executor(
        "task", canonicalizer=ReadOnlyUnsupportedCanonicalizer()
    ) as executor:
        unsupported = call("read", path="notes.txt", style="unsupported")
        assert await executor.lookup_or_execute([unsupported]) == "initial notes"
        assert executor.last_decision.fuzzy_key is None
        assert not executor.tracker.fuzzy_disabled
        supported = call("read", path="config.json", style="request")
        assert await executor.lookup_or_execute([unsupported, supported]) == "initial"
        assert executor.last_decision.source == "fuzzy"
        assert not executor.tracker.fuzzy_disabled


async def test_nonsequential_history_permanently_disables(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    read = workload.call("read", path="config.json")
    async with workload.make_executor("task") as executor:
        await executor.lookup_or_execute([read])
        await executor.lookup_or_execute([read])
        assert executor.tracker.fuzzy_disabled
        assert "sequential append" in executor.tracker.disabled_reason


async def test_mutation_missing_from_upstream_replay_disables(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    write = workload.call("write", path="config.json", text="hello", mutating=False)
    async with workload.make_executor("task") as executor:
        assert await executor.lookup_or_execute([write]) == "OK"
        assert executor.tracker.fuzzy_disabled
        assert "replay" in executor.tracker.disabled_reason


@pytest.mark.parametrize("operation", ["fail", "raise"])
async def test_execution_failure_permanently_disables(
    workload: SimpleNamespace, memory_cache: SimpleNamespace, operation: str,
) -> None:
    async with workload.make_executor("task") as executor:
        trace = [workload.call(operation)]
        if operation == "raise":
            with pytest.raises(RuntimeError, match="injected"):
                await executor.lookup_or_execute(trace)
        else:
            assert (await executor.lookup_or_execute(trace)).startswith(
                "Failed to execute"
            )
        assert executor.tracker.fuzzy_disabled
        assert executor.stats.fuzzy_hits == 0


async def test_replay_failure_prevents_secondary_population(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    call = workload.call
    write = call("write", path="config.json", text="hello")
    async with workload.make_executor("task") as seed:
        await seed.lookup_or_execute([write])
    async with workload.make_executor("task") as executor:
        await executor.lookup_or_execute([write])
        assert executor.rollout_sandbox is None
        workload.sandbox.fail_next_write = True
        read = call("read", path="notes.txt")
        assert await executor.lookup_or_execute([write, read]) == "initial notes"
        assert executor.tracker.fuzzy_disabled
        key = executor.last_decision.fuzzy_key
        assert (executor.index.namespace, (key,)) not in memory_cache.entries


@pytest.mark.parametrize("failure", ["lookup", "insert"])
async def test_secondary_cache_failure_preserves_execution(
    workload: SimpleNamespace, memory_cache: SimpleNamespace, failure: str,
) -> None:
    memory_cache.secondary_find_error = failure == "lookup"
    memory_cache.secondary_insert_error = failure == "insert"
    async with workload.make_executor("task") as executor:
        value = await executor.lookup_or_execute(
            [workload.call("read", path="config.json")]
        )
        assert value == "initial"
        assert executor.last_decision.source == "execute"
        assert executor.stats.secondary_errors == 1
        assert not executor.tracker.fuzzy_disabled
    assert all(client.closed for client in memory_cache.clients)


async def test_cancelled_secondary_lookup_releases_exact_lease(
    workload: SimpleNamespace, memory_cache: SimpleNamespace,
) -> None:
    call = workload.call
    write = call("write", path="config.json", text="hello")
    async with workload.make_executor("task", snapshot=True) as seed:
        await seed.lookup_or_execute([write])
    async with workload.make_executor("task") as executor:
        await executor.lookup_or_execute([write])
        async def cancelled_lookup(key: str) -> str | None:
            raise asyncio.CancelledError()
        executor.index.get = cancelled_lookup
        with pytest.raises(asyncio.CancelledError):
            await executor.lookup_or_execute(
                [write, call("read", path="config.json")]
            )
        assert executor.tracker.fuzzy_disabled
    assert all(count == 0 for count in memory_cache.leases.values())


@pytest.mark.parametrize("malformed", ["json", "wrong-key", "sandbox"])
async def test_invalid_secondary_entry_falls_back_and_releases_leases(
    workload: SimpleNamespace, memory_cache: SimpleNamespace, malformed: str,
) -> None:
    read = workload.call("read", path="config.json")
    async with workload.make_executor("task") as executor:
        analysis = workload.canonicalizer().analyze(
            read.to_dict(), CallContext("/app", "main")
        )
        key = executor.tracker.fuzzy_key(analysis)
        value = (
            "not json" if malformed == "json"
            else json.dumps({"version": 1, "key": "wrong", "value": "bad"})
        )
        sandbox_id = "unexpected-snapshot" if malformed == "sandbox" else None
        memory_cache.entries[(executor.index.namespace, (key,))] = (value, sandbox_id)
        assert await executor.lookup_or_execute([read]) == "initial"
        assert executor.last_decision.source == "execute"
        assert executor.stats.secondary_errors == 1
    assert all(count == 0 for count in memory_cache.leases.values())


def test_secondary_namespaces_include_task_initial_state_and_policy() -> None:
    def namespace(task: str, initial: str, policy: str) -> str:
        return FuzzyIndex(object(), task, CacheScope(initial, policy)).namespace
    original = namespace("task", "initial", "policy")
    assert original != namespace("other-task", "initial", "policy")
    assert original != namespace("task", "other-initial", "policy")
    assert original != namespace("task", "initial", "other-policy")
    with pytest.raises(ValueError, match="reserved"):
        namespace(RESERVED_PREFIX + "task", "initial", "policy")
    with pytest.raises(ValueError):
        CacheScope("", "policy")
