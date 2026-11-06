"""Tool-call dict schema used by the evaluation workloads (owner: Rohan).

The production `ToolCall.to_dict()` schema is still unconfirmed (see
docs/interfaces.md). Until it is, every evaluation trace and the canonicalizer
acceptance tests build calls only through these helpers, so the schema can be
changed in one place.
"""

from typing import Any

SQL_TOOL = "sql"
BASH_TOOL = "bash"


def sql_call(query: str) -> dict[str, Any]:
    return {"tool": SQL_TOOL, "query": query}


def bash_call(command: str) -> dict[str, Any]:
    return {"tool": BASH_TOOL, "command": command}
