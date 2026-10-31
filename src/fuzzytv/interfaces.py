"""Shared contract between canonicalizers (sql/, fs/) and the matcher (state/).

A canonicalizer looks at ONE tool call and says what it means: its canonical
form, which pieces of state it reads, and which it writes. It knows nothing
about rollout history. The state tracker (state/) combines these analyses with
history to decide whether a cached result can be reused.

State-key names (build them with the helpers below, never by hand):
  sql:table:<db>.<table>   rows AND schema of a table (DDL counts as a write)
  fs:file:<abs path>       contents and existence of a file
  fs:dir:<abs path>        set of entry names in a directory
  fs:cwd                   the shell's current working directory

Rules for implementers:
  - analyze() is pure: no I/O, no sandbox access, never raises on bad input.
  - When in doubt, say so: set reads_unknown / writes_unknown (or return
    unknown(reason)). Guessing produces wrong cached results; "unknown" only
    costs a cache hit.
  - The canonical form is used only for cache keys. It is never executed.

See docs/interfaces.md for worked examples.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CallContext:
    """What a canonicalizer may know besides the call itself."""

    cwd: str | None = None  # None = unknown; relative paths are then unanalyzable
    database: str | None = None  # SQL default database


@dataclass(frozen=True)
class CallAnalysis:
    """What one tool call means, as reported by a canonicalizer."""

    canonical: str | None  # equivalent calls -> identical string; None if not canonicalizable
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    reads_unknown: bool = False  # may read state not listed in `reads` (incl. time, network)
    writes_unknown: bool = False  # may write state not listed in `writes`
    reason: str | None = None  # why the call is not fuzzy-eligible, for logs/evaluation

    @property
    def fuzzy_eligible(self) -> bool:
        """True if the call may be served by a fuzzy cache hit.

        Only fully understood, read-only calls qualify: a write must actually
        run in the rollout's sandbox, and unknown reads leave nothing to compare.
        """
        return (
            self.canonical is not None
            and not self.reads_unknown
            and not self.writes_unknown
            and not self.writes
        )


class Canonicalizer(Protocol):
    """Implemented by sql/ (Kevin) and fs/ (Etienne)."""

    def analyze(self, call: dict[str, Any], ctx: CallContext) -> CallAnalysis: ...


def unknown(reason: str) -> CallAnalysis:
    """Fail-closed result: the call may read and write anything."""
    return CallAnalysis(canonical=None, reads_unknown=True, writes_unknown=True, reason=reason)


CWD = "fs:cwd"


def sql_table(database: str, table: str) -> str:
    return f"sql:table:{database}.{table}"


def fs_file(path: str) -> str:
    _require_absolute(path)
    return f"fs:file:{path}"


def fs_dir(path: str) -> str:
    _require_absolute(path)
    return f"fs:dir:{path}"


def _require_absolute(path: str) -> None:
    # Relative paths would make the same name mean different files under
    # different working directories; resolve against CallContext.cwd first.
    if not path.startswith("/"):
        raise ValueError(f"state-key paths must be absolute, got {path!r}")
