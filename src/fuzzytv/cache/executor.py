"""Relevant-state reuse with execution and replay delegated to upstream."""

from collections.abc import Callable
from typing import Any

from tvclient.client import TVCacheClient
from tvclient.executor import TvcacheToolExecutor
from tvclient.sandbox import Sandbox
from tvclient.tool_call import ToolCall

from fuzzytv.cache.adapters import (
    SECONDARY_ERRORS,
    LookupAdapter,
    observed_sandbox_class,
)
from fuzzytv.cache.index import FuzzyIndex
from fuzzytv.interfaces import (
    CacheDecision,
    CacheScope,
    CacheStats,
    CallAnalysis,
    CallContext,
    Canonicalizer,
    unknown,
)
from fuzzytv.state.tracker import StateTracker


class RelevantStateExecutor(TvcacheToolExecutor):
    """One sequential rollout with exact-first relevant-state reuse.

    For exact mode, construct upstream TvcacheToolExecutor directly.
    context_provider must describe logical state, which may be ahead of the
    live sandbox. The workload must explicitly confirm call-local outputs.
    """

    def __init__(
        self,
        tool_call_env_class: type[Sandbox],
        tool_call_class: type[ToolCall],
        task_id: str,
        *,
        canonicalizer: Canonicalizer,
        scope: CacheScope,
        context_provider: Callable[[], CallContext],
        call_local_results: bool = False,
        host: str = "127.0.0.1",
        port: int = 7878,
        **executor_options: Any,
    ) -> None:
        self.index = FuzzyIndex(TVCacheClient(host, port), task_id, scope)
        self.tracker = StateTracker(self.index.namespace)
        self.stats = CacheStats()
        self.canonicalizer = canonicalizer
        self.context_provider = context_provider
        self.call_local_results = call_local_results
        self.last_decision: CacheDecision | None = None
        self._history: list[tuple[str, bool]] = []
        self._in_lookup = False
        super().__init__(
            observed_sandbox_class(tool_call_env_class, self._disable),
            tool_call_class, task_id, host=host, port=port, **executor_options,
        )
        self._lookup = LookupAdapter(self.client, self.index, self.stats)
        self.client = self._lookup

    def _disable(self, reason: str) -> None:
        self.tracker.observe(unknown(reason))

    async def lookup_or_execute(self, tool_commands: list[ToolCall]) -> str:
        if not tool_commands:
            raise ValueError("at least one tool call is required")
        if self._in_lookup:
            raise RuntimeError("one executor cannot process concurrent calls")
        self._in_lookup = True
        self.last_decision = None
        try:
            signatures = [
                (call.serialize(), call.will_mutate_state()) for call in tool_commands
            ]
            if (
                len(signatures) != len(self._history) + 1
                or signatures[:-1] != self._history
            ):
                self._disable("tool history is not a sequential append")
            current = tool_commands[-1]
            try:
                analysis = self.canonicalizer.analyze(
                    current.to_dict(), self.context_provider()
                )
                if not isinstance(analysis, CallAnalysis):
                    analysis = unknown("canonicalizer returned an invalid analysis")
            except Exception:
                analysis = unknown("canonicalizer or context provider failed")
            if (
                (analysis.writes or analysis.writes_unknown)
                and not current.will_mutate_state()
            ):
                self._disable("upstream omitted a potentially mutating call from replay")
            key = self.tracker.fuzzy_key(analysis) if self.call_local_results else None
            self._lookup.key = key
            self._lookup.source = "execute"
            try:
                value = await super().lookup_or_execute(tool_commands)
            except BaseException:
                self._disable("lookup or execution did not complete")
                raise

            if value.startswith("Failed to execute"):
                self._disable("call returned an execution failure")
            # Logical observation happens once; replay only reports failures.
            self.tracker.observe(analysis)
            self._history = signatures
            source = self._lookup.source
            if source == "exact":
                self.stats.exact_hits += 1
            elif source == "fuzzy":
                self.stats.fuzzy_hits += 1
            else:
                self.stats.misses += 1
            reason = self.tracker.disabled_reason or analysis.reason
            if not self.call_local_results:
                reason = reason or "call-local result semantics not confirmed"
            self.last_decision = CacheDecision(
                source=source, fuzzy_key=key, reason=reason
            )
            if key is not None and not self.tracker.fuzzy_disabled and source != "fuzzy":
                try:
                    await self.index.put(key, value, self.ttl)
                except SECONDARY_ERRORS:
                    self.stats.secondary_errors += 1
            return value
        finally:
            self._lookup.key = None
            self._in_lookup = False

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.index.close()
