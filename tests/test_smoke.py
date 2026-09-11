"""End-to-end smoke test of the sample (offline) engine."""

from __future__ import annotations

import os

os.environ["FORCE_MOCK"] = "1"

from chartsight.nlp import analyze, load_notes


def test_notes_load() -> None:
    notes = load_notes()
    assert notes, "no sample notes found"
    assert all({"id", "title", "text"} <= n.keys() for n in notes)


def test_sample_engine_pipeline() -> None:
    notes = load_notes()
    # n2 is the "gaps present" note.
    note = next(n for n in notes if n["id"] == "n2")
    result = analyze(note["text"], mode="mock")

    assert result["engine"].startswith("Local sample")
    assert result["phi"], "expected PHI to be detected"
    assert result["conditions"], "expected conditions to be coded"
    assert result["insights"]["gaps"], "expected at least one gap statement"

    # Every condition carries an evidence span that is a verbatim substring.
    for c in result["conditions"]:
        assert c["text"] and c["text"] in note["text"]
        assert c["code"]

    # PHI is actually removed from the redacted text.
    for p in result["phi"]:
        assert p["text"] not in result["redacted"]
