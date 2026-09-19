"""Regression tests for the eval harness itself (not the model)."""

from __future__ import annotations

import os

os.environ["FORCE_MOCK"] = "1"

from evals.fragments import FRAGMENTS, validate_library
from evals.generate import generate
from evals.run import Report, run


def test_fragment_library_is_internally_consistent() -> None:
    assert validate_library() == []
    assert len(FRAGMENTS) >= 20


def test_generate_covers_every_fragment() -> None:
    notes = generate(seed=1, count=40)
    used = {fid for n in notes for fid in n["fragment_ids"]}
    assert used == {f.id for f in FRAGMENTS}


def test_generate_is_deterministic() -> None:
    assert generate(seed=3, count=10) == generate(seed=3, count=10)


def test_generated_spans_are_verbatim() -> None:
    for note in generate(seed=2, count=15):
        text = note["text"]
        for p in note["phi"]:
            assert text[p["begin"] : p["end"]] == p["text"]
        for c in note["conditions"]:
            assert c["begin"] < c["end"] <= len(text)


def test_scorer_runs_end_to_end_against_sample_engine(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    gold_path = tmp_path / "gold.jsonl"
    notes = generate(seed=5, count=10)
    with gold_path.open("w", encoding="utf-8") as fh:
        for note in notes:
            fh.write(json.dumps(note) + "\n")

    report = run(gold_path, mode="mock", limit=5)
    assert isinstance(report, Report)
    assert report.n_notes == 5
    assert report.phi.tp + report.phi.fn > 0
