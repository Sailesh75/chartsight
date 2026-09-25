"""Tests for the `chartsight analyze-batch` command."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

os.environ["FORCE_MOCK"] = "1"

import pytest

from chartsight import cli
from chartsight.nlp import analyze as real_analyze

NOTES = [
    {"id": "a", "text": "Assessment: 70-year-old male with essential hypertension."},
    {"id": "b", "text": "Assessment: 81-year-old female with chronic kidney disease."},
    {"id": "c", "text": "Assessment: congestive heart failure."},
]


def _write_jsonl(path: Path, records: list[dict[str, str]]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_batch_jsonl(tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    summary = cli.analyze_batch(_write_jsonl(tmp_path / "in.jsonl", NOTES), out, mode="mock", workers=2)
    records = {r["id"]: r for r in _read(out)}
    assert set(records) == {"a", "b", "c"} and all(r["ok"] for r in records.values())
    assert summary["processed"] == 3 and summary["failed"] == 0
    # 'b' (unstaged CKD) and 'c' (unspecified HF, via end-stage); hypertension has none.
    assert summary["notes_with_documentation_opportunities"] == 2


def test_batch_is_resumable(tmp_path: Path) -> None:
    source = _write_jsonl(tmp_path / "in.jsonl", NOTES)
    out = tmp_path / "out.jsonl"
    cli.analyze_batch(_write_jsonl(tmp_path / "first.jsonl", NOTES[:2]), out, mode="mock")
    with out.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "c", "ok": tr')  # torn line from a crash mid-write

    summary = cli.analyze_batch(source, out, mode="mock")

    assert summary["skipped_already_done"] == 2 and summary["processed"] == 1
    assert sorted(r["id"] for r in _read_ok(out)) == ["a", "b", "c"]


def _read_ok(path: Path) -> list[dict[str, Any]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("ok"):
            records.append(record)
    return records


def test_batch_directory_of_txt(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "p1.txt").write_text(NOTES[0]["text"], encoding="utf-8")
    (notes / "p2.txt").write_text(NOTES[1]["text"], encoding="utf-8")
    out = tmp_path / "out.jsonl"
    cli.analyze_batch(notes, out, mode="mock")
    assert sorted(r["id"] for r in _read(out)) == ["p1", "p2"]


def test_one_failing_note_does_not_sink_the_batch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def flaky(text: str, **kwargs: object) -> dict[str, object]:
        if "hypertension" in text:
            raise RuntimeError("boom")
        return real_analyze(text, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "analyze", flaky)
    out = tmp_path / "out.jsonl"
    summary = cli.analyze_batch(_write_jsonl(tmp_path / "in.jsonl", NOTES), out, mode="mock")
    assert summary["failed"] == 1 and summary["processed"] == 2
    failed = [r for r in _read(out) if not r["ok"]]
    assert failed == [{"id": "a", "ok": False, "error": "RuntimeError: boom"}]


def test_main_exit_codes(tmp_path: Path) -> None:
    source = _write_jsonl(tmp_path / "in.jsonl", NOTES[:1])
    assert cli.main(["analyze-batch", str(source), "-o", str(tmp_path / "o.jsonl"), "--mode", "mock"]) == 0


def test_missing_text_field_is_a_clear_error(tmp_path: Path) -> None:
    source = _write_jsonl(tmp_path / "in.jsonl", [{"id": "x", "body": "no text key"}])
    with pytest.raises(SystemExit, match="missing 'text'"):
        cli.analyze_batch(source, tmp_path / "o.jsonl", mode="mock")
