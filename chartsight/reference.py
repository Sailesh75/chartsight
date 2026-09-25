"""Official reference data the pipeline grounds itself on — loaded once, cached.

  * data/icd10cm_codes.tsv  — every billable FY2026 ICD-10-CM code, its official
    description, tabular inclusion terms and Alphabetic Index entries (the
    retrieval corpus and the guardrail's code set).
  * data/hcc_v28.tsv        — the CMS-HCC V28 ICD-10 → HCC crosswalk used by the
    2026 payment model, plus HCC labels and CMS's age/sex edits.

Both are built from the public CDC/CMS releases by scripts/build_reference.py —
see that script for provenance and how to refresh them for a new year.
"""

from __future__ import annotations

import operator
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# The repo's data/ by default; CHARTSIGHT_DATA_DIR points an installed package (e.g. the
# Docker image) at a copy elsewhere. Every module resolves data through this one constant.
DATA_DIR = Path(os.environ.get("CHARTSIGHT_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
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
    age_edit: str = ""  # CMS age edit, e.g. "age < 50"; "" when none
    sex_edit: str = ""  # "1" (male) / "2" (female); "" when none
    mce_age: str = ""  # Medicare Code Editor age edit, e.g. "0 <= age <= 17"

    @property
    def condition(self) -> str:
        """Human-readable summary of the edits, "" when the mapping is unconditional."""
        parts = [self.age_edit, self.mce_age]
        if self.sex_edit:
            parts.append("male" if self.sex_edit == "1" else "female")
        return "; ".join(p for p in parts if p)

    def applies(self, age: int, sex: int) -> bool:
        """True when every CMS edit holds for this beneficiary (sex: 1 = male, 2 = female)."""
        if self.sex_edit and int(self.sex_edit) != sex:
            return False
        return all(_age_rule_holds(rule, age) for rule in (self.age_edit, self.mce_age) if rule)


_OPS: dict[str, Callable[[int, int], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "=": operator.eq,
    "==": operator.eq,
}
_RULE_TOKEN = re.compile(r"\s*(<=|>=|==|=|<|>|age|\d+)")


def _age_rule_holds(rule: str, age: int) -> bool:
    """Evaluate a CMS age edit ("age < 50", "0 <= age <= 17", "age = 0") without eval()."""
    tokens: list[str] = []
    pos = 0
    rule = rule.strip().lower()
    while pos < len(rule):
        match = _RULE_TOKEN.match(rule, pos)
        if match is None:
            raise ValueError(f"unparseable CMS age edit: {rule!r}")
        tokens.append(match.group(1))
        pos = match.end()
    values = [age if t == "age" else int(t) for t in tokens[::2]]
    ops = tokens[1::2]
    if len(values) != len(ops) + 1 or not ops or any(op not in _OPS for op in ops):
        raise ValueError(f"unparseable CMS age edit: {rule!r}")
    return all(_OPS[op](a, b) for op, a, b in zip(ops, values, values[1:], strict=False))


def _split_terms(cols: list[str], i: int) -> tuple[str, ...]:
    return tuple(t for t in cols[i].split(" | ") if t) if len(cols) > i else ()


def _data_lines(path: Path) -> list[list[str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Point CHARTSIGHT_DATA_DIR at the repo's data/ directory "
            "(needed when chartsight is installed rather than run from a checkout)."
        )
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
        code, hcc, label, *edits = cols
        age_edit, sex_edit, mce_age = [*edits, "", "", ""][:3]
        grouped.setdefault(code, []).append(HCCMapping(hcc, label, age_edit, sex_edit, mce_age))
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
