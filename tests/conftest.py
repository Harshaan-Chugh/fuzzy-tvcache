"""Controlled workloads and explicit opt-in for server integration tests."""

import itertools
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tvclient.client import FindResult
from tvclient.protocol import FindResponse, InsertResult
from tvclient.sandbox import Sandbox
from tvclient.tool_call import ToolCall

from fuzzytv.cache.executor import RelevantStateExecutor
from fuzzytv.cache.index import RESERVED_PREFIX
from fuzzytv.interfaces import (
    CacheScope,
    CallAnalysis,
    CallContext,
    fs_dir,
    fs_file,
    sql_table,
    unknown,
)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-tvcache-integration",
        action="store_true",
        help="Run tests against a dedicated running TVCache server.",
    )


@pytest.fixture
def tvcache_address(request: pytest.FixtureRequest) -> tuple[str, int]:
    if not request.config.getoption("--run-tvcache-integration"):
        pytest.skip("requires --run-tvcache-integration and a TVCache server")
    return (
        os.environ.get("TVCACHE_HOST", "127.0.0.1"),
        int(os.environ.get("TVCACHE_PORT", "7878")),
    )


@pytest.fixture
def workload(tmp_path: Path) -> SimpleNamespace:
    identifiers = itertools.count()
    roots: dict[str, Path] = {}
    live: set[str] = set()

    class FixtureCall(ToolCall):
        def __init__(self, op: str, **arguments: Any) -> None:
            self.op = op
            self.arguments = arguments

        def to_dict(self) -> dict[str, Any]:
            return {"op": self.op, **self.arguments}

        def will_mutate_state(self) -> bool:
            return self.arguments.get(
                "mutating",
                self.op in {"write", "sql_write", "unknown", "fail", "raise"},
            )

    class FixtureSandbox(Sandbox):
        fail_next_write = False

        async def start(self) -> None:
            identifier = f"live-{next(identifiers)}"
            root = tmp_path / identifier
            if self.env_id is not None:
                shutil.copytree(roots[self.env_id], root)
            else:
                root.mkdir()
                (root / "config.json").write_text("initial", encoding="utf-8")
                (root / "notes.txt").write_text("initial notes", encoding="utf-8")
                with sqlite3.connect(root / "state.sqlite") as connection:
                    connection.executescript(
                        "CREATE TABLE items(value TEXT);"
                        "INSERT INTO items VALUES ('initial');"
                        "CREATE TABLE notes(value TEXT);"
                        "INSERT INTO notes VALUES ('initial notes');"
                    )
            roots[identifier] = root
            live.add(identifier)
            self.env_id = identifier

        async def fork(self) -> Sandbox:
            identifier = f"snapshot-{next(identifiers)}"
            root = tmp_path / identifier
            shutil.copytree(roots[self.env_id], root)
            roots[identifier] = root
            return FixtureSandbox(self.task_name, identifier)

        async def execute(self, tool_call: ToolCall) -> str:
            call = tool_call.to_dict()
            root = roots[self.env_id]
            op = call["op"]
            if op == "read":
                return (root / call["path"]).read_text(encoding="utf-8")
            if op == "write":
                if FixtureSandbox.fail_next_write:
                    FixtureSandbox.fail_next_write = False
                    (root / call["path"]).write_text("partial", encoding="utf-8")
                    return "Failed to execute: injected write failure"
                (root / call["path"]).write_text(call["text"], encoding="utf-8")
                return "OK"
            if op in {"sql", "sql_write"}:
                with sqlite3.connect(root / "state.sqlite") as connection:
                    cursor = connection.execute(call["sql"])
                    return (
                        json.dumps(cursor.fetchall())
                        if cursor.description is not None
                        else "OK"
                    )
            if op == "unknown":
                (root / "config.json").write_text("unknown script", encoding="utf-8")
                return "OK"
            if op in {"fail", "raise"}:
                (root / "config.json").write_text("partial", encoding="utf-8")
                if op == "raise":
                    raise RuntimeError("injected execution exception")
                return "Failed to execute: injected failure"
            raise ValueError(f"unsupported fixture operation: {op}")

        async def stop(self) -> None:
            if self.env_id is not None:
                root = roots.pop(self.env_id, None)
                live.discard(self.env_id)
                if root is not None:
                    shutil.rmtree(root)

    class FixtureCanonicalizer:
        def analyze(
            self, call: dict[str, Any], ctx: CallContext
        ) -> CallAnalysis:
            # Explicit fixture analyses, not a production shell/SQL parser.
            op = call["op"]
            path = call.get("path")
            if op in {"read", "write"} and path in {"config.json", "notes.txt"}:
                component = fs_file(f"/app/{path}")
                if op == "read":
                    return CallAnalysis(
                        f"cat /app/{path}", reads=frozenset({component})
                    )
                return CallAnalysis(
                    json.dumps(["write", path, call["text"]]),
                    writes=frozenset({component, fs_dir("/app")}),
                )
            if op in {"fail", "raise"}:
                return CallAnalysis(
                    op, writes=frozenset({fs_file("/app/config.json")})
                )
            sql = call.get("sql")
            if op == "sql" and sql in {
                "SELECT value FROM items",
                " select value from items ",
            }:
                return CallAnalysis(
                    "SELECT value FROM items",
                    reads=frozenset({sql_table("main", "items")}),
                )
            known_updates = {
                "UPDATE items SET value='hello'": "items",
                "UPDATE items SET value='world'": "items",
                "UPDATE notes SET value='changed'": "notes",
            }
            if op == "sql_write" and sql in known_updates:
                component = sql_table("main", known_updates[sql])
                return CallAnalysis(
                    sql, reads=frozenset({component}), writes=frozenset({component})
                )
            return unknown("unsupported fixture call")

    def make_executor(
        task_id: str, *, snapshot: bool = False, **options: Any
    ) -> RelevantStateExecutor:
        options.setdefault("canonicalizer", FixtureCanonicalizer())
        options.setdefault("scope", CacheScope("fixture-initial-v1", "fixture-policy-v1"))
        options.setdefault("context_provider", lambda: CallContext("/app", "main"))
        options.setdefault("call_local_results", True)
        executor = RelevantStateExecutor(
            FixtureSandbox, FixtureCall, task_id,
            adaptive=False, fixed_threshold_seconds=1_000_000, **options,
        )
        if snapshot:
            # Snapshot tests must not depend on measured wall-clock timing.
            executor.snapshot_policy.should_snapshot = lambda: True
        return executor

    return SimpleNamespace(
        call=FixtureCall,
        sandbox=FixtureSandbox,
        canonicalizer=FixtureCanonicalizer,
        make_executor=make_executor,
        roots=roots,
        live=live,
    )


