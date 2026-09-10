"""Clinical documentation intelligence backend (Amazon Bedrock).

Pipeline for a single clinical note, in one Bedrock call:
  * PHI detection    -> identifiers found, then redacted.
  * ICD-10-CM coding -> each condition, its code, a confidence, and the exact
    evidence span that supports it.
  * Gap review       -> documentation-specificity gaps that reduce
    risk-adjustment (HCC) capture.

Two tiers keep the demo alive:
  * Bedrock - live Amazon Bedrock (Claude) when AWS credentials resolve.
  * sample  - a local keyword/rule engine when no credentials are present, so
    the UI runs with zero setup and zero cost.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
MODEL_LABEL = os.environ.get("BEDROCK_MODEL_LABEL", "Claude Haiku 4.5")


def load_notes() -> list[dict[str, str]]:
    return json.loads((DATA_DIR / "notes.json").read_text(encoding="utf-8"))


def aws_available() -> bool:
    """True only if boto3 is installed AND credentials resolve."""
    if os.environ.get("FORCE_MOCK") == "1":
        return False
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Amazon Bedrock extraction
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You are a clinical coding and documentation-integrity engine used in a healthcare "
    "payment-integrity setting (risk adjustment / HCC). You read a clinical note and return "
    "structured data. Return ONLY valid JSON — no prose, no markdown fences."
)

_SCHEMA_INSTRUCTIONS = """Extract the following from the clinical note and return ONLY this JSON object:

{
  "phi": [ {"text": "<verbatim substring from the note>", "type": "NAME|ID|DATE|PHONE|ADDRESS|EMAIL|AGE"} ],
  "conditions": [ {"text": "<verbatim evidence substring>", "code": "<ICD-10-CM code>", "description": "<official code description>", "confidence": <number 0..1>} ],
  "gaps": [ "<one concise documentation-specificity gap that reduces HCC/risk-adjustment capture, with a concrete fix>" ]
}

