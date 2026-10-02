"""Daily cap on live (billed) Bedrock analyses, for a public deployment.

    CHARTSIGHT_LIVE_DAILY_LIMIT   live analyses allowed per UTC day; unset = unlimited
    CHARTSIGHT_QUOTA_TABLE        DynamoDB table to count in (partition key "day", string)

With a table, the count is shared by every server instance and enforced atomically: a
conditional update only increments while the count is under the limit. Without one, the
count lives in process memory, which is right for a single local server but resets on
restart. Once the cap is reached, the API answers with the free sample engine instead.

One unit = one note, whether that takes one Bedrock call or two (grounded).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Protocol

_TTL_SECONDS = 3 * 24 * 3600  # DynamoDB TTL removes old day rows on its own


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class Quota(Protocol):
    limit: int

    def try_acquire(self) -> bool: ...

    def used(self) -> int: ...


@dataclass
class MemoryQuota:
    limit: int
    _counts: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def try_acquire(self) -> bool:
        with self._lock:
            day = today()
            if self._counts.get(day, 0) >= self.limit:
                return False
            self._counts = {day: self._counts.get(day, 0) + 1}  # drops earlier days
            return True

    def used(self) -> int:
        return self._counts.get(today(), 0)


@dataclass
class DynamoQuota:
    limit: int
    table: str
    client: Any  # boto3 DynamoDB client

    def try_acquire(self) -> bool:
        try:
            self.client.update_item(
                TableName=self.table,
                Key={"day": {"S": today()}},
                UpdateExpression="ADD n :one SET expires_at = if_not_exists(expires_at, :ttl)",
                ConditionExpression="attribute_not_exists(n) OR n < :limit",
                ExpressionAttributeValues={
                    ":one": {"N": "1"},
                    ":limit": {"N": str(self.limit)},
                    ":ttl": {"N": str(int(time.time()) + _TTL_SECONDS)},
                },
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def used(self) -> int:
        item = self.client.get_item(TableName=self.table, Key={"day": {"S": today()}}).get("Item")
        return int(item["n"]["N"]) if item and "n" in item else 0


@lru_cache(maxsize=4)
def _build(limit: int, table: str | None) -> Quota:
    if table:
        import boto3

        return DynamoQuota(limit, table, boto3.client("dynamodb"))
    return MemoryQuota(limit)


def get_quota() -> Quota | None:
    """The configured quota, or None when live analysis is unlimited."""
    raw = os.environ.get("CHARTSIGHT_LIVE_DAILY_LIMIT")
    if not raw:
        return None
    return _build(int(raw), os.environ.get("CHARTSIGHT_QUOTA_TABLE") or None)