@pytest.fixture
def memory_cache(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    entries: dict[
        tuple[str, tuple[str, ...]], tuple[str | None, str | None]
    ] = {}
    leases: dict[tuple[str, str], int] = {}
    clients: list[Any] = []
    state = SimpleNamespace(
        entries=entries, leases=leases, clients=clients,
        secondary_find_error=False, secondary_insert_error=False, unrefs=0,
    )

    class MemoryClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.closed = False
            clients.append(self)

        async def connect(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

        async def find(self, task_id: str, path: list[str]) -> FindResult:
            if task_id.startswith(RESERVED_PREFIX) and state.secondary_find_error:
                raise OSError("injected secondary lookup failure")
            value = entries.get((task_id, tuple(path)), (None, None))[0]
            sandbox_id = None
            sandbox_path = None
            for depth in range(1, len(path) + 1):
                prefix = tuple(path[:depth])
                candidate = entries.get((task_id, prefix), (None, None))[1]
                if candidate is not None:
                    sandbox_id = candidate
                    sandbox_path = list(prefix)
            if sandbox_id is not None:
                lease = (task_id, sandbox_id)
                leases[lease] = leases.get(lease, 0) + 1
            return FindResult(
                self, task_id, FindResponse(value, sandbox_path, sandbox_id)
            )

        async def insert(
            self, task_id: str, path: list[str], value: str | None,
            sandbox_id: str | None, runtime: int, ttl: int | None = None,
        ) -> InsertResult:
            if task_id.startswith(RESERVED_PREFIX) and state.secondary_insert_error:
                raise OSError("injected secondary insertion failure")
            key = (task_id, tuple(path))
            if len(path) > 1 and (task_id, tuple(path[:-1])) not in entries:
                return InsertResult(False, False)
            old_value, old_sandbox = entries.get(key, (None, None))
            entries[key] = (
                value if value is not None else old_value,
                old_sandbox if old_sandbox is not None else sandbox_id,
            )
            return InsertResult(
                value is not None, sandbox_id is not None and old_sandbox is None
            )

        async def unref_sandbox(self, task_id: str, sandbox_id: str) -> int:
            lease = (task_id, sandbox_id)
            leases[lease] -= 1
            state.unrefs += 1
            assert leases[lease] >= 0
            return leases[lease]

        async def get_fork_cost(self, task_id: str) -> int:
            return 0

        async def report_fork_cost(self, task_id: str, runtime: int) -> int:
            return runtime

    monkeypatch.setattr("tvclient.executor.TVCacheClient", MemoryClient)
    monkeypatch.setattr("fuzzytv.cache.executor.TVCacheClient", MemoryClient)
    return state
