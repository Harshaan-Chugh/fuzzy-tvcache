"""Value-only secondary index using the existing TVCache protocol."""

import hashlib
import json

from tvclient.client import TVCacheClient

from fuzzytv.interfaces import CacheScope

RESERVED_PREFIX = "__fuzzytv_relevant_v1__:"


class FuzzyIndex:
    """Own a separate connection for secondary-cache traffic."""

    def __init__(
        self, client: TVCacheClient, task_id: str, scope: CacheScope
    ) -> None:
        if task_id.startswith(RESERVED_PREFIX):
            raise ValueError("task ID uses the reserved fuzzy-index namespace")
        payload = json.dumps(
            [task_id, scope.initial_state_id, scope.policy_version],
            separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        self.namespace = RESERVED_PREFIX + hashlib.sha256(payload).hexdigest()
        self.client = client

    async def get(self, key: str) -> str | None:
        async with await self.client.find(self.namespace, [key]) as found:
            if found.sandbox_id is not None:
                raise ValueError("secondary entries must not contain sandboxes")
            if found.value is None:
                return None
            envelope = json.loads(found.value)
            if (
                not isinstance(envelope, dict)
                or envelope.get("version") != 1
                or envelope.get("key") != key
                or not isinstance(envelope.get("value"), str)
            ):
                raise ValueError("invalid secondary-cache value")
            return envelope["value"]

    async def put(self, key: str, value: str, ttl: int | None) -> None:
        envelope = json.dumps(
            {"version": 1, "key": key, "value": value},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        await self.client.insert(
            self.namespace, [key], envelope, None, runtime=0, ttl=ttl
        )

    async def close(self) -> None:
        await self.client.close()
