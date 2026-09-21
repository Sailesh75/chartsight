"""Hallucinated-code guardrail: reject any ICD-10-CM code the model returns
that isn't in the real CMS/CDC code set (data/icd10cm_valid_codes.txt, FY2026
— see scripts/build_icd10_reference.py for provenance and how to refresh it).

This is the difference between "I prompted an LLM to output codes" and "I
verify every code against the ground truth before it reaches a user" — the
kind of grounding check a payment-integrity system needs regardless of how
good the model is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

PKG_DIR = Path(__file__).resolve().parent
CODES_PATH = PKG_DIR.parent / "data" / "icd10cm_valid_codes.txt"


@lru_cache(maxsize=1)
def valid_codes() -> frozenset[str]:
    lines = CODES_PATH.read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip() for line in lines if line.strip() and not line.startswith("#"))


@dataclass
class GuardrailResult:
    verified: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)


def apply(conditions: list[dict[str, Any]]) -> GuardrailResult:
    """Split `conditions` into those with a real ICD-10-CM code and those without.

    A rejected entry keeps every original field plus `"reason"` explaining why —
    the caller decides whether to show it, log it, or just drop it.
    """
    codes = valid_codes()
    result = GuardrailResult()
    for condition in conditions:
        code = str(condition.get("code", "")).strip().upper()
        if code in codes:
            result.verified.append(condition)
        else:
            result.rejected.append(
                {**condition, "reason": f"'{code}' is not a valid ICD-10-CM code (FY2026)"}
            )
    return result
