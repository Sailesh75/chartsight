"""`chartsight` command line.

    chartsight analyze-batch notes.jsonl -o results.jsonl [--mode aws] [--workers 4]
    chartsight analyze-batch notes_dir/ -o results.jsonl      # one note per *.txt file
    chartsight serve [--host 0.0.0.0] [--port 8000]

analyze-batch input: JSONL with {"id": ..., "text": ...} per line (other fields are
ignored), or a directory of .txt files (the id is the file stem). Output: one JSON
result per line, {"id", "ok", "result" | "error"}, plus a run summary on stderr.

Resumable: ids already in the output file with ok=true are skipped, so a re-run after
a crash or throttling picks up where it left off without re-billing finished notes.
Results are written as each note finishes, not at the end.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from chartsight import raf
from chartsight.nlp import analyze


def _read_notes(source: Path) -> Iterator[tuple[str, str]]:
    if source.is_dir():
        for path in sorted(source.glob("*.txt")):
            yield path.stem, path.read_text(encoding="utf-8")
        return
    with source.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if "text" not in record:
                raise SystemExit(f"{source}:{line_no}: missing 'text'")
            yield str(record.get("id", line_no)), record["text"]


def _done_ids(output: Path) -> set[str]:
    if not output.exists():
        return set()
    done = set()
    with output.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from an interrupted run
            if record.get("ok"):
                done.add(str(record["id"]))
    return done


def analyze_batch(
    source: Path,
    output: Path,
    mode: str = "auto",
    grounded: bool = True,
    workers: int = 4,
    segment: str = raf.DEFAULT_SEGMENT,
    base_rate_pmpm: float = raf.USPCC_PMPM,
) -> dict[str, Any]:
    done = _done_ids(output)
    todo = [(note_id, text) for note_id, text in _read_notes(source) if note_id not in done]
    summary: dict[str, Any] = {
        "skipped_already_done": len(done),
        "processed": 0,
        "failed": 0,
        "fallbacks": 0,  # notes where Bedrock was requested but the sample engine answered
        "total_payment_raf": 0.0,
        # A count, not a dollar sum: summing each gap's best case would advertise rare
        # outcomes (end-stage HF, ESRD) as if they were expected revenue.
        "notes_with_documentation_opportunities": 0,
    }
    lock = threading.Lock()
    started = time.monotonic()

    def work(note_id: str, text: str) -> dict[str, Any]:
        try:
            result = analyze(
                text, mode=mode, grounded=grounded, segment=segment, base_rate_pmpm=base_rate_pmpm
            )
            return {"id": note_id, "ok": True, "result": result}
        except Exception as exc:  # one bad note must not sink the batch
            return {"id": note_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    output.parent.mkdir(parents=True, exist_ok=True)
    # A crash can leave a torn last line with no newline; appending straight after it would
    # glue the next record onto the fragment and lose it too.
    if output.exists() and output.stat().st_size and not output.read_bytes().endswith(b"\n"):
        with output.open("a", encoding="utf-8") as out:
            out.write("\n")
    with output.open("a", encoding="utf-8") as out, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, note_id, text) for note_id, text in todo]
        for future in as_completed(futures):
            record = future.result()
            with lock:
                out.write(json.dumps(record) + "\n")
                out.flush()
                if record["ok"]:
                    result = record["result"]
                    summary["processed"] += 1
                    summary["fallbacks"] += bool(result.get("fallback_reason"))
                    summary["total_payment_raf"] += result["raf"]["payment"]
                    summary["notes_with_documentation_opportunities"] += any(
                        o["max_delta_dollars"] > 0 for o in result["raf"]["opportunities"]
                    )
                else:
                    summary["failed"] += 1
                    print(f"  ! {record['id']}: {record['error']}", file=sys.stderr)

    summary["seconds"] = round(time.monotonic() - started, 1)
    summary["total_payment_raf"] = round(summary["total_payment_raf"], 3)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chartsight", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    batch = sub.add_parser("analyze-batch", help="analyze many notes (JSONL file or directory of .txt)")
    batch.add_argument("source", type=Path)
    batch.add_argument("-o", "--output", type=Path, required=True)
    batch.add_argument("--mode", choices=["auto", "aws", "mock"], default="auto")
    batch.add_argument("--no-grounding", action="store_true")
    batch.add_argument("--workers", type=int, default=4, help="concurrent notes (mind Bedrock quotas)")
    batch.add_argument("--segment", choices=list(raf.SEGMENTS), default=raf.DEFAULT_SEGMENT)
    batch.add_argument("--base-rate", type=float, default=raf.USPCC_PMPM, help="$ PMPM for RAF dollars")

    serve = sub.add_parser("serve", help="run the HTTP API (needs the 'api' extra)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    if args.command == "analyze-batch":
        summary = analyze_batch(
            args.source,
            args.output,
            mode=args.mode,
            grounded=not args.no_grounding,
            workers=args.workers,
            segment=args.segment,
            base_rate_pmpm=args.base_rate,
        )
        print(json.dumps(summary, indent=2), file=sys.stderr)
        return 1 if summary["failed"] else 0

    import uvicorn

    uvicorn.run("chartsight.api:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
