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
