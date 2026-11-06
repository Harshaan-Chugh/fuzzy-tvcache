"""Canonicalizers used by the evaluation runner.

- `OracleCanonicalizer`: replays the generator's oracle analyses. A
  spec-perfect upper bound, available before `fuzzytv.sql`/`fuzzytv.fs` exist.
- `LooseCanonicalizer`: the least-strict level from the proposal. It knows only
  "read" vs "write" and ignores read dependencies. It is intentionally
  **unsafe** and exists to measure the false-hit side of the trade-off and to
  prove the validator catches false hits.
- `RealCanonicalizer`: dispatches to Kevin's and Etienne's implementations by
  the `tool` field from `fuzzytv.eval.calls`.
- `TimedCanonicalizer`: wraps any of these to measure analysis overhead.
"""

import importlib
import json
import time
from typing import Any

from fuzzytv.eval.calls import BASH_TOOL, SQL_TOOL
from fuzzytv.eval.workloads import Workload, analysis_from_json
from fuzzytv.interfaces import CallAnalysis, CallContext, Canonicalizer, unknown

LOOSE_WRITE_KEY = "loose:any"


def _call_key(call: dict[str, Any], ctx: CallContext) -> str:
    return json.dumps([call, ctx.cwd, ctx.database], sort_keys=True)


class OracleCanonicalizer:
    name = "oracle"

    def __init__(self, workload: Workload) -> None:
        self.table: dict[str, CallAnalysis] = {}
        for task in workload.tasks:
            for rollout in task.rollouts:
                for step in rollout:
                    key = _call_key(step.call, step.ctx)
                    analysis = analysis_from_json(step.oracle)
                    if self.table.setdefault(key, analysis) != analysis:
                        raise ValueError(f"inconsistent oracle for {key}")

    def analyze(self, call: dict[str, Any], ctx: CallContext) -> CallAnalysis:
        return self.table.get(_call_key(call, ctx)) or unknown("not in oracle table")


class LooseCanonicalizer:
    """Read/write classification only. Unsafe by design."""

    name = "loose"

    def __init__(self, inner: Canonicalizer) -> None:
        self.inner = inner

    def analyze(self, call: dict[str, Any], ctx: CallContext) -> CallAnalysis:
        analysis = self.inner.analyze(call, ctx)
        canonical = analysis.canonical or json.dumps(
            [call, ctx.cwd, ctx.database], sort_keys=True
        )
        if analysis.writes or analysis.writes_unknown:
            return CallAnalysis(canonical, writes=frozenset({LOOSE_WRITE_KEY}))
        return CallAnalysis(canonical, reason="loose: read dependencies ignored")


class RealCanonicalizer:
    """Route by tool to `fuzzytv.sql.SqlCanonicalizer` / `fuzzytv.fs.FsCanonicalizer`."""

    name = "real"
    ROUTES = {
        SQL_TOOL: ("fuzzytv.sql", "SqlCanonicalizer"),
        BASH_TOOL: ("fuzzytv.fs", "FsCanonicalizer"),
    }

    def __init__(self) -> None:
        self.routes: dict[str, Canonicalizer] = {}
        missing = []
        for tool, (module, cls) in self.ROUTES.items():
            try:
                self.routes[tool] = getattr(importlib.import_module(module), cls)()
            except (ImportError, AttributeError):
                missing.append(f"{module}.{cls}")
        if not self.routes:
            raise RuntimeError(f"no real canonicalizer implemented yet: {missing}")
        self.missing = missing

    def analyze(self, call: dict[str, Any], ctx: CallContext) -> CallAnalysis:
        route = self.routes.get(call.get("tool")) if isinstance(call, dict) else None
        if route is None:
            return unknown(f"no canonicalizer for tool {call.get('tool')!r}")
        return route.analyze(call, ctx)


class TimedCanonicalizer:
    def __init__(self, inner: Canonicalizer) -> None:
        self.inner = inner
        self.calls = 0
        self.seconds = 0.0
        self.last: CallAnalysis | None = None

    def analyze(self, call: dict[str, Any], ctx: CallContext) -> CallAnalysis:
        start = time.perf_counter()
        try:
            self.last = self.inner.analyze(call, ctx)
            return self.last
        finally:
            self.seconds += time.perf_counter() - start
            self.calls += 1


def make_canonicalizer(name: str, workload: Workload) -> Canonicalizer:
    if name == "oracle":
        return OracleCanonicalizer(workload)
    if name == "loose":
        return LooseCanonicalizer(OracleCanonicalizer(workload))
    if name == "real":
        return RealCanonicalizer()
    if name == "real-loose":
        return LooseCanonicalizer(RealCanonicalizer())
    raise ValueError(f"unknown canonicalizer {name!r}")
