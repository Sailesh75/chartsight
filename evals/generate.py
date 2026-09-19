"""Compose synthetic clinical notes from the fragment library, with gold labels
that fall out by construction (see evals/fragments.py's module docstring).

Every non-adversarial fragment anchors at least one note (coverage pass), then
random combinations fill up to --count notes. Each note gets synthetic PHI
(name, MRN, DOB, phone) inserted at known offsets, 1-3 companion conditions
from other families, and occasionally an adversarial (negation/history)
fragment mixed in to test that the model doesn't over-code.

Usage:
    python -m evals.generate --count 60 --seed 7 --out evals/gold.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any

from evals.fragments import FRAGMENTS, Fragment

OUT_DEFAULT = Path(__file__).resolve().parent / "gold.jsonl"

# --------------------------------------------------------------------------- #
# Synthetic demographics — invented, no real PHI.
# --------------------------------------------------------------------------- #
FIRST_NAMES_M = ["John", "Robert", "Michael", "David", "James", "William", "Thomas", "Charles"]
FIRST_NAMES_F = ["Maria", "Linda", "Susan", "Patricia", "Barbara", "Nancy", "Karen", "Betty"]
LAST_NAMES = ["Miller", "Gomez", "King", "Chen", "Nguyen", "Patel", "Johnson", "Okafor", "Rossi", "Kowalski"]
MIDDLE_INITIALS = list("ABCDEFGHJKLMNPRSTW")

CHIEF_COMPLAINTS: dict[str, str] = {
    "dm": "follow-up of diabetes",
    "hf": "shortness of breath and lower extremity edema",
    "ckd": "routine nephrology follow-up",
    "esrd": "dialysis follow-up",
    "copd": "follow-up of chronic lung disease",
    "afib": "palpitations, anticoagulation follow-up",
    "mdd": "follow-up of mood symptoms",
    "pad": "leg pain with walking",
    "morbid": "weight-management follow-up",
    "htn": "routine hypertension follow-up",
    "hyperlipidemia": "routine follow-up, lipid panel review",
    "adv": "general follow-up",
}


def _family(fragment: Fragment) -> str:
    return fragment.id.split("-")[0]


CORE_FAMILIES: dict[str, list[Fragment]] = {}
ADV_ONLY: list[Fragment] = []
for _f in FRAGMENTS:
    if "adversarial" in _f.tags:
        ADV_ONLY.append(_f)
    else:
        CORE_FAMILIES.setdefault(_family(_f), []).append(_f)

# Families an adversarial fragment must never share a note with — it would
# contradict a positive diagnosis rendered from that family (e.g. "no evidence
# of CHF" alongside an actual heart-failure fragment).
ADV_CONFLICTS: dict[str, set[str]] = {
    "adv-negation-chf": {"hf"},
}


def _format_dob(rng: random.Random, age: int) -> str:
    year = 2026 - age
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    if rng.random() < 0.5:
        return f"{month:02d}/{day:02d}/{year}"
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{day:02d}-{months[month - 1]}-{year}"


def _format_phone(rng: random.Random) -> str:
    area, exch, line = rng.randint(200, 999), 555, rng.randint(1000, 9999)
    if rng.random() < 0.5:
        return f"({area}) {exch}-{line:04d}"
    return f"{area}-{exch}-{line:04d}"


def _demographics(rng: random.Random) -> dict[str, Any]:
    sex = rng.choice(["male", "female"])
    first = rng.choice(FIRST_NAMES_M if sex == "male" else FIRST_NAMES_F)
    name = f"{first} {rng.choice(MIDDLE_INITIALS)}. {rng.choice(LAST_NAMES)}"
    age = rng.randint(42, 88)
    return {
        "name": name,
        "mrn": str(rng.randint(1_000_000, 9_999_999)),
        "dob": _format_dob(rng, age),
        "phone": _format_phone(rng),
        "age": age,
        "sex": sex,
    }


def _build_fragment_set(rng: random.Random, anchor: Fragment) -> list[Fragment]:
    anchor_fam = _family(anchor)
    blocked = ADV_CONFLICTS.get(anchor.id, set()) | (
        {anchor_fam} if "adversarial" not in anchor.tags else set()
    )
    other_families = [fam for fam in CORE_FAMILIES if fam not in blocked]
    n_extra = rng.randint(2, 3) if "adversarial" in anchor.tags else rng.randint(1, 3)
    extra_families = rng.sample(other_families, k=min(n_extra, len(other_families)))
    chosen = [rng.choice(CORE_FAMILIES[fam]) for fam in extra_families]
    chosen.append(anchor)

    if "adversarial" not in anchor.tags and rng.random() < 0.3:
        chosen_families = {anchor_fam, *extra_families}
        candidates = [f for f in ADV_ONLY if not (ADV_CONFLICTS.get(f.id, set()) & chosen_families)]
        if candidates:
            chosen.append(rng.choice(candidates))

    rng.shuffle(chosen)
    return chosen


def _render_note(rng: random.Random, fragments: list[Fragment], note_num: int) -> dict[str, Any]:
    demo = _demographics(rng)
    chief_complaint = CHIEF_COMPLAINTS[_family(fragments[0])]

    header = f"Patient: {demo['name']}   MRN: {demo['mrn']}   DOB: {demo['dob']}   Phone: {demo['phone']}\n\n"
    cc_line = f"Chief complaint: {chief_complaint}.\n\n"
    sex_word = "male" if demo["sex"] == "male" else "female"
    intro = f"Assessment: {demo['age']}-year-old {sex_word} with "
    body = "; ".join(f.text for f in fragments)
    text = header + cc_line + intro + body + "."

    phi = []
    for value, phi_type in (
        (demo["name"], "NAME"),
        (demo["mrn"], "ID"),
        (demo["dob"], "DATE"),
        (demo["phone"], "PHONE"),
    ):
        idx = text.index(value)
        phi.append({"text": value, "type": phi_type, "begin": idx, "end": idx + len(value)})

    conditions: list[dict[str, Any]] = []
    cursor = len(header) + len(cc_line) + len(intro)
    for frag in fragments:
        idx = text.index(frag.text, cursor)
        end = idx + len(frag.text)
        cursor = end
        for code in frag.codes:
            conditions.append(
                {
                    "code": code.code,
                    "description": code.description,
                    "begin": idx,
                    "end": end,
                    "fragment_id": frag.id,
                }
            )

    expected_gap_keys = sorted({f.gap for f in fragments if f.gap})

    return {
        "id": f"g{note_num:03d}",
        "text": text,
        "phi": phi,
        "conditions": conditions,
        "expected_gap_keys": expected_gap_keys,
        "fragment_ids": [f.id for f in fragments],
    }


def generate(seed: int = 7, count: int = 60) -> list[dict[str, Any]]:
    """Generate `count` notes, or `len(FRAGMENTS)` if that's larger — the coverage
    pass (every fragment anchors at least one note) always runs in full first."""
    rng = random.Random(seed)
    all_fragments = [f for frags in CORE_FAMILIES.values() for f in frags] + ADV_ONLY
    rng.shuffle(all_fragments)

    # Coverage pass: every fragment anchors at least one note.
    notes = [
        _render_note(rng, _build_fragment_set(rng, anchor), i + 1) for i, anchor in enumerate(all_fragments)
    ]

    # Fill pass: random combinations up to `count`.
    while len(notes) < count:
        anchor = rng.choice(all_fragments)
        notes.append(_render_note(rng, _build_fragment_set(rng, anchor), len(notes) + 1))

    return notes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=OUT_DEFAULT)
    args = parser.parse_args()

    notes = generate(seed=args.seed, count=args.count)
    with args.out.open("w", encoding="utf-8") as fh:
        for note in notes:
            fh.write(json.dumps(note) + "\n")

    fragment_ids_used = {fid for n in notes for fid in n["fragment_ids"]}
    missing = {f.id for f in FRAGMENTS} - fragment_ids_used
    gap_notes = sum(1 for n in notes if n["expected_gap_keys"])
    adv_notes = sum(1 for n in notes if any("adv-" in fid for fid in n["fragment_ids"]))

    print(f"wrote {len(notes)} notes -> {args.out}")
    print(
        f"  fragment coverage: {len(fragment_ids_used)}/{len(FRAGMENTS)}"
        + (f"  MISSING: {sorted(missing)}" if missing else "")
    )
    print(f"  notes with an expected gap: {gap_notes}")
    print(f"  notes with an adversarial fragment: {adv_notes}")


if __name__ == "__main__":
    main()
