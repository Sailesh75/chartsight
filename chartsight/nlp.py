"""Clinical documentation intelligence backend (Amazon Bedrock).

Pipeline for a single clinical note:
  * PHI detection    -> identifiers found, then redacted.
  * ICD-10-CM coding -> each condition, its code, a confidence, and the exact
    evidence span that supports it.
  * Grounding (RAG)  -> for each condition, retrieve real candidate codes from
    the official FY2026 code set (chartsight.retrieval) and have the model
    re-select its code from those candidates only — a second, schema-constrained
    Bedrock call. The model never has to recall a code from memory.
  * Gap review       -> documentation-specificity gaps that affect coding
    accuracy or risk-adjustment (HCC) capture.
  * Guardrail        -> every code is checked against the real ICD-10-CM code
    set (chartsight.guardrail); anything hallucinated is pulled out into
    "rejected_codes" instead of being trusted.
  * HCC enrichment   -> each verified code is tagged with its CMS-HCC V28
    category by crosswalk lookup (chartsight.reference) — deterministic, never
    model output.

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

from chartsight import guardrail, reference
from chartsight.retrieval import Candidate, default_retriever
from chartsight.schema import AnalysisExtraction

PKG_DIR = Path(__file__).resolve().parent
DATA_DIR = PKG_DIR.parent / "data"

REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
MODEL_LABEL = os.environ.get("BEDROCK_MODEL_LABEL", "Claude Haiku 4.5")
GROUNDING_K = 8  # candidates retrieved per condition for the grounded selection pass


def load_notes() -> list[dict[str, str]]:
    notes: list[dict[str, str]] = json.loads((DATA_DIR / "notes.json").read_text(encoding="utf-8"))
    return notes


def aws_available() -> bool:
    """True only if boto3 is installed AND credentials resolve."""
    if os.environ.get("FORCE_MOCK") == "1":
        return False
    try:
        import boto3

        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Amazon Bedrock extraction
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You are a clinical coding and documentation-integrity engine used in a healthcare "
    "payment-integrity setting (risk adjustment / HCC). You read a clinical note and call "
    "the record_analysis tool with the structured extraction — never respond in prose."
)

_INSTRUCTIONS = """Extract the following from the clinical note, via the record_analysis tool:

- phi: every PHI entity (name, MRN/ID, date, phone, address, email, age).
- conditions: each diagnosed condition, its most specific ICD-10-CM code, the official
  code description, and a confidence (0-1). Return 0-8 conditions.
- gaps: documentation-specificity gaps that affect coding accuracy or HCC/risk-adjustment
  capture (e.g. heart failure without type/acuity, diabetes without a linked complication,
  CKD without a stage). If documentation is already specific, return a single positive statement.

Rules:
- "text" fields MUST be exact substrings copied verbatim from the note.
- Do not code negated, family-history-only, ruled-out, or resolved/historical conditions."""

_TOOL_NAME = "record_analysis"
_TOOL_SPEC = {
    "name": _TOOL_NAME,
    "description": "Record the PHI, coded conditions, and documentation gaps extracted from a clinical note.",
    "input_schema": AnalysisExtraction.model_json_schema(),
}


def _invoke_tool(
    client: Any, system: str, prompt: str, tool: dict[str, Any], max_tokens: int
) -> dict[str, Any]:
    """One forced tool-use call; returns the tool's `input` payload."""
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "system": system,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool["name"]},
        "messages": [{"role": "user", "content": prompt}],
    }
    resp = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps(body))
    payload = json.loads(resp["body"].read())

    tool_input: dict[str, Any] | None = next(
        (block["input"] for block in payload.get("content", []) if block.get("type") == "tool_use"),
        None,
    )
    if tool_input is None:
        raise ValueError("Bedrock response contained no tool_use block")
    return tool_input


def _extract(client: Any, text: str) -> AnalysisExtraction:
    tool_input = _invoke_tool(
        client, _SYSTEM, _INSTRUCTIONS + "\n\nClinical note:\n" + text, _TOOL_SPEC, 1500
    )
    # Raises pydantic.ValidationError on a malformed code, out-of-range confidence, etc. —
    # analyze() catches that and falls back to the sample engine rather than trusting
    # unvalidated model output.
    return AnalysisExtraction.model_validate(tool_input)


