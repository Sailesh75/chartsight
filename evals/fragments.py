"""Condition fragment library — the clinical ground truth for the eval gold set.

Each :class:`Fragment` is a short, realistic phrasing of a diagnosis (as it would
appear in a note's assessment/plan) paired with the ICD-10-CM code(s) a correct
coder must return, the CMS-HCC V28 category those codes map into, and the
documentation gap (if any) that the phrasing should trigger.

`evals/generate.py` composes these fragments into synthetic notes; because every
fragment's ground truth is known, the gold labels (codes, evidence spans,
expected gaps) fall out by construction — no manual span tagging.

=============================================================================
REVIEWER CHECKLIST  (this file is the part that needs a human — you)
=============================================================================
For every fragment below, confirm against the 2026 ICD-10-CM tabular list and
the CMS-HCC V28 model:
  1. `text` is phrasing a clinician would actually write.
  2. `codes` are the *most specific* codes that phrasing supports — no more.
  3. `hcc_v28` is the correct HCC category (or () when the code does not
     risk-adjust, e.g. I10, E78.5).
  4. `gap` is set when — and only when — the phrasing is genuinely underspecified
     in a way that loses HCC capture, and the matching GAP_RULES entry describes
     the right fix.
  5. Negation / history / resolved fragments (tags containing "adversarial")
     have `codes=()` — a correct coder must NOT code them.
Mark anything you are unsure about with a `# REVIEW:` comment.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExpectedCode:
    code: str
    description: str


@dataclass(frozen=True)
class Fragment:
    id: str
    text: str
    codes: tuple[ExpectedCode, ...]
    hcc_v28: tuple[str, ...] = ()
    gap: str | None = None
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


def _c(code: str, description: str) -> ExpectedCode:
    return ExpectedCode(code, description)


# --------------------------------------------------------------------------- #
# Gap rules — canonical documentation gaps a fragment can trigger.
# `keywords`: lowercase tokens; a predicted gap string "addresses" the rule if it
# mentions the condition term AND at least one fix term. Used by evals/run.py.
# --------------------------------------------------------------------------- #
GAP_RULES: dict[str, dict[str, object]] = {
    "hf-unspecified": {
        "label": "Heart failure coded unspecified (I50.9) — does not risk-adjust.",
        "fix": "Document systolic/diastolic (or combined) and acute/chronic to reach I50.2x–I50.4x.",
        "condition_terms": ["heart failure", "chf", "i50"],
        "fix_terms": [
            "systolic",
            "diastolic",
            "acute",
            "chronic",
            "type",
            "acuity",
            "i50.2",
            "i50.3",
            "i50.4",
        ],
    },
    "dm-no-complication": {
        "label": "Diabetes coded without a linked complication (E11.9).",
        "fix": "Link the manifestation (neuropathy, CKD, retinopathy) to move to an HCC-eligible E11.x code.",
        "condition_terms": ["diabetes", "diabetic", "e11"],
        "fix_terms": [
            "complication",
            "manifestation",
            "neuropathy",
            "ckd",
            "kidney",
            "retinopathy",
            "linked",
            "e11.2",
            "e11.3",
            "e11.4",
        ],
    },
    "ckd-no-stage": {
        "label": "Chronic kidney disease documented without a stage (N18.9).",
        "fix": "Document the stage (N18.1–N18.6); stage 3+ restores HCC capture.",
        "condition_terms": ["chronic kidney disease", "ckd", "n18"],
        "fix_terms": ["stage", "n18.3", "n18.4", "n18.5", "n18.6", "gfr", "egfr"],
    },
    "mdd-unspecified-severity": {
        "label": "Major depressive disorder without single/recurrent + severity (F32.9).",
        "fix": "Document episode (single vs recurrent) and severity to reach an HCC-eligible F32.x/F33.x.",
        "condition_terms": ["depression", "depressive", "mdd", "f32", "f33"],
        "fix_terms": ["recurrent", "single", "severity", "moderate", "severe", "episode", "f33"],
    },
}


# --------------------------------------------------------------------------- #
# Fragment library
# --------------------------------------------------------------------------- #
FRAGMENTS: tuple[Fragment, ...] = (
    # ---- Diabetes mellitus ------------------------------------------------- #
    Fragment(
        id="dm-unspec",
        text="type 2 diabetes mellitus, poorly controlled",
        codes=(_c("E11.9", "Type 2 diabetes mellitus without complications"),),
        hcc_v28=(),  # REVIEW: E11.9 does not map to an HCC in V28 (uncomplicated DM was removed).
        gap="dm-no-complication",
        note="Uncomplicated T2DM. Gap: no linked manifestation, so no HCC.",
        tags=("common", "gap"),
    ),
    Fragment(
        id="dm-polyneuropathy",
        text="type 2 diabetes mellitus with diabetic peripheral neuropathy",
        codes=(_c("E11.42", "Type 2 diabetes mellitus with diabetic polyneuropathy"),),
        hcc_v28=("HCC38",),  # REVIEW: diabetes with chronic complications
        note="Manifestation explicitly linked -> single combination code, HCC-eligible.",
        tags=("common", "multi-manifestation"),
    ),
    Fragment(
        id="dm-ckd",
        text="type 2 diabetes mellitus with diabetic chronic kidney disease, stage 3a",
        codes=(
            _c("E11.22", "Type 2 diabetes mellitus with diabetic chronic kidney disease"),
            _c("N18.31", "Chronic kidney disease, stage 3a"),
        ),
        hcc_v28=("HCC38", "HCC329"),  # REVIEW: DM w/ CKD + CKD stage 3
        note="Two codes required: the E11.22 combination code AND the N18 stage code.",
        tags=("multi-code",),
    ),
    Fragment(
        id="dm-hyperglycemia",
        text="type 2 diabetes with hyperglycemia, A1c 9.4%",
        codes=(_c("E11.65", "Type 2 diabetes mellitus with hyperglycemia"),),
        hcc_v28=("HCC38",),  # REVIEW
        note="Hyperglycemia is a documented complication -> E11.65.",
        tags=("common",),
    ),
    # ---- Heart failure --------------------------------------------------- #
    Fragment(
        id="hf-unspec",
        text="history of congestive heart failure",
        codes=(_c("I50.9", "Heart failure, unspecified"),),
        hcc_v28=(),  # REVIEW: I50.9 does not risk-adjust
        gap="hf-unspecified",
        note="No type, no acuity. Classic specificity gap.",
        tags=("common", "gap"),
    ),
    Fragment(
        id="hf-chronic-systolic",
        text="chronic systolic congestive heart failure, NYHA class III",
        codes=(_c("I50.22", "Chronic systolic (congestive) heart failure"),),
        hcc_v28=("HCC226",),  # REVIEW: heart failure HCC
        note="Type + acuity documented -> HCC-eligible.",
        tags=("common",),
    ),
    Fragment(
        id="hf-acute-on-chronic-diastolic",
        text="acute on chronic diastolic heart failure",
        codes=(_c("I50.33", "Acute on chronic diastolic (congestive) heart failure"),),
        hcc_v28=("HCC226",),  # REVIEW
        note="Both acuity states documented -> I50.33.",
        tags=(),
    ),
    Fragment(
        id="hf-combined",
        text="chronic combined systolic and diastolic heart failure",
        codes=(
            _c("I50.42", "Chronic combined systolic (congestive) and diastolic (congestive) heart failure"),
        ),
        hcc_v28=("HCC226",),  # REVIEW
        tags=(),
    ),
    # ---- Chronic kidney disease --------------------------------------------- #
    Fragment(
        id="ckd-unspec",
        text="chronic kidney disease",
        codes=(_c("N18.9", "Chronic kidney disease, unspecified"),),
        hcc_v28=(),  # REVIEW: N18.9 does not risk-adjust
        gap="ckd-no-stage",
        tags=("common", "gap"),
    ),
    Fragment(
        id="ckd-3b",
        text="stage 3b chronic kidney disease",
        codes=(_c("N18.32", "Chronic kidney disease, stage 3b"),),
        hcc_v28=("HCC329",),  # REVIEW: CKD stage 3
        tags=("common",),
    ),
    Fragment(
        id="ckd-4",
        text="chronic kidney disease stage 4, eGFR 22",
        codes=(_c("N18.4", "Chronic kidney disease, stage 4 (severe)"),),
        hcc_v28=("HCC328",),  # REVIEW: CKD stage 4
        tags=(),
    ),
    Fragment(
        id="esrd",
        text="end-stage renal disease on hemodialysis",
        codes=(
            _c("N18.6", "End stage renal disease"),
            _c("Z99.2", "Dependence on renal dialysis"),
        ),
        hcc_v28=("HCC326",),  # REVIEW: ESRD/dialysis
        note="ESRD + dialysis dependence both coded.",
        tags=("multi-code",),
    ),
    # ---- Respiratory ----------------------------------------------------- #
    Fragment(
        id="copd-stable",
        text="COPD on tiotropium, stable, no recent exacerbations",
        codes=(_c("J44.9", "Chronic obstructive pulmonary disease, unspecified"),),
        hcc_v28=("HCC280",),  # REVIEW: COPD HCC — confirm J44.9 is included in V28
        note="Stable COPD with no exacerbation -> J44.9 is correct and complete. No gap.",
        tags=("common",),
    ),
    Fragment(
        id="copd-exacerbation",
        text="chronic obstructive pulmonary disease with acute exacerbation",
        codes=(_c("J44.1", "Chronic obstructive pulmonary disease with (acute) exacerbation"),),
        hcc_v28=("HCC280",),  # REVIEW
        tags=("common",),
    ),
    # ---- Arrhythmia ---------------------------------------------------------- #
    Fragment(
        id="afib-unspec",
        text="atrial fibrillation on apixaban",
        codes=(_c("I48.91", "Unspecified atrial fibrillation"),),
        hcc_v28=("HCC238",),  # REVIEW: arrhythmia HCC — confirm I48.91 included in V28
        note="V28 narrowed the arrhythmia HCC; confirm I48.91 still maps.",
        tags=("common",),
    ),
    Fragment(
        id="afib-chronic",
        text="chronic atrial fibrillation",
        codes=(_c("I48.20", "Chronic atrial fibrillation, unspecified"),),
        hcc_v28=("HCC238",),  # REVIEW
        tags=(),
    ),
    # ---- Behavioral health ------------------------------------------------- #
    Fragment(
        id="mdd-unspec",
        text="depression, on sertraline",
        codes=(_c("F32.A", "Depression, unspecified"),),  # REVIEW: F32.A (2024+) vs F32.9
        hcc_v28=(),
        gap="mdd-unspecified-severity",
        note="Bare 'depression' -> F32.A. Gap: no episode/severity, no HCC.",
        tags=("common", "gap"),
    ),
    Fragment(
        id="mdd-recurrent-severe",
        text="major depressive disorder, recurrent, severe without psychotic features",
        codes=(_c("F33.2", "Major depressive disorder, recurrent severe without psychotic features"),),
        hcc_v28=("HCC155",),  # REVIEW: depression HCC
        tags=(),
    ),
    # ---- Vascular / metabolic ------------------------------------------- #
    Fragment(
        id="pad-claudication",
        text="peripheral arterial disease of the native arteries of the right leg with intermittent claudication",
        codes=(
            _c(
                "I70.211",
                "Atherosclerosis of native arteries of extremities with intermittent claudication, right leg",
            ),
        ),
        hcc_v28=("HCC263",),  # REVIEW: vascular disease HCC
        tags=("multi-manifestation",),
    ),
    Fragment(
        id="morbid-obesity-bmi",
        text="morbid obesity, BMI 42.1",
        codes=(
            _c("E66.01", "Morbid (severe) obesity due to excess calories"),
            _c("Z68.41", "Body mass index [BMI] 40.0-44.9, adult"),
        ),
        hcc_v28=("HCC48",),  # REVIEW: obesity HCC — confirm V28 and that BMI Z-code is required
        note="E66.01 + BMI Z-code. BMI code alone does not risk-adjust.",
        tags=("multi-code",),
    ),
    # ---- Not HCC (must still be coded correctly) ------------------------ #
    Fragment(
        id="htn-essential",
        text="essential hypertension, stable on lisinopril",
        codes=(_c("I10", "Essential (primary) hypertension"),),
        hcc_v28=(),
        note="Correct code, but I10 does not risk-adjust. No gap — nothing to fix.",
        tags=("common", "non-hcc"),
    ),
    Fragment(
        id="hyperlipidemia",
        text="hyperlipidemia",
        codes=(_c("E78.5", "Hyperlipidemia, unspecified"),),
        hcc_v28=(),
        note="Non-HCC. Do not flag as a gap.",
        tags=("common", "non-hcc"),
    ),
    # ---- Adversarial: negation / history / resolved -------------------- #
    Fragment(
        id="adv-negation-chf",
        text="no clinical evidence of congestive heart failure",
        codes=(),
        note="Explicit negation -> must NOT code heart failure.",
        tags=("adversarial", "negation"),
    ),
    Fragment(
        id="adv-family-history-dm",
        text="family history of type 2 diabetes mellitus in mother",
        codes=(),  # REVIEW: some coders would assign Z83.3 — decide if the gold set expects it
        note="Family history, not a patient condition. Gold set treats as no code.",
        tags=("adversarial", "history"),
    ),
    Fragment(
        id="adv-resolved-cancer",
        text="history of colon cancer, resected 2019, no evidence of recurrence, off chemotherapy",
        codes=(_c("Z85.038", "Personal history of other malignant neoplasm of large intestine"),),
        hcc_v28=(),
        note="Resolved/historical malignancy -> Z85, NOT an active C-code. Common overcoding trap.",
        tags=("adversarial", "history"),
    ),
    Fragment(
        id="adv-rule-out",
        text="shortness of breath, rule out pulmonary embolism",
        codes=(),
        note="'rule out' -> do not code the suspected condition in the outpatient setting.",
        tags=("adversarial", "uncertain"),
    ),
)


def by_id(fragment_id: str) -> Fragment:
    for fragment in FRAGMENTS:
        if fragment.id == fragment_id:
            return fragment
    raise KeyError(fragment_id)


def validate_library() -> list[str]:
    """Cheap internal consistency checks (not clinical correctness)."""
    problems: list[str] = []
    seen: set[str] = set()
    for fragment in FRAGMENTS:
        if fragment.id in seen:
            problems.append(f"duplicate fragment id: {fragment.id}")
        seen.add(fragment.id)
        if fragment.gap is not None and fragment.gap not in GAP_RULES:
            problems.append(f"{fragment.id}: gap '{fragment.gap}' not in GAP_RULES")
        if fragment.gap is not None and not fragment.codes:
            problems.append(f"{fragment.id}: has a gap but no codes")
        if "adversarial" in fragment.tags and fragment.gap is not None:
            problems.append(f"{fragment.id}: adversarial fragment should not carry a gap")
    return problems


if __name__ == "__main__":
    issues = validate_library()
    print(f"{len(FRAGMENTS)} fragments, {len(GAP_RULES)} gap rules")
    if issues:
        print("\nconsistency problems:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("internal consistency: OK")
