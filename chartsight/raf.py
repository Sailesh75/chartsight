"""CMS-HCC V28 risk score (RAF) and the dollar value of documentation gaps.

A re-implementation of the continuing-enrollee scoring in CMS's own PY2026 V28
model software, using that software's tables verbatim (data/cms_hcc_v28/):

  1. ICD-10 → HCC, honoring CMS's age/sex edits (data/hcc_v28.tsv).
  2. HCC 223 only counts alongside another heart-failure HCC (a CMS recode).
  3. Hierarchies: a higher-severity HCC drops the lower ones it outranks.
  4. Demographic cell (age/sex), originally-disabled and LTI-Medicaid terms.
  5. Interactions (e.g. Diabetes x Heart Failure) and the payment-HCC count term.
  6. Sum the relative factors for the chosen segment.

tests/test_raf.py checks the result against scores produced by the CMS software
itself (tests/fixtures/cms_v28_reference_scores.json), for all seven segments.

The payment-year RAF then applies the CY2026 Rate Announcement adjustments, and
dollars use the national FFS USPCC as an illustrative base rate. A real plan's
revenue depends on its county benchmarks and bid, so set `base_rate_pmpm` to
the plan's own rate for real numbers.

Documentation opportunities answer the question a CDI team asks: "if the
record supports a more specific code, what is it worth?". For each unspecified
code, every more specific sibling code is scored in the context of the whole
patient, so hierarchies, interactions and count terms are all accounted for.
This is for prioritizing provider queries, not for choosing codes. A code is
only ever justified by the documentation.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from chartsight import reference

MODEL_DIR = Path(__file__).resolve().parent.parent / "data" / "cms_hcc_v28"

# CY2026 Rate Announcement (CMS, April 2025), https://www.cms.gov/files/document/2026-announcement.pdf
NORMALIZATION_FACTOR = 1.067  # "2024 CMS-HCC Part C Model: 1.067"
CODING_PATTERN_ADJUSTMENT = (
    0.059  # "statutory minimum MA coding pattern difference adjustment of 5.90 percent"
)
USPCC_PMPM = 1230.52  # Table I-2: current projected 2026 FFS USPCC, aged + disabled ($ per member per month)

SEGMENTS: dict[str, str] = {
    "COMMUNITY_NA": "Community, non-dual, aged",
    "COMMUNITY_ND": "Community, non-dual, disabled",
    "COMMUNITY_FBA": "Community, full-benefit dual, aged",
    "COMMUNITY_FBD": "Community, full-benefit dual, disabled",
    "COMMUNITY_PBA": "Community, partial-benefit dual, aged",
    "COMMUNITY_PBD": "Community, partial-benefit dual, disabled",
    "INSTITUTIONAL": "Institutional (long-term)",
}
DEFAULT_SEGMENT = "COMMUNITY_NA"

_AGE_BANDS = ((0, 34), (35, 44), (45, 54), (55, 59), (60, 64), (65, 69), (70, 74), (75, 79), (80, 84))
_AGE_BANDS_TAIL = ((85, 89), (90, 94))
_HF_HCCS = frozenset({"HCC221", "HCC222", "HCC224", "HCC225", "HCC226"})
_UNSPECIFIED = re.compile(r"\bunspecified\b|without complications?\b|\bNOS\b", re.IGNORECASE)


@dataclass(frozen=True)
class Demographics:
    age: int
    sex: int  # CMS coding: 1 = male, 2 = female
    orec: int = 0  # original reason for entitlement: 0 old age, 1 disability, 2 ESRD, 3 both
    ltimcaid: bool = False  # long-term-institutional Medicaid
    assumed: bool = False  # True when not documented in the note (defaults were used)

    @property
    def disabled(self) -> bool:
        return self.age < 65 and self.orec in (1, 2, 3)

    @property
    def originally_disabled(self) -> bool:
        return self.orec == 1 and not self.disabled

    def label(self) -> str:
        text = f"{self.age}-year-old {'male' if self.sex == 1 else 'female'}"
        return text + (" (assumed — not documented)" if self.assumed else "")


DEFAULT_DEMOGRAPHICS = Demographics(age=70, sex=2, assumed=True)


@dataclass(frozen=True)
class Term:
    variable: str  # CMS variable name, e.g. "HCC226", "DIABETES_HF_V28", "F70_74", "D5"
    label: str
    coefficient: float


@dataclass
class RafScore:
    segment: str
    demographics: Demographics
    demographic_terms: list[Term] = field(default_factory=list)
    hcc_terms: list[Term] = field(default_factory=list)
    interaction_terms: list[Term] = field(default_factory=list)
    count_term: Term | None = None
    dropped_by_hierarchy: dict[str, str] = field(default_factory=dict)  # dropped HCC -> HCC that outranked it

    @property
    def terms(self) -> list[Term]:
        count = [self.count_term] if self.count_term else []
        return self.demographic_terms + self.hcc_terms + self.interaction_terms + count

    @property
    def raw(self) -> float:
        """The model's risk score, as the CMS software reports it (before payment adjustments)."""
        return sum(t.coefficient for t in self.terms)

    @property
    def payment(self) -> float:
        """Payment-year RAF: raw / normalization factor, less the MA coding pattern adjustment."""
        return self.raw / NORMALIZATION_FACTOR * (1 - CODING_PATTERN_ADJUSTMENT)

    def annual_dollars(self, base_rate_pmpm: float = USPCC_PMPM) -> float:
        return self.payment * base_rate_pmpm * 12

    @property
    def hccs(self) -> list[str]:
        return [t.variable for t in self.hcc_terms]


