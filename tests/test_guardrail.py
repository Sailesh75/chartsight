"""Tests for the hallucinated-code guardrail and its reference data."""

from __future__ import annotations

import os

os.environ["FORCE_MOCK"] = "1"

from chartsight import guardrail
from chartsight.nlp import _ICD_PATTERNS, analyze
from evals.fragments import FRAGMENTS


def test_reference_file_loaded_and_sane() -> None:
    codes = guardrail.valid_codes()
    assert len(codes) > 50_000
    assert "E11.9" in codes
    assert "I10" in codes


def test_apply_splits_verified_from_hallucinated() -> None:
    conditions = [
        {"text": "a", "code": "E11.9", "description": "real"},
        {"text": "b", "code": "Z99.99Z", "description": "not a real code"},
    ]
    result = guardrail.apply(conditions)
    assert [c["code"] for c in result.verified] == ["E11.9"]
    assert result.rejected[0]["code"] == "Z99.99Z"
    assert "not a valid ICD-10-CM code" in result.rejected[0]["reason"]


def test_apply_is_case_and_whitespace_tolerant() -> None:
    result = guardrail.apply([{"text": "a", "code": " e11.9 ", "description": "x"}])
    assert len(result.verified) == 1


def test_every_fragment_code_is_a_real_icd10_code() -> None:
    """Ties evals/fragments.py's ground truth back to the official code set."""
    codes = guardrail.valid_codes()
    for fragment in FRAGMENTS:
        for expected in fragment.codes:
            assert expected.code in codes, f"{fragment.id}: {expected.code} is not a real ICD-10-CM code"


def test_every_sample_engine_code_is_a_real_icd10_code() -> None:
    codes = guardrail.valid_codes()
    for _phrase, code, _description, _score in _ICD_PATTERNS:
        assert code in codes, f"sample engine code {code} is not a real ICD-10-CM code"


def test_analyze_exposes_rejected_codes_key() -> None:
    result = analyze("Assessment: essential hypertension.", mode="mock")
    assert "rejected_codes" in result
    assert result["rejected_codes"] == []


def test_apply_normalizes_undotted_codes_to_the_dotted_form() -> None:
    result = guardrail.apply([{"text": "a", "code": "e119", "description": "x"}])
    assert [c["code"] for c in result.verified] == ["E11.9"]
