"""How the Streamlit UI (or any Python caller) talks to ChartSight: in-process or over HTTP.

    backend = get_backend()   # HTTPBackend if CHARTSIGHT_API_URL is set, else LocalBackend

Both return the same dict shapes, so a caller never needs to know which one it has.
The HTTP backend uses only the standard library, so the UI needs no extra dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

from chartsight import raf


class Backend(Protocol):
    label: str

    def analyze(self, text: str) -> dict[str, Any]: ...

    def assess(
        self, codes: list[str], demo: raf.Demographics, segment: str, base_rate_pmpm: float
    ) -> dict[str, Any]: ...


class LocalBackend:
    label = "in-process"

    def analyze(self, text: str) -> dict[str, Any]:
        from chartsight.nlp import analyze

        return analyze(text)

    def assess(
        self, codes: list[str], demo: raf.Demographics, segment: str, base_rate_pmpm: float
    ) -> dict[str, Any]:
        return raf.assess(codes, demo, segment, base_rate_pmpm)


class HTTPBackend:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.label = f"API at {self.base_url}"

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        request = urllib.request.Request(
            self.base_url + path, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body: dict[str, Any] = json.loads(resp.read())
                return body
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"ChartSight API {path} returned {exc.code}: {detail}") from exc

    def analyze(self, text: str) -> dict[str, Any]:
        result = self._post("/v1/analyze", {"text": text})
        result.setdefault("insights", {"engine": result["engine"], "gaps": result.get("gaps", [])})
        return result

    def assess(
        self, codes: list[str], demo: raf.Demographics, segment: str, base_rate_pmpm: float
    ) -> dict[str, Any]:
        demographics = {
            "age": demo.age,
            "sex": "M" if demo.sex == 1 else "F",
            "orec": demo.orec,
            "ltimcaid": demo.ltimcaid,
        }
        return self._post(
            "/v1/raf",
            {
                "codes": codes,
                "demographics": demographics,
                "segment": segment,
                "base_rate_pmpm": base_rate_pmpm,
            },
        )


def get_backend() -> Backend:
    url = os.environ.get("CHARTSIGHT_API_URL")
    if url:
        return HTTPBackend(url, api_key=os.environ.get("CHARTSIGHT_API_KEY"))
    return LocalBackend()
