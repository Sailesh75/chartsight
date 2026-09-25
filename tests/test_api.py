"""Tests for the HTTP API, and for the UI's HTTP client against a real running server."""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator

os.environ["FORCE_MOCK"] = "1"

import pytest
import uvicorn
from fastapi.testclient import TestClient

from chartsight import raf
from chartsight.api import AnalyzeRequest, app
from chartsight.client import HTTPBackend, LocalBackend, get_backend

NOTE = "Assessment: 74-year-old female with congestive heart failure and chronic kidney disease."


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["engine"] == "local sample engine"


def test_analyze_returns_full_pipeline_result(client: TestClient) -> None:
    resp = client.post("/v1/analyze", json={"text": NOTE, "mode": "mock"})
    assert resp.status_code == 200
    body = resp.json()
    assert {c["code"] for c in body["conditions"]} == {"I50.9", "N18.9"}
    assert body["conditions"][0]["hcc_v28"]
    assert body["raf"]["demographics"]["age"] == 74
    assert body["gaps"]


def test_analyze_honors_demographics_and_segment_overrides(client: TestClient) -> None:
    body = client.post(
        "/v1/analyze",
        json={
            "text": NOTE,
            "mode": "mock",
            "demographics": {"age": 80, "sex": "M"},
            "segment": "COMMUNITY_FBA",
            "base_rate_pmpm": 1000,
        },
    ).json()
    assert body["raf"]["demographics"]["age"] == 80
    assert body["raf"]["segment"] == "COMMUNITY_FBA"
    assert body["raf"]["base_rate_pmpm"] == 1000


@pytest.mark.parametrize(
    "payload",
    [
        {"text": ""},
        {"text": "x", "mode": "gpt"},
        {"text": "x", "segment": "COMMUNITY_XX"},
        {"text": "x", "base_rate_pmpm": 0},
        {"text": "x", "demographics": {"age": 70, "sex": "X"}},
    ],
)
def test_analyze_rejects_bad_input(client: TestClient, payload: dict[str, object]) -> None:
    assert client.post("/v1/analyze", json=payload).status_code == 422


def test_analyze_rejects_oversized_note(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chartsight.api.MAX_NOTE_CHARS", 10)
    assert client.post("/v1/analyze", json={"text": "x" * 11}).status_code == 422


def test_raf_endpoint_is_deterministic_scoring(client: TestClient) -> None:
    payload = {"codes": ["E119", "I50.9"], "demographics": {"age": 68, "sex": "M"}}
    body = client.post("/v1/raf", json=payload).json()
    expected = raf.score(["E11.9", "I50.9"], raf.Demographics(68, 1)).payment
    assert body["payment"] == round(expected, 3)
    assert "DIABETES_HF_V28" in {t["variable"] for t in body["terms"]}


def test_raf_endpoint_requires_demographics(client: TestClient) -> None:
    assert client.post("/v1/raf", json={"codes": ["I10"]}).status_code == 422


def test_code_search(client: TestClient) -> None:
    body = client.get("/v1/codes/search", params={"q": "chronic systolic heart failure", "k": 3}).json()
    assert len(body) == 3
    assert body[0]["code"] == "I50.22"
    assert body[0]["hcc_v28"] == ["HCC226"]


def test_api_key_enforced_when_configured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHARTSIGHT_API_KEY", "s3cret")
    payload = {"text": NOTE, "mode": "mock"}
    assert client.post("/v1/analyze", json=payload).status_code == 401
    assert client.post("/v1/analyze", json=payload, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/v1/analyze", json=payload, headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200  # liveness stays open for load balancers


def test_segment_default_matches_library() -> None:
    assert AnalyzeRequest(text="x").segment == raf.DEFAULT_SEGMENT


# --------------------------------------------------------------------------- #
# The UI's HTTP backend, against a real server
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def live_server() -> Iterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def test_http_backend_matches_local_backend(live_server: str) -> None:
    remote, local = HTTPBackend(live_server), LocalBackend()
    remote_result, local_result = remote.analyze(NOTE), local.analyze(NOTE)
    assert [c["code"] for c in remote_result["conditions"]] == [c["code"] for c in local_result["conditions"]]
    assert remote_result["insights"]["gaps"] == local_result["insights"]["gaps"]

    demo = raf.Demographics(74, 2)
    codes = [c["code"] for c in local_result["conditions"]]
    assert remote.assess(codes, demo, "COMMUNITY_NA", 1230.52) == local.assess(
        codes, demo, "COMMUNITY_NA", 1230.52
    )


def test_http_backend_surfaces_api_errors(live_server: str) -> None:
    with pytest.raises(RuntimeError, match="422"):
        HTTPBackend(live_server).analyze("")


def test_get_backend_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHARTSIGHT_API_URL", raising=False)
    assert isinstance(get_backend(), LocalBackend)
    monkeypatch.setenv("CHARTSIGHT_API_URL", "http://api:8000/")
    backend = get_backend()
    assert isinstance(backend, HTTPBackend) and backend.base_url == "http://api:8000"