# --------------------------------------------------------------------------- #
# Model tables
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Model:
    factors: dict[str, dict[str, float]]  # variable -> segment -> coefficient (missing = not in segment)
    labels: dict[str, str]
    hierarchy: dict[str, tuple[str, ...]]  # HCC -> HCCs it drops
    categories: dict[str, frozenset[str]]  # e.g. HF_V28 -> {HCC221, ...}
    interactions: tuple[tuple[str, str, str], ...]  # (name, var_1, var_2)


def _rows(name: str) -> list[dict[str, str]]:
    with (MODEL_DIR / name).open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


@lru_cache(maxsize=1)
def _model() -> _Model:
    factors: dict[str, dict[str, float]] = {}
    labels: dict[str, str] = {}
    for row in _rows("V28_CE_Relative_Factors.csv"):
        variable = row["Variable"].strip()
        if not variable:
            continue
        labels[variable] = " ".join(row["Label"].split())
        factors[variable] = {seg: float(row[seg]) for seg in SEGMENTS if (row.get(seg) or "").strip()}

    hierarchy = {
        row["HCC"].strip(): tuple(v.strip() for k, v in row.items() if k != "HCC" and v and v.strip())
        for row in _rows("V28_HCC_Hierarchies.csv")
    }
    categories = {
        row["diag_category"].strip(): frozenset(
            v.strip() for k, v in row.items() if k != "diag_category" and v and v.strip()
        )
        for row in _rows("V28_Diagnosis_Categories.csv")
    }
    interactions = tuple(
        (row["interaction"].strip(), row["var_1"].strip(), row["var_2"].strip())
        for row in _rows("V28_Interactions.csv")
        if row["interaction"].strip()
    )
    return _Model(factors, labels, hierarchy, categories, interactions)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def _age_sex_cell(demo: Demographics) -> str:
    sex = "M" if demo.sex == 1 else "F"
    for lo, hi in _AGE_BANDS + _AGE_BANDS_TAIL:
        if lo <= demo.age <= hi:
            return f"{sex}{lo}_{hi}"
    return f"{sex}95_GT"


def model_hccs(codes: list[str], demo: Demographics) -> set[str]:
    """Payment HCCs before hierarchies: crosswalk + age/sex edits + the HCC 223 recode."""
    model = _model()
    hccs = {m.hcc for code in codes for m in reference.hcc_for(code) if m.applies(demo.age, demo.sex)} & set(
        model.hierarchy
    )
    if "HCC223" in hccs and not hccs & _HF_HCCS:
        hccs.discard("HCC223")
    return hccs


