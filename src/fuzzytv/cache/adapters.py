"""Delegate upstream operations while adding lookup and outcome monitoring."""

from asyncio import TimeoutError
from collections.abc import Callable
from typing import Any

from tvclient.client import FindResult, TVCacheClient
from tvclient.protocol import FindResponse, TVCacheServerError
from tvclient.sandbox import Sandbox
from tvclient.tool_call import ToolCall

from fuzzytv.cache.index import FuzzyIndex
from fuzzytv.interfaces import CacheSource, CacheStats

SECONDARY_ERRORS = (OSError, EOFError, TimeoutError, TVCacheServerError, ValueError)


class LookupAdapter:
    """Check exact first and preserve its lease until a result is selected."""

    def __init__(
        self, exact: TVCacheClient, index: FuzzyIndex, stats: CacheStats
    ) -> None:
        self.exact = exact
        self.index = index
        self.stats = stats
        self.key: str | None = None
        self.source: CacheSource = "execute"

    def __getattr__(self, name: str) -> Any:
        return getattr(self.exact, name)

    async def find(self, task_id: str, path: list[str]) -> FindResult:
        found = await self.exact.find(task_id, path)
        self.source = "exact" if found.value is not None else "execute"
        if found.value is not None or self.key is None:
            return found
        try:
            try:
                value = await self.index.get(self.key)
            except SECONDARY_ERRORS:
                self.stats.secondary_errors += 1
                return found
            if value is None:
                return found
            await found.release()
            self.source = "fuzzy"
            # Upstream receives a value hit without a sandbox and leaves the
            # requesting environment and its materialized history unchanged.
            return FindResult(
                self.exact, task_id,
                FindResponse(value=value, sandbox_path=None, sandbox_id=None),
            )
        except BaseException:
            # Upstream has not received this result yet, so we own its lease.
            await found.release()
            raise


def observed_sandbox_class(
    base: type[Sandbox], on_failure: Callable[[str], None]
) -> type[Sandbox]:
    """Monitor physical execution, including calls replayed by upstream."""

    class ObservedSandbox(Sandbox):
        def __init__(self, task_name: str, env_id: str | None = None) -> None:
            super().__init__(task_name, env_id)
            self.inner = base(task_name=task_name, env_id=env_id)

        def get_id(self) -> str:
            return self.inner.get_id()

        async def start(self) -> None:
            await self.inner.start()
            self.env_id = self.inner.get_id()

        async def fork(self) -> Sandbox:
            # Upstream stores only the cold handle's ID. Later restores
            # construct ObservedSandbox again through tool_call_env_class.
            return await self.inner.fork()

        async def execute(self, tool_call: ToolCall) -> str:
            try:
                value = await self.inner.execute(tool_call)
            except BaseException:
                on_failure("sandbox execution raised or was cancelled")
                raise
            if value.startswith("Failed to execute"):
                on_failure("sandbox reported an execution failure")
            return value

        async def stop(self) -> None:
            await self.inner.stop()

    return ObservedSandbox
