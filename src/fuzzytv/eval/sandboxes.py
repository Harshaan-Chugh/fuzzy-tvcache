"""Deterministic sandboxes for controlled evaluation workloads.

- SQL: an in-memory SQLite database. Snapshots use the backup API.
- Terminal: an emulated shell over a virtual filesystem (no host I/O). It
  supports the commands the workloads generate, including ones the
  canonicalizers must treat as unknown (`grep`, `ls -l`, `python`).

Both return a call-local value string ("exit=<code>\\n<output>"), which is the
property `RelevantStateExecutor(call_local_results=True)` requires.
Sandboxes count their physical operations in a shared `SandboxCounters`, so
modes can be compared by work done, not only by wall-clock time.
"""

import asyncio
import copy
import itertools
import json
import posixpath
import shlex
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from tvclient.sandbox import Sandbox
from tvclient.tool_call import ToolCall

from fuzzytv.eval.calls import BASH_TOOL, SQL_TOOL


class EvalCall(ToolCall):
    """One workload call.

    `will_mutate_state()` is True (the upstream default) unless the runner
    passes a read-only hint, modelling a tool that declares its reads.
    """

    def __init__(self, call: dict[str, Any], mutating: bool = True) -> None:
        self.call = call
        self.mutating = mutating

    def to_dict(self) -> dict[str, Any]:
        return dict(self.call)

    def will_mutate_state(self) -> bool:
        return self.mutating


@dataclass
class SandboxCounters:
    starts: int = 0
    restores: int = 0
    forks: int = 0
    executes: int = 0
    stops: int = 0
    live: set[str] = field(default_factory=set)
    # Simulated seconds per operation ("start", "restore", "fork", "execute").
    # Lets the adaptive snapshot policy see realistic relative costs.
    latency: dict[str, float] = field(default_factory=dict)

    async def pause(self, operation: str) -> None:
        seconds = self.latency.get(operation, 0.0)
        if seconds > 0:
            await asyncio.sleep(seconds)


def _result(code: int, output: str) -> str:
    return f"exit={code}\n{output}"


# ------------------------------------------------------------------ SQLite


def sqlite_sandbox_class(
    initial_state: dict[str, Any], counters: SandboxCounters
) -> type[Sandbox]:
    """`initial_state = {"schema_sql": "<script>"}`."""
    ids = itertools.count()
    connections: dict[str, sqlite3.Connection] = {}

    def copy_of(source: sqlite3.Connection) -> sqlite3.Connection:
        target = sqlite3.connect(":memory:", isolation_level=None)
        source.backup(target)
        return target

    class SqliteSandbox(Sandbox):
        async def start(self) -> None:
            if self.env_id is None:
                connection = sqlite3.connect(":memory:", isolation_level=None)
                connection.executescript(initial_state["schema_sql"])
                counters.starts += 1
                await counters.pause("start")
            else:
                connection = copy_of(connections[self.env_id])
                counters.restores += 1
                await counters.pause("restore")
            self.env_id = f"sqlite-live-{next(ids)}"
            connections[self.env_id] = connection
            counters.live.add(self.env_id)

        async def fork(self) -> Sandbox:
            counters.forks += 1
            await counters.pause("fork")
            handle = f"sqlite-snap-{next(ids)}"
            connections[handle] = copy_of(connections[self.env_id])
            return SqliteSandbox(self.task_name, handle)

        async def execute(self, tool_call: ToolCall) -> str:
            counters.executes += 1
            await counters.pause("execute")
            call = tool_call.to_dict()
            if call.get("tool") != SQL_TOOL:
                return _result(2, f"unsupported tool {call.get('tool')!r}")
            try:
                cursor = connections[self.env_id].execute(call["query"])
                rows = cursor.fetchall()
            except (sqlite3.Error, sqlite3.Warning, ValueError) as error:
                return _result(1, f"{type(error).__name__}: {error}")
            if cursor.description is None:
                return _result(0, f"rows affected: {cursor.rowcount}")
            columns = [column[0] for column in cursor.description]
            return _result(0, json.dumps({"columns": columns, "rows": rows}))

        async def stop(self) -> None:
            counters.stops += 1
            connection = connections.pop(self.env_id, None)
            counters.live.discard(self.env_id)
            if connection is not None:
                connection.close()

    return SqliteSandbox


# ----------------------------------------------------------- emulated shell


@dataclass
class ShellState:
    files: dict[str, str]
    dirs: set[str]
    cwd: str

    @classmethod
    def from_initial(cls, initial: dict[str, Any]) -> "ShellState":
        files = dict(initial["files"])
        dirs = set(initial.get("dirs", ())) | {"/"}
        for path in files:
            parent = posixpath.dirname(path)
            while parent not in dirs:
                dirs.add(parent)
                parent = posixpath.dirname(parent)
        return cls(files, dirs, initial.get("cwd", "/"))


class _ShellError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def run_shell(state: ShellState, command: str) -> str:
    """Execute `cmd [&& cmd ...]` against `state`; return the value string."""
    outputs: list[str] = []
    code = 0
    for part in command.split("&&"):
        try:
            outputs.append(_run_one(state, part.strip()))
            code = 0
        except _ShellError as error:
            outputs.append(str(error))
            code = error.code
            break
    return _result(code, "".join(outputs))


