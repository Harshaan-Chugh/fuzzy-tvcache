"""Independent reference execution for reproducible controlled rollouts."""

from tvclient.sandbox import Sandbox
from tvclient.tool_call import ToolCall

from fuzzytv.interfaces import CacheDecision, ValidationRecord


class ReferenceValidator:
    """Use a separate, started sandbox with equivalent initial state.

    Supply every original logical call, including exact and fuzzy hits.
    The caller owns starting and stopping this reference sandbox.
    """

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self.records: list[ValidationRecord] = []
        self._history: list[str] = []

    async def check(
        self,
        tool_commands: list[ToolCall],
        returned_value: str,
        decision: CacheDecision,
    ) -> ValidationRecord:
        signatures = [call.serialize() for call in tool_commands]
        if (
            len(signatures) != len(self._history) + 1
            or signatures[:-1] != self._history
        ):
            raise ValueError("validation requires every logical call in order")
        reference_value = await self.sandbox.execute(tool_commands[-1])
        self._history = signatures
        record = ValidationRecord(decision, returned_value, reference_value)
        self.records.append(record)
        if not record.matched:
            raise AssertionError(
                f"{decision.source} result mismatch: "
                f"returned={returned_value!r}, reference={reference_value!r}"
            )
        return record

    @property
    def validated_fuzzy_hits(self) -> int:
        return sum(record.decision.source == "fuzzy" for record in self.records)

    @property
    def false_fuzzy_hits(self) -> int:
        return sum(
            record.decision.source == "fuzzy" and not record.matched
            for record in self.records
        )