def _bedrock_extract(text: str) -> dict[str, Any]:
    """Single pass, ungrounded: the model codes from memory."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name=REGION)
    return _to_result(text, _extract(client, text))


def _to_result(text: str, extraction: AnalysisExtraction) -> dict[str, Any]:
    phi = []
    for entity in extraction.phi:
        idx = text.find(entity.text)
        if idx == -1:
            continue
        phi.append({"text": entity.text, "type": entity.type, "begin": idx, "end": idx + len(entity.text)})

    conditions = []
    for condition in extraction.conditions:
        idx = text.find(condition.text)
        conditions.append(
            {
                "text": condition.text,
                "code": condition.code,
                "description": condition.description,
                "confidence": condition.confidence,
                "begin": idx if idx != -1 else 0,
                "end": (idx + len(condition.text)) if idx != -1 else 0,
            }
        )

    gaps = [g for g in extraction.gaps if g.strip()]
    return {"phi": phi, "conditions": conditions, "gaps": gaps or ["Documentation appears specific."]}


# --------------------------------------------------------------------------- #
# Grounded selection (RAG) — second pass
# --------------------------------------------------------------------------- #
_SELECT_TOOL_NAME = "select_codes"
_NO_MATCH = "NONE"

_SELECT_SYSTEM = (
    "You are a certified clinical coder. For each condition extracted from a clinical note you are "
    "given candidate ICD-10-CM codes retrieved from the official code set. Choose the single most "
    "specific candidate the documentation actually supports, via the select_codes tool."
)

_SELECT_INSTRUCTIONS = """For each numbered condition, pick exactly one code from ITS OWN candidate list.
- Choose the most specific code the note's wording supports — never assume a type, acuity,
  stage, or complication that is not documented.
- Answer "NONE" if no candidate fits, or if the condition is negated, family-history-only,
  ruled-out, or resolved.
- confidence (0-1) is how sure you are that the chosen code is right for this documentation."""


def _candidates_for(extraction: AnalysisExtraction, k: int = GROUNDING_K) -> list[list[Candidate]]:
    """Retrieve candidates per condition, querying with the mention + the model's own description.

    The model's first-pass code is kept as a candidate when it is real, so grounding only ever
    adds options — it never silently removes a correct one.
    """
    retriever = default_retriever()
    per_condition = []
    for condition in extraction.conditions:
        candidates = retriever.search(f"{condition.text} {condition.description}", k=k)
        proposed = reference.normalize(condition.code)
        entry = reference.icd10_codes().get(proposed)
        if entry is not None and all(c.code != proposed for c in candidates):
            candidates.append(Candidate(proposed, entry.description, 0.0))
        per_condition.append(candidates)
    return per_condition


def _selection_tool(candidates: list[list[Candidate]]) -> dict[str, Any]:
    """A per-condition enum, so an off-list code is unrepresentable in the tool call itself."""
    properties: dict[str, Any] = {}
    for i, options in enumerate(candidates):
        properties[f"c{i}"] = {
            "type": "object",
            "properties": {
                "code": {"type": "string", "enum": [c.code for c in options] + [_NO_MATCH]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["code", "confidence"],
        }
    return {
        "name": _SELECT_TOOL_NAME,
        "description": "Record the chosen ICD-10-CM code for each numbered condition.",
        "input_schema": {"type": "object", "properties": properties, "required": list(properties)},
    }


def _selection_prompt(text: str, extraction: AnalysisExtraction, candidates: list[list[Candidate]]) -> str:
    # HCC categories are deliberately NOT shown: telling the model which candidate risk-adjusts
    # would bias it toward the paying code — upcoding, the thing payment integrity exists to catch.
    blocks = []
    for i, (condition, options) in enumerate(zip(extraction.conditions, candidates, strict=True)):
        listed = "\n".join(f"    {c.code} — {c.description}" for c in options)
        blocks.append(f'c{i}: "{condition.text}"\n  candidates:\n{listed}')
    return (
        f"{_SELECT_INSTRUCTIONS}\n\nClinical note:\n{text}\n\n"
        "Conditions and their candidate codes:\n\n" + "\n\n".join(blocks)
    )


def _bedrock_grounded(text: str) -> dict[str, Any]:
    """Two passes: extract mentions, retrieve real candidates, re-select each code from them."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name=REGION)
    extraction = _extract(client, text)
    result = _to_result(text, extraction)
    result["ungrounded"] = []
    if not extraction.conditions:
        return result

    candidates = _candidates_for(extraction)
    selection = _invoke_tool(
        client,
        _SELECT_SYSTEM,
        _selection_prompt(text, extraction, candidates),
        _selection_tool(candidates),
        800,
    )

    grounded: list[dict[str, Any]] = []
    for i, (condition, options) in enumerate(zip(result["conditions"], candidates, strict=True)):
        choice = selection.get(f"c{i}") or {}
        chosen = str(choice.get("code", _NO_MATCH))
        by_code = {c.code: c for c in options}
        shown = [{"code": c.code, "description": c.description} for c in options]
        if chosen not in by_code:
            # "NONE" — or, defensively, anything the enum should already have prevented.
            result["ungrounded"].append({**condition, "candidates": shown, "reason": "no candidate fits"})
            continue
        grounded.append(
            {
                **condition,
                "code": chosen,
                "description": by_code[chosen].description,
                "confidence": float(choice.get("confidence", condition["confidence"])),
                "first_pass_code": condition["code"],
                "candidates": shown,
            }
        )
    result["conditions"] = grounded
    return result