def score(codes: list[str], demo: Demographics, segment: str = DEFAULT_SEGMENT) -> RafScore:
    if segment not in SEGMENTS:
        raise ValueError(f"unknown segment {segment!r}; expected one of {sorted(SEGMENTS)}")
    model = _model()

    def term(variable: str) -> Term | None:
        coefficient = model.factors.get(variable, {}).get(segment)
        return Term(variable, model.labels.get(variable, variable), coefficient) if coefficient else None

    result = RafScore(segment, demo)

    # Demographics
    demo_vars = [_age_sex_cell(demo)]
    if demo.originally_disabled:
        demo_vars += ["ORIGDIS", "OriginallyDisabled_Male" if demo.sex == 1 else "OriginallyDisabled_Female"]
    if demo.ltimcaid:
        demo_vars.append("LTIMCAID")
    result.demographic_terms = [t for v in demo_vars if (t := term(v))]

    # HCCs, after hierarchies (exclusions judged against the pre-hierarchy set, as CMS does)
    present = model_hccs(codes, demo)
    kept = set(present)
    for hcc in sorted(present):
        for lower in model.hierarchy.get(hcc, ()):
            if lower in present:
                kept.discard(lower)
                result.dropped_by_hierarchy.setdefault(lower, hcc)
    ordered = sorted(kept, key=lambda h: int(h[3:]))
    result.hcc_terms = [t for h in ordered if (t := term(h))]

    # Interactions: diagnosis categories, the disabled flag and raw HCC flags are the operands
    flags = {cat: bool(members & kept) for cat, members in model.categories.items()}
    flags["DISABL"] = demo.disabled
    flags.update(dict.fromkeys(kept, True))
    result.interaction_terms = [
        t for name, a, b in model.interactions if flags.get(a) and flags.get(b) and (t := term(name))
    ]

    # Payment-HCC count (every kept HCC counts, whether or not it has a coefficient in this segment)
    if kept:
        result.count_term = term(f"D{len(kept)}" if len(kept) < 10 else "D10P")
    return result


# --------------------------------------------------------------------------- #
# Documentation opportunities
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Outcome:
    hccs: tuple[str, ...]  # HCCs the specific code would map to (() = none)
    label: str
    example_codes: tuple[str, ...]
    delta_raf: float  # payment-RAF change for the whole patient
    delta_dollars: float  # annual


@dataclass(frozen=True)
class Opportunity:
    code: str
    description: str
    current_hccs: tuple[str, ...]
    outcomes: tuple[Outcome, ...]  # best first

    @property
    def max_delta_dollars(self) -> float:
        return max((o.delta_dollars for o in self.outcomes), default=0.0)


@lru_cache(maxsize=1)
def _families() -> dict[str, frozenset[str]]:
    """HCC -> its condition family: the connected component of CMS's hierarchy graph.

    E.g. HCC35–38 (diabetes), HCC221–227 (heart failure), HCC326–329 (CKD). A more specific
    code only counts as an opportunity if its new HCCs stay inside the documented condition's
    family: E11.9 -> E11.52 adds HCC263 (gangrene) — a different diagnosis, not more
    specificity about this one — so it's excluded rather than advertised as "worth $14k".
    """
    neighbours: dict[str, set[str]] = {}
    for hcc, lower in _model().hierarchy.items():
        neighbours.setdefault(hcc, set())
        for other in lower:
            neighbours[hcc].add(other)
            neighbours.setdefault(other, set()).add(hcc)
    family: dict[str, frozenset[str]] = {}
    for start in neighbours:
        if start in family:
            continue
        seen, stack = {start}, [start]
        while stack:
            for nxt in neighbours[stack.pop()] - seen:
                seen.add(nxt)
                stack.append(nxt)
        component = frozenset(seen)
        family.update(dict.fromkeys(component, component))
    return family


def _same_family(current: tuple[str, ...], candidate: tuple[str, ...]) -> bool:
    families = _families()
    new = [h for h in candidate if h not in current]
    if not new:
        return True
    anchor = families.get(current[0], frozenset(current)) if current else families.get(new[0], frozenset())
    return all(h in anchor for h in new)


@lru_cache(maxsize=1)
def _by_category() -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for code in reference.icd10_codes():
        grouped.setdefault(code[:3], []).append(code)
    return {cat: tuple(codes) for cat, codes in grouped.items()}


def is_unspecified(code: str) -> bool:
    desc = reference.description(code)
    return bool(desc and _UNSPECIFIED.search(desc))


