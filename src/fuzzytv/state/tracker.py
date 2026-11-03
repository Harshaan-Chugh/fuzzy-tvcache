"""Rollout-local relevant-state identities built from mutation lineage."""

import hashlib
import json

from fuzzytv.interfaces import CallAnalysis


def _fingerprint(namespace: str, **payload: object) -> str:
    encoded = json.dumps(
        {"namespace": namespace, **payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StateTracker:
    """Track logical rollout history independently of sandbox materialization.

    Compute a fuzzy key before a call, and observe its analysis afterwards.
    Once disabled, this instance remains disabled for its entire rollout.
    Public state is diagnostic; callers must not mutate it.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.versions: dict[str, str] = {}
        self.fuzzy_disabled = False
        self.disabled_reason: str | None = None

    def _version(self, component: str) -> str:
        if component in self.versions:
            return self.versions[component]
        return _fingerprint(
            "fuzzytv.initial.v1", task_id=self.task_id, component=component
        )

    def _dependencies(self, components: frozenset[str]) -> list[tuple[str, str]]:
        return [(component, self._version(component)) for component in sorted(components)]

    def _disable(self, reason: str, analysis: CallAnalysis) -> None:
        self.fuzzy_disabled = True
        self.disabled_reason = (
            f"{reason}: {analysis.reason}" if analysis.reason else reason
        )

    def observe(self, analysis: CallAnalysis) -> None:
        """Record one call after it enters the logical rollout history."""
        if self.fuzzy_disabled:
            return
        if analysis.writes_unknown:
            self._disable("unknown writes", analysis)
            return
        if analysis.writes and analysis.canonical is None:
            self._disable("write without canonical text", analysis)
            return
        if analysis.writes and analysis.reads_unknown:
            self._disable("write with unknown read dependencies", analysis)
            return
        if not analysis.writes:
            return

        # Snapshot every dependency before updating any written component.
        dependencies = self._dependencies(analysis.reads)
        updates = {
            component: _fingerprint(
                "fuzzytv.write.v1",
                previous=self._version(component),
                canonical=analysis.canonical,
                reads=dependencies,
            )
            for component in sorted(analysis.writes)
        }
        self.versions.update(updates)

    def fuzzy_key(self, analysis: CallAnalysis) -> str | None:
        """Return a key containing only this call's relevant state."""
        if self.fuzzy_disabled or not analysis.fuzzy_eligible:
            return None
        return _fingerprint(
            "fuzzytv.key.v1",
            task_id=self.task_id,
            canonical=analysis.canonical,
            reads=self._dependencies(analysis.reads),
        )