# --------------------------------------------------------------------------- #
# Local sample engine (fallback when AWS is unavailable)
# --------------------------------------------------------------------------- #
_ICD_PATTERNS: list[tuple[str, str, str, float]] = [
    (
        "diabetic peripheral neuropathy",
        "E11.42",
        "Type 2 diabetes mellitus with diabetic polyneuropathy",
        0.93,
    ),
    ("type 2 diabetes mellitus", "E11.9", "Type 2 diabetes mellitus without complications", 0.95),
    (
        "chronic systolic congestive heart failure",
        "I50.22",
        "Chronic systolic (congestive) heart failure",
        0.94,
    ),
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
            {
                "text": text[idx : idx + len(phrase)],
                "code": code,
                "description": description,
                "confidence": score,
                "begin": idx,
                "end": idx + len(phrase),
            }
        )
    conditions.sort(key=lambda c: c["begin"])
    return conditions


def _sample_gaps(conditions: list[dict[str, Any]]) -> list[str]:
    gaps: list[str] = []
    codes = {c["code"] for c in conditions}
    has_dm_complication = any(c["code"].startswith("E11.") and c["code"] != "E11.9" for c in conditions)
    if "E11.9" in codes and not has_dm_complication:
        gaps.append(
            "**Diabetes** is documented without a linked complication (E11.9 → V28 HCC 38). The V28 "
            "diabetes HCCs share one coefficient, so this is a coding-accuracy gap rather than a RAF one: "
            "if a manifestation is present (e.g. diabetic neuropathy, CKD, retinopathy), document the link "
            "so the combination code (e.g. E11.42, E11.22) is captured."
        )
    if "I50.9" in codes:
        gaps.append(
            "**Heart failure** is coded as unspecified (I50.9 → V28 HCC 226). Document the type "
            "(systolic/diastolic) and acuity (acute/chronic) for an accurate I50.2x–I50.4x code; acute and "
            "acute-on-chronic failure map to the higher-weighted HCC 225/224."
        )
    if "N18.9" in codes:
        gaps.append(
            "**Chronic kidney disease** lacks a stage (N18.9). Documenting the stage (N18.1–N18.6) restores "
            "specificity and, at stage 3+, HCC capture."
        )
    if not gaps:
        gaps.append(
            "Documentation appears specific — the extracted conditions map to well-defined ICD-10-CM codes."
        )
    return gaps


# --------------------------------------------------------------------------- #
# Redaction + orchestrator
# --------------------------------------------------------------------------- #
def redact(text: str, phi: list[dict[str, Any]]) -> str:
    result = text
    for span in sorted(phi, key=lambda s: s["begin"], reverse=True):
        result = result[: span["begin"]] + f"[{span['type']}]" + result[span["end"] :]
    return result


def _with_hcc(condition: dict[str, Any]) -> dict[str, Any]:
    """Attach CMS-HCC V28 categories by crosswalk lookup ([] = does not risk-adjust)."""
    hcc = [
        {"hcc": m.hcc, "label": m.label, "condition": m.condition}
        for m in reference.hcc_for(condition["code"])
    ]
    return {**condition, "hcc_v28": hcc}


def analyze(text: str, mode: str = "auto", grounded: bool = True) -> dict[str, Any]:
    """Run the full pipeline.

    mode="auto" -> Amazon Bedrock if credentials resolve, else the sample engine.
    mode="aws"  -> force Bedrock.
    mode="mock" -> force the local sample engine.

    grounded=True  -> Bedrock codes are re-selected from retrieved official candidates (two calls).
    grounded=False -> single-pass, the model codes from memory (kept for A/B evaluation).
    The sample engine is rule-based and unaffected.
    """
    use_aws = mode == "aws" or (mode == "auto" and aws_available())
    engine = f"Amazon Bedrock · {MODEL_LABEL}" + (" · grounded" if grounded else "")
    fallback_reason = None
    ungrounded: list[dict[str, Any]] = []

    if use_aws:
        try:
            data = _bedrock_grounded(text) if grounded else _bedrock_extract(text)
            phi, conditions, gaps = data["phi"], data["conditions"], data["gaps"]
            ungrounded = data.get("ungrounded", [])
        except Exception as exc:
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

    # Guardrail: never trust a code just because the model (or the sample engine) said
    # so — verify it's real against the FY2026 CMS/CDC ICD-10-CM code set. Applied to
    # both engines uniformly, outside the AWS try/except above so a guardrail issue is
    # never mistaken for "Bedrock unavailable".
    checked = guardrail.apply(conditions)

    return {
        "engine": engine,
        "region": REGION if use_aws else None,
        "fallback_reason": fallback_reason,
        "phi": phi,
        "redacted": redact(text, phi),
        "grounded": use_aws and grounded,
        "conditions": [_with_hcc(c) for c in checked.verified],
        "rejected_codes": checked.rejected,
        "ungrounded": ungrounded,
        "insights": {"engine": engine, "gaps": gaps},
    }