def opportunities(
    codes: list[str],
    demo: Demographics,
    segment: str = DEFAULT_SEGMENT,
    base_rate_pmpm: float = USPCC_PMPM,
) -> list[Opportunity]:
    """Value of documenting each unspecified code more specifically, best opportunity first.

    Outcomes are the HCC groups that more specific codes in the same ICD-10-CM category map
    to, restricted to the documented condition's own HCC family (see `_families`).
    """
    codes = [reference.normalize(c) for c in codes]
    baseline = score(codes, demo, segment).payment
    labels = _model().labels
    found = []
    for code in dict.fromkeys(codes):
        if not is_unspecified(code):
            continue
        current = tuple(sorted(model_hccs([code], demo), key=lambda h: int(h[3:])))
        others = [c for c in codes if c != code]
        groups: dict[tuple[str, ...], list[tuple[str, float]]] = {}
        for sibling in _by_category().get(code[:3], ()):
            if sibling == code or is_unspecified(sibling):
                continue
            hccs = tuple(sorted(model_hccs([sibling], demo), key=lambda h: int(h[3:])))
            if not _same_family(current, hccs):
                continue
            delta = score([*others, sibling], demo, segment).payment - baseline
            groups.setdefault(hccs, []).append((sibling, delta))
        outcomes = []
        for hccs, members in groups.items():
            best_delta = max(d for _, d in members)
            examples = tuple(c for c, d in members if d == best_delta)[:3]
            label = " + ".join(f"{h} {labels.get(h, '')}".strip() for h in hccs) or "No HCC"
            outcomes.append(
                Outcome(
                    hccs, label, examples, round(best_delta, 4), round(best_delta * base_rate_pmpm * 12, 2)
                )
            )
        outcomes.sort(key=lambda o: -o.delta_raf)
        found.append(Opportunity(code, reference.description(code) or "", current, tuple(outcomes)))
    found.sort(key=lambda o: -o.max_delta_dollars)
    return found


# --------------------------------------------------------------------------- #
# Demographics from the note
# --------------------------------------------------------------------------- #
_AGE_RE = re.compile(r"\b(\d{1,3})[- ](?:year|yr)s?[- ]old\b", re.IGNORECASE)
_MALE_RE = re.compile(r"\b(male|man|gentleman)\b", re.IGNORECASE)
_FEMALE_RE = re.compile(r"\b(female|woman|lady)\b", re.IGNORECASE)


def parse_demographics(text: str) -> Demographics:
    """Age/sex as documented ("68-year-old male"), else DEFAULT_DEMOGRAPHICS (flagged assumed)."""
    age_match = _AGE_RE.search(text)
    female = _FEMALE_RE.search(text)
    male = _MALE_RE.search(text)
    if age_match is None or bool(female) == bool(male):
        return DEFAULT_DEMOGRAPHICS
    return Demographics(age=int(age_match.group(1)), sex=2 if female else 1)


# --------------------------------------------------------------------------- #
# Pipeline-facing summary
# --------------------------------------------------------------------------- #
def assess(
    codes: list[str],
    demo: Demographics,
    segment: str = DEFAULT_SEGMENT,
    base_rate_pmpm: float = USPCC_PMPM,
) -> dict[str, object]:
    """JSON-friendly RAF breakdown + documentation opportunities for one patient."""
    current = score(codes, demo, segment)
    return {
        "segment": segment,
        "segment_label": SEGMENTS[segment],
        "demographics": {"age": demo.age, "sex": demo.sex, "assumed": demo.assumed, "label": demo.label()},
        "raw": round(current.raw, 3),
        "payment": round(current.payment, 3),
        "annual_dollars": round(current.annual_dollars(base_rate_pmpm), 2),
        "base_rate_pmpm": base_rate_pmpm,
        "terms": [
            {"variable": t.variable, "label": t.label, "coefficient": t.coefficient} for t in current.terms
        ],
        "dropped_by_hierarchy": current.dropped_by_hierarchy,
        "opportunities": [
            {
                "code": o.code,
                "description": o.description,
                "current_hccs": list(o.current_hccs),
                "max_delta_dollars": o.max_delta_dollars,
                "outcomes": [
                    {
                        "hccs": list(x.hccs),
                        "label": x.label,
                        "example_codes": list(x.example_codes),
                        "delta_raf": x.delta_raf,
                        "delta_dollars": x.delta_dollars,
                    }
                    for x in o.outcomes
                ],
            }
            for o in opportunities(codes, demo, segment, base_rate_pmpm)
        ],
    }