def _run_one(state: ShellState, command: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise _ShellError(2, f"sh: {error}\n") from None
    if not argv:
        return ""

    def resolve(path: str) -> str:
        return posixpath.normpath(posixpath.join(state.cwd, path))

    def need_parent(path: str, name: str) -> None:
        if posixpath.dirname(path) not in state.dirs:
            raise _ShellError(1, f"{name}: {path}: No such file or directory\n")

    for redirect in (">>", ">"):
        if redirect in argv:
            index = argv.index(redirect)
            if argv[0] != "echo" or index != len(argv) - 2:
                raise _ShellError(2, "sh: unsupported redirection\n")
            target = resolve(argv[-1])
            need_parent(target, "sh")
            if target in state.dirs:
                raise _ShellError(1, f"sh: {argv[-1]}: Is a directory\n")
            text = " ".join(argv[1:index]) + "\n"
            previous = state.files.get(target, "") if redirect == ">>" else ""
            state.files[target] = previous + text
            return ""

    name, args = argv[0], argv[1:]
    if name == "echo":
        return " ".join(args) + "\n"
    if name == "cat":
        out = []
        for arg in args:
            path = resolve(arg)
            if path in state.dirs:
                raise _ShellError(1, f"cat: {arg}: Is a directory\n")
            if path not in state.files:
                raise _ShellError(1, f"cat: {arg}: No such file or directory\n")
            out.append(state.files[path])
        return "".join(out)
    if name == "ls":
        long = "-l" in args
        targets = [arg for arg in args if not arg.startswith("-")] or ["."]
        out = []
        for arg in targets:
            path = resolve(arg)
            if path in state.files:
                entries = [posixpath.basename(path)]
            elif path in state.dirs:
                prefix = path.rstrip("/") + "/"
                entries = sorted(
                    {
                        item[len(prefix):].split("/")[0]
                        for item in itertools.chain(state.files, state.dirs)
                        if item.startswith(prefix) and item != path
                    }
                )
            else:
                raise _ShellError(2, f"ls: {arg}: No such file or directory\n")
            for entry in entries:
                full = posixpath.join(path, entry) if path in state.dirs else path
                if long:
                    kind = "d" if full in state.dirs else "-"
                    size = len(state.files.get(full, ""))
                    out.append(f"{kind} {size:>6} {entry}\n")
                else:
                    out.append(entry + "\n")
        return "".join(out)
    if name == "cd":
        path = resolve(args[0] if args else "/")
        if path not in state.dirs:
            raise _ShellError(1, f"cd: {args[0]}: No such file or directory\n")
        state.cwd = path
        return ""
    if name == "mkdir":
        for arg in args:
            path = resolve(arg)
            need_parent(path, "mkdir")
            if path in state.dirs or path in state.files:
                raise _ShellError(1, f"mkdir: {arg}: File exists\n")
            state.dirs.add(path)
        return ""
    if name == "touch":
        for arg in args:
            path = resolve(arg)
            need_parent(path, "touch")
            state.files.setdefault(path, "")
        return ""
    if name == "rm":
        for arg in args:
            path = resolve(arg)
            if path not in state.files:
                raise _ShellError(1, f"rm: {arg}: No such file or directory\n")
            del state.files[path]
        return ""
    if name == "grep" and len(args) == 2:
        path = resolve(args[1])
        if path not in state.files:
            raise _ShellError(2, f"grep: {args[1]}: No such file or directory\n")
        lines = [line for line in state.files[path].splitlines() if args[0] in line]
        if not lines:
            raise _ShellError(1, "")
        return "".join(line + "\n" for line in lines)
    if name == "wc" and args[:1] == ["-l"] and len(args) == 2:
        path = resolve(args[1])
        if path not in state.files:
            raise _ShellError(1, f"wc: {args[1]}: No such file or directory\n")
        return f"{state.files[path].count(chr(10))} {args[1]}\n"
    if name == "python" and len(args) == 1:
        # Stand-in for an arbitrary program: reads and writes state that no
        # canonicalizer can see from the command text.
        script = resolve(args[0])
        if script not in state.files:
            raise _ShellError(2, f"python: can't open file '{args[0]}'\n")
        log = "/app/out.log"
        state.files[log] = state.files.get(log, "") + f"ran {args[0]}\n"
        runs = state.files[log].count("\n")
        return f"ok run={runs} config_bytes={len(state.files.get('/app/config.json', ''))}\n"
    raise _ShellError(127, f"sh: {name}: command not found\n")


def shell_sandbox_class(
    initial_state: dict[str, Any], counters: SandboxCounters
) -> type[Sandbox]:
    """`initial_state = {"files": {abs: text}, "dirs": [abs], "cwd": abs}`."""
    ids = itertools.count()
    states: dict[str, ShellState] = {}

    class ShellSandbox(Sandbox):
        async def start(self) -> None:
            if self.env_id is None:
                state = ShellState.from_initial(initial_state)
                counters.starts += 1
                await counters.pause("start")
            else:
                state = copy.deepcopy(states[self.env_id])
                counters.restores += 1
                await counters.pause("restore")
            self.env_id = f"shell-live-{next(ids)}"
            states[self.env_id] = state
            counters.live.add(self.env_id)

        async def fork(self) -> Sandbox:
            counters.forks += 1
            await counters.pause("fork")
            handle = f"shell-snap-{next(ids)}"
            states[handle] = copy.deepcopy(states[self.env_id])
            return ShellSandbox(self.task_name, handle)

        async def execute(self, tool_call: ToolCall) -> str:
            counters.executes += 1
            await counters.pause("execute")
            call = tool_call.to_dict()
            command = call.get("command")
            if call.get("tool") != BASH_TOOL or not isinstance(command, str):
                return _result(2, f"unsupported tool {call.get('tool')!r}")
            return run_shell(states[self.env_id], command)

        async def stop(self) -> None:
            counters.stops += 1
            states.pop(self.env_id, None)
            counters.live.discard(self.env_id)

    return ShellSandbox


SANDBOX_FACTORIES = {"sql": sqlite_sandbox_class, "fs": shell_sandbox_class}
