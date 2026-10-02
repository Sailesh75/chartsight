"""Tests for the daily cap on live (billed) analyses."""

from __future__ import annotations

import os

os.environ["FORCE_MOCK"] = "1"

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chartsight import quota
from chartsight.api import app
from chartsight.nlp import analyze as real_analyze


@pytest.fixture(autouse=True)
def _fresh_quota() -> Iterator[None]:
    quota._build.cache_clear()
    yield
    quota._build.cache_clear()


def test_unset_limit_means_unlimited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHARTSIGHT_LIVE_DAILY_LIMIT", raising=False)
    assert quota.get_quota() is None


def test_memory_quota_stops_at_the_limit() -> None:
    q = quota.MemoryQuota(limit=2)
    assert [q.try_acquire() for _ in range(3)] == [True, True, False]
    assert q.used() == 2


class _ConditionFailed(Exception):
    pass


class FakeDynamo:
    """Just enough of the DynamoDB client: a conditional ADD on one counter per day."""

    class exceptions:
        ConditionalCheckFailedException = _ConditionFailed

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}

    def update_item(
        self, *, Key: dict[str, Any], ExpressionAttributeValues: dict[str, Any], **_: Any
    ) -> None:
        day = Key["day"]["S"]
        n = int(self.items.get(day, {}).get("n", {"N": "0"})["N"])
        if day in self.items and n >= int(ExpressionAttributeValues[":limit"]["N"]):
            raise _ConditionFailed
        self.items[day] = {"day": {"S": day}, "n": {"N": str(n + 1)}}

    def get_item(self, *, Key: dict[str, Any], **_: Any) -> dict[str, Any]:
        item = self.items.get(Key["day"]["S"])
        return {"Item": item} if item else {}


def test_dynamo_quota_is_a_conditional_counter() -> None:
    q = quota.DynamoQuota(limit=2, table="t", client=FakeDynamo())
    assert q.used() == 0
    assert [q.try_acquire() for _ in range(3)] == [True, True, False]
    assert q.used() == 2


def test_api_falls_back_to_sample_engine_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTSIGHT_LIVE_DAILY_LIMIT", "1")
    monkeypatch.delenv("CHARTSIGHT_QUOTA_TABLE", raising=False)
    monkeypatch.setattr("chartsight.api.aws_available", lambda: True)
    modes: list[str] = []

    def fake_analyze(text: str, mode: str = "auto", **kwargs: Any) -> dict[str, Any]:
        modes.append(mode)
        return real_analyze(text, mode="mock", **kwargs)  # never actually call Bedrock in tests

    monkeypatch.setattr("chartsight.api.analyze", fake_analyze)
    client = TestClient(app)
    note = {"text": "Assessment: 70-year-old male with heart failure."}

    first = client.post("/v1/analyze", json=note).json()
    second = client.post("/v1/analyze", json=note).json()
    assert modes == ["auto", "mock"]
    assert (first["quota_exhausted"], second["quota_exhausted"]) == (False, True)
    assert client.get("/health").json()["live_quota"] == {"limit": 1, "used": 1}

    # An explicit mock request is free, so it never touches the quota.
    assert client.post("/v1/analyze", json={**note, "mode": "mock"}).json()["quota_exhausted"] is False
