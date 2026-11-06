"""In-process stand-in for the TVCache server, for server-free evaluation.

Mirrors the semantics of the pinned Rust server (`src/tcg/tool_call_graph.rs`)
that the executors depend on:

- `find`: value only on an exact full-path match; the deepest node on the path
  holding a sandbox is returned with a lease (reference count +1).
- `insert`: every parent must exist, else nothing is stored. A present value
  overwrites, an absent one keeps the old value, and the first sandbox wins.
- Fork-cost EMA with alpha = 0.3, seeded by the first sample.

No TTL, eviction or memory accounting. Hit and miss counts therefore match
a real server run with `--eviction-policy=none`. Latencies do not, so time
numbers from this backend measure our Python overhead only.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest import mock

from tvclient.client import FindResult
from tvclient.protocol import FindResponse, InsertResult


@dataclass
class _Node:
    value: str | None = None
    sandbox_id: str | None = None
    children: dict[str, "_Node"] = field(default_factory=dict)


@dataclass
class MemoryStore:
    """All tool-call graphs of one in-process "server"."""

    graphs: dict[str, _Node] = field(default_factory=dict)
    leases: dict[tuple[str, str], int] = field(default_factory=dict)
    fork_cost_ms: dict[str, int] = field(default_factory=dict)
    finds: int = 0
    inserts: int = 0

    def node_count(self, task_id: str | None = None) -> int:
        def count(node: _Node) -> int:
            return sum(1 + count(child) for child in node.children.values())

        roots = (
            [self.graphs[task_id]] if task_id in self.graphs
            else [] if task_id is not None else list(self.graphs.values())
        )
        return sum(count(root) for root in roots)

    def outstanding_leases(self) -> int:
        return sum(self.leases.values())


class MemoryClient:
    """Drop-in for `tvclient.client.TVCacheClient` backed by a MemoryStore."""

    store: MemoryStore  # bound per run by `memory_backend`

    def __init__(self, host: str = "", port: int = 0, **_: Any) -> None:
        self.host = host
        self.port = port

    async def __aenter__(self) -> "MemoryClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def find(self, task_id: str, path: list[str]) -> FindResult:
        return FindResult(self, task_id, await self.find_unmanaged(task_id, path))

    async def find_unmanaged(self, task_id: str, path: list[str]) -> FindResponse:
        self.store.finds += 1
        node = self.store.graphs.get(task_id)
        value = sandbox_id = sandbox_path = None
        walked: list[str] = []
        for depth, element in enumerate(path, start=1):
            if node is None or element not in node.children:
                break
            node = node.children[element]
            walked.append(element)
            if node.sandbox_id is not None:
                sandbox_id, sandbox_path = node.sandbox_id, list(walked)
            if depth == len(path):
                value = node.value
        if sandbox_id is not None:
            lease = (task_id, sandbox_id)
            self.store.leases[lease] = self.store.leases.get(lease, 0) + 1
        return FindResponse(value, sandbox_path, sandbox_id)

    async def insert(
        self,
        task_id: str,
        path: list[str],
        value: str | None,
        sandbox_id: str | None,
        runtime: int,
        ttl: int | None = None,
    ) -> InsertResult:
        self.store.inserts += 1
        if not path:
            return InsertResult(False, False)
        node = self.store.graphs.setdefault(task_id, _Node())
        for element in path[:-1]:
            if element not in node.children:
                return InsertResult(False, False)
            node = node.children[element]
        leaf = node.children.get(path[-1])
        if leaf is None:
            leaf = node.children[path[-1]] = _Node(value=value)
        elif value is not None:
            leaf.value = value
        consumed = sandbox_id is not None and leaf.sandbox_id is None
        if consumed:
            leaf.sandbox_id = sandbox_id
        return InsertResult(True, consumed)

    async def unref_sandbox(self, task_id: str, sandbox_id: str) -> int:
        lease = (task_id, sandbox_id)
        remaining = self.store.leases.get(lease, 0) - 1
        if remaining < 0:
            raise AssertionError(f"unbalanced unref of {sandbox_id}")
        if remaining:
            self.store.leases[lease] = remaining
        else:
            self.store.leases.pop(lease, None)
        return remaining

    async def get_fork_cost(self, task_id: str) -> int:
        return self.store.fork_cost_ms.get(task_id, 0)

    async def report_fork_cost(self, task_id: str, fork_cost_ms: int) -> int:
        old = self.store.fork_cost_ms.get(task_id, 0)
        new = fork_cost_ms if old == 0 else (7 * old + 3 * fork_cost_ms) // 10
        self.store.fork_cost_ms[task_id] = new
        return new


@contextmanager
def memory_backend(store: MemoryStore | None = None) -> Iterator[MemoryStore]:
    """Route every executor constructed inside the block to one MemoryStore."""
    store = store or MemoryStore()
    client = type("BoundMemoryClient", (MemoryClient,), {"store": store})
    with mock.patch("tvclient.executor.TVCacheClient", client), mock.patch(
        "fuzzytv.cache.executor.TVCacheClient", client
    ):
        yield store
