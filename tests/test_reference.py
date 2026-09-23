"""Tests for the official reference data (ICD-10-CM code set + CMS-HCC V28 crosswalk)."""

from __future__ import annotations

import pytest

from chartsight import reference
from evals.fragments import FRAGMENTS


def test_code_set_loaded_with_descriptions_and_synonyms() -> None:
    codes = reference.icd10_codes()
    assert len(codes) > 70_000
    assert codes["I50.22"].description == "Chronic systolic (congestive) heart failure"
    assert "Congestive heart failure NOS" in codes["I50.9"].terms  # tabular inclusion term
    assert any("renal" in t.lower() and "chronic" in t.lower() for t in codes["N18.9"].index_terms)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("E11.9", "E11.9"), ("e119", "E11.9"), (" I10 ", "I10"), ("N1832", "N18.32"), ("F32.A", "F32.A")],
)
def test_normalize(raw: str, expected: str) -> None:
    assert reference.normalize(raw) == expected


@pytest.mark.parametrize(
    ("code", "hccs"),
    [
        ("E11.22", {"HCC37"}),  # diabetes with chronic complications
        ("E11.9", {"HCC38"}),  # uncomplicated diabetes still risk-adjusts in V28
        ("I50.9", {"HCC226"}),  # unspecified heart failure still risk-adjusts in V28
        ("I50.33", {"HCC224"}),  # acute on chronic heart failure
        ("N18.32", {"HCC328"}),
        ("N18.9", set()),  # unstaged CKD does not risk-adjust
        ("I10", set()),
        ("E78.5", set()),
    ],
)
def test_hcc_crosswalk_known_mappings(code: str, hccs: set[str]) -> None:
    assert {m.hcc for m in reference.hcc_for(code)} == hccs


def test_hcc_labels_present() -> None:
    (mapping,) = reference.hcc_for("I50.22")
    assert mapping.label == "Heart Failure, Except End Stage and Acute"


def test_age_conditional_mappings_are_kept() -> None:
    # C50.011 maps to HCC 22 under age 50 and HCC 23 at 50+ — both edits are preserved.
    conditions = {m.hcc: m.condition for m in reference.hcc_for("C50.011")}
    assert set(conditions) == {"HCC22", "HCC23"}
    assert all("age" in c for c in conditions.values())


def test_crosswalk_codes_are_real_codes() -> None:
    # The CMS file spans FY2025+FY2026; a handful of FY2025 codes were expanded for FY2026.
    missing = [code for code in reference.hcc_crosswalk() if not reference.is_valid(code)]
    assert len(missing) < 10, missing


def test_fragment_hcc_labels_match_official_crosswalk() -> None:
    """The eval gold set's HCC ground truth must equal the CMS V28 mapping, not anyone's memory."""
    for fragment in FRAGMENTS:
        official = {m.hcc for code in fragment.codes for m in reference.hcc_for(code.code)}
        assert set(fragment.hcc_v28) == official, (
            f"{fragment.id}: hcc_v28={fragment.hcc_v28} but CMS V28 says {sorted(official)}"
        )
