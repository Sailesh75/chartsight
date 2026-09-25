"""Tests for CMS-HCC V28 risk scoring and documentation-opportunity valuation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chartsight import raf
from chartsight.raf import Demographics, opportunities, parse_demographics, score
from chartsight.reference import HCCMapping, _age_rule_holds

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "cms_v28_reference_scores.json").read_text(encoding="utf-8")
)
MALE_72 = Demographics(age=72, sex=1)


@pytest.mark.parametrize("bene", FIXTURE["beneficiaries"], ids=lambda b: f"bene{b['id']}")
def test_scores_match_official_cms_software(bene: dict[str, Any]) -> None:
    """Every segment's raw score equals what CMS's own V28 software produced for the same inputs."""
    demo = Demographics(bene["age"], bene["sex"], bene["orec"], bool(bene["ltimcaid"]))
    assert sorted(score(bene["icd10"], demo).hccs) == bene["hccs"]
    for segment, expected in bene["scores"].items():
        assert round(score(bene["icd10"], demo, segment).raw, 3) == expected, segment


def test_hierarchy_drops_lower_hcc_and_records_why() -> None:
    result = score(["I50.23", "I50.9"], MALE_72)  # acute on chronic (224) outranks unspecified (226)
    assert result.hccs == ["HCC224"]
    assert result.dropped_by_hierarchy == {"HCC226": "HCC224"}


def test_hcc223_needs_another_heart_failure_hcc() -> None:
    assert score(["Z95.811"], MALE_72).hccs == []  # heart assist device alone: recoded away
    with_hf = score(["Z95.811", "I50.9"], MALE_72)
    assert with_hf.hccs == ["HCC223"]  # kept, and it outranks HCC226
    assert with_hf.dropped_by_hierarchy == {"HCC226": "HCC223"}


def test_interaction_terms() -> None:
    variables = {t.variable for t in score(["E11.9", "I50.9"], MALE_72).terms}
    assert "DIABETES_HF_V28" in variables


def test_age_edit_selects_the_right_cancer_hcc() -> None:
    assert score(["C50.011"], Demographics(45, 2)).hccs == ["HCC22"]
    assert score(["C50.011"], Demographics(70, 2)).hccs == ["HCC23"]


def test_payment_raf_applies_cy2026_adjustments() -> None:
    result = score(["I50.9"], MALE_72)
    assert result.payment == pytest.approx(result.raw / 1.067 * (1 - 0.059))
    assert result.annual_dollars(1000) == pytest.approx(result.payment * 12_000)


def test_unknown_segment_rejected() -> None:
    with pytest.raises(ValueError):
        score(["I10"], MALE_72, "COMMUNITY_XX")


# --------------------------------------------------------------------------- #
# Documentation opportunities
# --------------------------------------------------------------------------- #
def _by_code(codes: list[str], demo: Demographics = MALE_72) -> dict[str, raf.Opportunity]:
    return {o.code: o for o in opportunities(codes, demo)}


def test_unstaged_ckd_is_worth_documenting() -> None:
    opp = _by_code(["N18.9"])["N18.9"]
    by_hcc = {o.hccs: o for o in opp.outcomes}
    assert by_hcc[("HCC328",)].delta_raf > 0
    assert by_hcc[()].delta_raf == 0  # stage 1-2 doesn't risk-adjust
    assert opp.outcomes[0].delta_raf == max(o.delta_raf for o in opp.outcomes)  # best first


def test_diabetes_complication_is_worth_nothing_under_v28() -> None:
    """HCCs 36-38 share a coefficient: linking a complication changes the code, not the RAF."""
    opp = _by_code(["E11.9"])["E11.9"]
    assert opp.max_delta_dollars == 0


def test_opportunities_stay_inside_the_documented_condition_family() -> None:
    # E11.52 adds HCC263 (gangrene) — a different diagnosis, never offered as "more specific diabetes".
    opp = _by_code(["E11.9"])["E11.9"]
    assert all("HCC263" not in o.hccs for o in opp.outcomes)
    assert all(code != "E11.52" for o in opp.outcomes for code in o.example_codes)


def test_heart_failure_acuity_does_not_change_raf() -> None:
    outcomes = {o.hccs: o for o in _by_code(["I50.9"])["I50.9"].outcomes}
    assert outcomes[("HCC224",)].delta_raf == outcomes[("HCC225",)].delta_raf == 0


def test_opportunity_value_includes_interactions() -> None:
    alone = {o.hccs: o for o in _by_code(["N18.9"])["N18.9"].outcomes}
    with_hf = {o.hccs: o for o in _by_code(["N18.9", "I50.22"])["N18.9"].outcomes}
    # Staging CKD with heart failure present also triggers the HF x Kidney interaction.
    assert with_hf[("HCC328",)].delta_raf > alone[("HCC328",)].delta_raf


def test_specific_codes_are_not_opportunities() -> None:
    assert _by_code(["I50.22", "N18.32"]) == {}


# --------------------------------------------------------------------------- #
# Demographics + CMS edits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "age", "sex", "assumed"),
    [
        ("Assessment: 68-year-old male with CHF.", 68, 1, False),
        ("72 year old woman, follow-up.", 72, 2, False),
        ("Assessment: CHF.", 70, 2, True),  # nothing documented -> defaults, flagged
    ],
)
def test_parse_demographics(text: str, age: int, sex: int, assumed: bool) -> None:
    demo = parse_demographics(text)
    assert (demo.age, demo.sex, demo.assumed) == (age, sex, assumed)


@pytest.mark.parametrize(
    ("rule", "age", "holds"),
    [("age < 50", 49, True), ("age < 50", 50, False), ("0 <= age <= 17", 17, True), ("age = 0", 1, False)],
)
def test_age_rules(rule: str, age: int, holds: bool) -> None:
    assert _age_rule_holds(rule, age) is holds


def test_unparseable_age_rule_raises() -> None:
    with pytest.raises(ValueError):
        _age_rule_holds("age ~ 3", 5)


def test_sex_edit() -> None:
    mapping = HCCMapping("HCC1", "x", sex_edit="2")
    assert mapping.applies(40, 2) and not mapping.applies(40, 1)


def test_analyze_includes_raf() -> None:
    from chartsight.nlp import analyze

    result = analyze(
        "Assessment: 74-year-old female with congestive heart failure and chronic kidney disease.",
        mode="mock",
    )
    risk = result["raf"]
    assert risk["demographics"]["age"] == 74 and not risk["demographics"]["assumed"]
    assert risk["payment"] > 0
    assert {o["code"] for o in risk["opportunities"]} == {"I50.9", "N18.9"}
