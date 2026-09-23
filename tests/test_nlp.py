"""Tests for the Pydantic schema and the Bedrock tool-use extraction path."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from chartsight.nlp import _bedrock_extract
from chartsight.schema import AnalysisExtraction, CodedCondition


def test_schema_accepts_a_valid_extraction() -> None:
    # model_validate (not the constructor) accepts raw dicts for nested models — the same
    # path _bedrock_extract uses on a tool_use `input` payload.
    extraction = AnalysisExtraction.model_validate(
        {
            "phi": [{"text": "John A. Miller", "type": "NAME"}],
            "conditions": [
                {
                    "text": "type 2 diabetes mellitus",
                    "code": "E11.9",
                    "description": "T2DM",
                    "confidence": 0.9,
                }
            ],
            "gaps": ["Documentation appears specific."],
        }
    )
    assert extraction.conditions[0].code == "E11.9"


@pytest.mark.parametrize(
    "bad_condition",
    [
        {"text": "x", "code": "not-a-code", "description": "y", "confidence": 0.9},  # malformed code
        {"text": "x", "code": "E11.9", "description": "y", "confidence": 1.5},  # confidence out of range
        {"text": "", "code": "E11.9", "description": "y", "confidence": 0.9},  # empty evidence text
    ],
)
def test_schema_rejects_invalid_conditions(bad_condition: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CodedCondition(**bad_condition)


def test_schema_caps_conditions_at_eight() -> None:
    valid = {"text": "x", "code": "E11.9", "description": "y", "confidence": 0.9}
    with pytest.raises(ValidationError):
        AnalysisExtraction.model_validate({"conditions": [valid] * 9})


def _fake_bedrock_response(tool_input: dict[str, Any]) -> Any:
    class _Body:
        def read(self) -> bytes:
            payload = {"content": [{"type": "tool_use", "name": "record_analysis", "input": tool_input}]}
            return json.dumps(payload).encode("utf-8")

    class _Client:
        def invoke_model(self, modelId: str, body: str) -> dict[str, Any]:
            return {"body": _Body()}

    return _Client()


def test_bedrock_extract_uses_tool_use_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    note = "Patient: John A. Miller   MRN: 123\n\nAssessment: type 2 diabetes mellitus, poorly controlled."
    tool_input = {
        "phi": [{"text": "John A. Miller", "type": "NAME"}],
        "conditions": [
            {
                "text": "type 2 diabetes mellitus",
                "code": "E11.9",
                "description": "Type 2 diabetes mellitus without complications",
                "confidence": 0.95,
            }
        ],
        "gaps": ["Diabetes documented without a linked complication."],
    }
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _fake_bedrock_response(tool_input))

    result = _bedrock_extract(note)

    assert result["phi"][0]["text"] == "John A. Miller"
    assert result["phi"][0]["begin"] == note.index("John A. Miller")
    assert result["conditions"][0]["code"] == "E11.9"
    assert result["conditions"][0]["begin"] == note.index("type 2 diabetes mellitus")
    assert result["gaps"] == ["Diabetes documented without a linked complication."]


def test_bedrock_extract_raises_on_invalid_tool_input(monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3

    bad_input = {"conditions": [{"text": "x", "code": "GARBAGE", "description": "y", "confidence": 0.9}]}
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _fake_bedrock_response(bad_input))

    with pytest.raises(ValidationError):
        _bedrock_extract("some note text")


# --------------------------------------------------------------------------- #
# Grounded (RAG) two-pass extraction
# --------------------------------------------------------------------------- #
class _ScriptedClient:
    """Returns one scripted tool_use payload per invoke_model call and records the request bodies."""

    def __init__(self, tool_inputs: list[dict[str, Any]]) -> None:
        self._tool_inputs = list(tool_inputs)
        self.bodies: list[dict[str, Any]] = []

    def invoke_model(self, modelId: str, body: str) -> dict[str, Any]:
        self.bodies.append(json.loads(body))
        tool_input = self._tool_inputs.pop(0)

        class _Body:
            def read(self) -> bytes:
                return json.dumps({"content": [{"type": "tool_use", "input": tool_input}]}).encode("utf-8")

        return {"body": _Body()}


_HF_NOTE = "Assessment: chronic systolic congestive heart failure, NYHA class III."
_HF_FIRST_PASS = {
    "phi": [],
    "conditions": [
        {
            "text": "chronic systolic congestive heart failure",
            "code": "I50.9",  # real but under-specific — what grounding should fix
            "description": "Heart failure, unspecified",
            "confidence": 0.7,
        }
    ],
    "gaps": ["Documentation appears specific."],
}


def _install(monkeypatch: pytest.MonkeyPatch, client: _ScriptedClient) -> None:
    import boto3

    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)


def test_grounded_reselects_from_retrieved_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    from chartsight.nlp import _bedrock_grounded

    client = _ScriptedClient([_HF_FIRST_PASS, {"c0": {"code": "I50.22", "confidence": 0.93}}])
    _install(monkeypatch, client)

    result = _bedrock_grounded(_HF_NOTE)

    (condition,) = result["conditions"]
    assert condition["code"] == "I50.22"
    assert condition["first_pass_code"] == "I50.9"
    assert condition["description"] == "Chronic systolic (congestive) heart failure"  # official, not model's
    assert condition["confidence"] == 0.93
    assert condition["begin"] == _HF_NOTE.index("chronic systolic")

    # Second call: per-condition enum of retrieved codes (+ NONE); first-pass code always offered.
    select_tool = client.bodies[1]["tools"][0]
    enum = select_tool["input_schema"]["properties"]["c0"]["properties"]["code"]["enum"]
    assert "I50.22" in enum and "I50.9" in enum and enum[-1] == "NONE"
    assert client.bodies[1]["tool_choice"] == {"type": "tool", "name": "select_codes"}


def test_grounded_prompt_never_reveals_hcc(monkeypatch: pytest.MonkeyPatch) -> None:
    from chartsight.nlp import _bedrock_grounded

    client = _ScriptedClient([_HF_FIRST_PASS, {"c0": {"code": "I50.22", "confidence": 0.9}}])
    _install(monkeypatch, client)
    _bedrock_grounded(_HF_NOTE)

    select_request = json.dumps(client.bodies[1])
    assert "HCC" not in select_request.upper()  # no payment signal to bias the choice


def test_grounded_none_moves_condition_to_ungrounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from chartsight.nlp import _bedrock_grounded

    client = _ScriptedClient([_HF_FIRST_PASS, {"c0": {"code": "NONE", "confidence": 0.8}}])
    _install(monkeypatch, client)

    result = _bedrock_grounded(_HF_NOTE)

    assert result["conditions"] == []
    assert result["ungrounded"][0]["code"] == "I50.9"
    assert result["ungrounded"][0]["candidates"]


def test_grounded_skips_second_call_when_nothing_to_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from chartsight.nlp import _bedrock_grounded

    client = _ScriptedClient([{"phi": [], "conditions": [], "gaps": []}])
    _install(monkeypatch, client)

    result = _bedrock_grounded("Assessment: well visit, no active problems.")

    assert result["conditions"] == []
    assert len(client.bodies) == 1


def test_analyze_grounded_attaches_hcc_from_crosswalk(monkeypatch: pytest.MonkeyPatch) -> None:
    from chartsight.nlp import analyze

    client = _ScriptedClient([_HF_FIRST_PASS, {"c0": {"code": "I50.22", "confidence": 0.93}}])
    _install(monkeypatch, client)

    result = analyze(_HF_NOTE, mode="aws")

    assert result["grounded"] is True
    (condition,) = result["conditions"]
    assert condition["hcc_v28"] == [
        {"hcc": "HCC226", "label": "Heart Failure, Except End Stage and Acute", "condition": ""}
    ]


def test_analyze_ungrounded_mode_is_single_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from chartsight.nlp import analyze

    client = _ScriptedClient([_HF_FIRST_PASS])
    _install(monkeypatch, client)

    result = analyze(_HF_NOTE, mode="aws", grounded=False)

    assert result["grounded"] is False
    assert result["conditions"][0]["code"] == "I50.9"
    assert len(client.bodies) == 1