Rules:
- "text" fields MUST be exact substrings copied verbatim from the note.
- Assign the most specific ICD-10-CM code the documentation supports.
- In "gaps", flag unspecified diagnoses that lose HCC capture (e.g. heart failure without type/acuity, diabetes without a linked complication, CKD without a stage). If documentation is already specific, return a single positive statement.
- Return 0-8 conditions. Output JSON only."""


def _extract_json(text: str) -> dict[str, Any]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


def _bedrock_extract(text: str) -> dict[str, Any]:
    import boto3

    client = boto3.client("bedrock-runtime", region_name=REGION)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1500,
        "system": _SYSTEM,
        "messages": [{"role": "user", "content": _SCHEMA_INSTRUCTIONS + "\n\nClinical note:\n" + text}],
    }
    resp = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
    payload = json.loads(resp["body"].read())
    raw = "".join(block.get("text", "") for block in payload.get("content", []))
    data = _extract_json(raw)

    phi = []
    for e in data.get("phi", []):
        value = str(e.get("text", "")).strip()
        idx = text.find(value)
        if not value or idx == -1:
            continue
        phi.append({"text": value, "type": str(e.get("type", "PHI")).upper(), "begin": idx, "end": idx + len(value)})

    conditions = []
    for c in data.get("conditions", []):
        value = str(c.get("text", "")).strip()
        idx = text.find(value)
        try:
            conf = max(0.0, min(1.0, float(c.get("confidence", 0.9))))
        except (TypeError, ValueError):
            conf = 0.9
        conditions.append(
            {
                "text": value,
                "code": str(c.get("code", "—")),
                "description": str(c.get("description", "")),
                "confidence": conf,
                "begin": idx if idx != -1 else 0,
                "end": (idx + len(value)) if idx != -1 else 0,
            }
        )

    gaps = [str(g) for g in data.get("gaps", []) if str(g).strip()]
    return {"phi": phi, "conditions": conditions, "gaps": gaps or ["Documentation appears specific."]}


# --------------------------------------------------------------------------- #
# Local sample engine (fallback when AWS is unavailable)
# --------------------------------------------------------------------------- #
_ICD_PATTERNS: list[tuple[str, str, str, float]] = [
    ("diabetic peripheral neuropathy", "E11.42", "Type 2 diabetes mellitus with diabetic polyneuropathy", 0.93),
    ("type 2 diabetes mellitus", "E11.9", "Type 2 diabetes mellitus without complications", 0.95),
    ("chronic systolic congestive heart failure", "I50.22", "Chronic systolic (congestive) heart failure", 0.94),
    ("acute exacerbation of heart failure", "I50.9", "Heart failure, unspecified", 0.88),
    ("congestive heart failure", "I50.9", "Heart failure, unspecified", 0.86),
    ("heart failure", "I50.9", "Heart failure, unspecified", 0.85),
    ("stage 3b chronic kidney disease", "N18.32", "Chronic kidney disease, stage 3b", 0.93),
    ("chronic kidney disease", "N18.9", "Chronic kidney disease, unspecified", 0.84),
    ("atrial fibrillation", "I48.91", "Unspecified atrial fibrillation", 0.92),
    ("essential hypertension", "I10", "Essential (primary) hypertension", 0.95),
    ("high blood pressure", "I10", "Essential (primary) hypertension", 0.80),
    ("hyperlipidemia", "E78.5", "Hyperlipidemia, unspecified", 0.90),
    ("copd", "J44.9", "Chronic obstructive pulmonary disease, unspecified", 0.88),
    ("diabetes", "E11.9", "Type 2 diabetes mellitus without complications", 0.78),
]

_PHI_PATTERNS: list[tuple[str, str]] = [
    (r"(?<=Patient:)\s*[A-Z][^\n]*?(?=\s{2,}|$)", "NAME"),
    (r"(?<=MRN:)\s*\w+", "ID"),
    (r"\b\d{1,2}[/-][A-Za-z]{3}[/-]\d{4}\b", "DATE"),
    (r"\b\d{2}[/-]\d{2}[/-]\d{4}\b", "DATE"),
    (r"\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}", "PHONE"),
]


def _sample_phi(text: str) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for pattern, phi_type in _PHI_PATTERNS:
        for m in re.finditer(pattern, text):
            value = m.group().strip()
            if not value:
                continue
            start = m.start() + (len(m.group()) - len(m.group().lstrip()))
            spans.append({"text": value, "type": phi_type, "begin": start, "end": start + len(value)})
    spans.sort(key=lambda s: s["begin"])
    deduped: list[dict[str, Any]] = []
    for s in spans:
        if not any(s["begin"] < d["end"] and d["begin"] < s["end"] for d in deduped):
            deduped.append(s)
    return deduped


def _sample_icd10(text: str) -> list[dict[str, Any]]:
    lower = text.lower()
    used: list[tuple[int, int]] = []
    seen_codes: set[str] = set()
    conditions: list[dict[str, Any]] = []
    for phrase, code, description, score in _ICD_PATTERNS:
        idx = lower.find(phrase)
        if idx == -1:
            continue
        span = (idx, idx + len(phrase))
        if any(span[0] < u[1] and u[0] < span[1] for u in used):
            continue
        if code in seen_codes:
            continue
        used.append(span)
        seen_codes.add(code)
        conditions.append(
            {"text": text[idx : idx + len(phrase)], "code": code, "description": description, "confidence": score, "begin": idx, "end": idx + len(phrase)}
        )
    conditions.sort(key=lambda c: c["begin"])
    return conditions


def _sample_gaps(conditions: list[dict[str, Any]]) -> list[str]:
    gaps: list[str] = []
    codes = {c["code"] for c in conditions}
    has_dm_complication = any(c["code"].startswith("E11.") and c["code"] != "E11.9" for c in conditions)
    if "E11.9" in codes and not has_dm_complication:
        gaps.append(
            "**Diabetes** is documented without a linked complication. Specifying a manifestation "
            "(e.g. diabetic neuropathy, CKD, retinopathy) moves it from E11.9 to an HCC-eligible code."
        )
    if "I50.9" in codes:
        gaps.append(
            "**Heart failure** is coded as unspecified (I50.9), which does **not** risk-adjust. Document the "
            "type (systolic/diastolic) and acuity (acute/chronic) to reach HCC-eligible I50.2x–I50.4x."
        )
    if "N18.9" in codes:
        gaps.append(
            "**Chronic kidney disease** lacks a stage (N18.9). Documenting the stage (N18.1–N18.6) restores "
            "specificity and, at stage 3+, HCC capture."
        )
    if not gaps:
        gaps.append("Documentation appears specific — the extracted conditions map to well-defined ICD-10-CM codes.")
    return gaps


# --------------------------------------------------------------------------- #
# Redaction + orchestrator
# --------------------------------------------------------------------------- #
def redact(text: str, phi: list[dict[str, Any]]) -> str:
    result = text
    for span in sorted(phi, key=lambda s: s["begin"], reverse=True):
        result = result[: span["begin"]] + f"[{span['type']}]" + result[span["end"] :]
    return result


def analyze(text: str, mode: str = "auto") -> dict[str, Any]:
    """Run the full pipeline.

    mode="auto" -> Amazon Bedrock if credentials resolve, else the sample engine.
    mode="aws"  -> force Bedrock.
    mode="mock" -> force the local sample engine.
    """
    use_aws = mode == "aws" or (mode == "auto" and aws_available())
    engine = f"Amazon Bedrock · {MODEL_LABEL}"
    fallback_reason = None

    if use_aws:
        try:
            data = _bedrock_extract(text)
            phi, conditions, gaps = data["phi"], data["conditions"], data["gaps"]
        except Exception as exc:  # noqa: BLE001 - demo resilience
            use_aws = False
            engine = "Local sample (Bedrock unavailable)"
            fallback_reason = f"{type(exc).__name__}: {exc}"
            phi = _sample_phi(text)
            conditions = _sample_icd10(text)
            gaps = _sample_gaps(conditions)
    else:
        engine = "Local sample (keyword-based)"
        phi = _sample_phi(text)
        conditions = _sample_icd10(text)
        gaps = _sample_gaps(conditions)

    return {
        "engine": engine,
        "region": REGION if use_aws else None,
        "fallback_reason": fallback_reason,
        "phi": phi,
        "redacted": redact(text, phi),
        "conditions": conditions,
        "insights": {"engine": engine, "gaps": gaps},
    }
