"""Official reference data the pipeline grounds itself on — loaded once, cached.

  * data/icd10cm_codes.tsv  — every billable FY2026 ICD-10-CM code, its official
    description, tabular inclusion terms and Alphabetic Index entries (the
    retrieval corpus and the guardrail's code set).
  * data/hcc_v28.tsv        — the CMS-HCC V28 ICD-10 → HCC crosswalk used by the
    2026 payment model, plus HCC labels.

Both are built from the public CDC/CMS releases by scripts/build_reference.py —
see that script for provenance and how to refresh them for a new year.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CODES_PATH = DATA_DIR / "icd10cm_codes.tsv"
HCC_PATH = DATA_DIR / "hcc_v28.tsv"


@dataclass(frozen=True)
class CodeEntry:
    code: str  # dotted, e.g. "E11.22"
    description: str
    terms: tuple[str, ...] = ()  # tabular inclusion terms, e.g. "Congestive heart failure NOS"
    index_terms: tuple[str, ...] = ()  # Alphabetic Index paths, e.g. "Disease, diseased kidney chronic"


@dataclass(frozen=True)
class HCCMapping:
    hcc: str  # e.g. "HCC226"
    label: str  # e.g. "Heart Failure, Except End Stage and Acute"
    condition: str = ""  # CMS age/sex edit, e.g. "age < 50"; "" when unconditional


def _split_terms(cols: list[str], i: int) -> tuple[str, ...]:
    return tuple(t for t in cols[i].split(" | ") if t) if len(cols) > i else ()


def _data_lines(path: Path) -> list[list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.split("\t") for line in lines if line and not line.startswith("#")]


@lru_cache(maxsize=1)
def icd10_codes() -> dict[str, CodeEntry]:
    entries: dict[str, CodeEntry] = {}
    for cols in _data_lines(CODES_PATH):
        code, description = cols[0], cols[1]
        entries[code] = CodeEntry(code, description, _split_terms(cols, 2), _split_terms(cols, 3))
    return entries


@lru_cache(maxsize=1)
def hcc_crosswalk() -> dict[str, tuple[HCCMapping, ...]]:
    grouped: dict[str, list[HCCMapping]] = {}
    for cols in _data_lines(HCC_PATH):
        code, hcc, label = cols[0], cols[1], cols[2]
        condition = cols[3] if len(cols) > 3 else ""
        grouped.setdefault(code, []).append(HCCMapping(hcc, label, condition))
    return {code: tuple(mappings) for code, mappings in grouped.items()}


def normalize(code: str) -> str:
    """'e119' / 'E11.9 ' / 'E11.9' -> 'E11.9' (the dotted form every table here uses)."""
    raw = code.strip().upper().replace(".", "")
    return raw if len(raw) <= 3 else f"{raw[:3]}.{raw[3:]}"


def is_valid(code: str) -> bool:
    return normalize(code) in icd10_codes()


def description(code: str) -> str | None:
    entry = icd10_codes().get(normalize(code))
    return entry.description if entry else None


def hcc_for(code: str) -> tuple[HCCMapping, ...]:
    """V28 HCC(s) a code maps to — () when the code does not risk-adjust."""
    return hcc_crosswalk().get(normalize(code), ())
