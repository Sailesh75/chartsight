"""Score the pipeline against the gold set: code P/R/F1, HCC capture, PHI recall,
documentation-gap detection, confidence calibration, and retrieval recall.

Usage:
    python -m evals.run --mode mock                    # local rule engine (no AWS needed)
    python -m evals.run --mode auto                     # Bedrock (grounded) if creds resolve, else mock
    python -m evals.run --mode aws --no-grounding       # single-pass Bedrock, for the A/B comparison
    python -m evals.run --mode auto --limit 10          # quick iteration on a subset
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chartsight import reference
from chartsight.nlp import analyze
from chartsight.retrieval import default_retriever
from evals.fragments import GAP_RULES

GOLD_DEFAULT = Path(__file__).resolve().parent / "gold.jsonl"
CALIBRATION_BUCKETS = [(0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
RETRIEVAL_KS = (1, 5, 10)


def _overlaps(a_begin: int, a_end: int, b_begin: int, b_end: int) -> bool:
    return a_begin < b_end and b_begin < a_end


@dataclass
class Counters:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class Report:
    n_notes: int = 0
    code_exact: Counters = field(default_factory=Counters)
    code_category: Counters = field(default_factory=Counters)
    hcc: Counters = field(default_factory=Counters)
    phi: Counters = field(default_factory=Counters)
    # Retrieval ceiling: is the gold code among the top-k candidates the grounded pass would see?
    # "mention" queries with the evidence text alone; "oracle" adds the gold description, i.e. a
    # perfectly-described first pass — an upper bound on what retrieval can hand the model.
    retrieval_total: int = 0
    retrieval_hits_mention: dict[int, int] = field(default_factory=lambda: dict.fromkeys(RETRIEVAL_KS, 0))
    retrieval_hits_oracle: dict[int, int] = field(default_factory=lambda: dict.fromkeys(RETRIEVAL_KS, 0))
    grounded_notes: int = 0  # notes where grounding actually ran (Bedrock only; sample engine is rule-based)
    grounding_changed: int = 0  # grounded pass picked a different code than the first pass
    ungrounded: int = 0  # grounded pass answered NONE
    gap_expected: int = 0
    gap_addressed: int = 0
    gap_false_positive_notes: int = 0
    no_gap_notes: int = 0
    calibration: dict[tuple[float, float], list[int]] = field(
        default_factory=lambda: {b: [0, 0] for b in CALIBRATION_BUCKETS}
    )
    engine_seen: set[str] = field(default_factory=set)
    per_note: list[dict[str, Any]] = field(default_factory=list)

    @property
    def gap_recall(self) -> float:
        return self.gap_addressed / self.gap_expected if self.gap_expected else 0.0

    @property
    def gap_false_positive_rate(self) -> float:
        return self.gap_false_positive_notes / self.no_gap_notes if self.no_gap_notes else 0.0


def _category(code: str) -> str:
    return code.split(".")[0]


def _score_conditions(
    gold_conditions: list[dict[str, Any]], pred_conditions: list[dict[str, Any]], report: Report
) -> None:
    matched_pred: set[int] = set()
    for g in gold_conditions:
        for i, p in enumerate(pred_conditions):
            if i in matched_pred:
                continue
            same_code = p.get("code") == g["code"]
            # Bedrock extraction defaults an unresolved span to (0, 0); fall back to code-only match then.
            span_ok = _overlaps(g["begin"], g["end"], p.get("begin", 0), p.get("end", 0)) or (
                p.get("begin") == 0 and p.get("end") == 0
            )
            if same_code and span_ok:
                matched_pred.add(i)
                break
        else:
            report.code_exact.fn += 1
            continue
        report.code_exact.tp += 1

    report.code_exact.fp += len(pred_conditions) - len(matched_pred)

    gold_categories = {_category(c["code"]) for c in gold_conditions}
    pred_categories = {_category(c["code"]) for c in pred_conditions}
    report.code_category.tp += len(gold_categories & pred_categories)
    report.code_category.fn += len(gold_categories - pred_categories)
    report.code_category.fp += len(pred_categories - gold_categories)

    for p in pred_conditions:
        conf = float(p.get("confidence", 0.0))
        is_correct = any(
            p.get("code") == g["code"]
            and (
                _overlaps(g["begin"], g["end"], p.get("begin", 0), p.get("end", 0))
                or (p.get("begin") == 0 == p.get("end"))
            )
            for g in gold_conditions
        )
        for lo, hi in CALIBRATION_BUCKETS:
            if lo <= conf < hi:
                bucket = report.calibration[(lo, hi)]
                bucket[0] += 1
                bucket[1] += int(is_correct)
                break


def _hccs(codes: list[str]) -> set[str]:
    return {m.hcc for code in codes for m in reference.hcc_for(code)}


def _score_hcc(
    gold_conditions: list[dict[str, Any]], pred_conditions: list[dict[str, Any]], report: Report
) -> None:
    """HCC capture at note level — what actually drives payment (I50.22 vs I50.42: same HCC)."""
    gold = _hccs([c["code"] for c in gold_conditions])
    pred = _hccs([c["code"] for c in pred_conditions])
    report.hcc.tp += len(gold & pred)
    report.hcc.fn += len(gold - pred)
    report.hcc.fp += len(pred - gold)


def _score_retrieval(text: str, gold_conditions: list[dict[str, Any]], report: Report) -> None:
    retriever = default_retriever()
    k_max = max(RETRIEVAL_KS)
    for g in gold_conditions:
        mention = text[g["begin"] : g["end"]]
        report.retrieval_total += 1
        for query, hits in (
            (mention, report.retrieval_hits_mention),
            (f"{mention} {g['description']}", report.retrieval_hits_oracle),
        ):
            ranked = [c.code for c in retriever.search(query, k=k_max)]
            for k in RETRIEVAL_KS:
                hits[k] += g["code"] in ranked[:k]


def _score_phi(gold_phi: list[dict[str, Any]], pred_phi: list[dict[str, Any]], report: Report) -> None:
    matched_pred: set[int] = set()
    for g in gold_phi:
        for i, p in enumerate(pred_phi):
            if i in matched_pred:
                continue
            if _overlaps(g["begin"], g["end"], p["begin"], p["end"]):
                matched_pred.add(i)
                break
        else:
            report.phi.fn += 1
            continue
        report.phi.tp += 1
    report.phi.fp += len(pred_phi) - len(matched_pred)


def _gap_rule_hits(gap_text: str, rule: dict[str, object]) -> bool:
    text = gap_text.lower()
    condition_terms = rule["condition_terms"]
    fix_terms = rule["fix_terms"]
    assert isinstance(condition_terms, list) and isinstance(fix_terms, list)
    return any(t in text for t in condition_terms) and any(t in text for t in fix_terms)


def _score_gaps(expected_keys: list[str], pred_gaps: list[str], report: Report) -> None:
    pred_gaps_lower = [g.lower() for g in pred_gaps]
    for key in expected_keys:
        rule = GAP_RULES[key]
        report.gap_expected += 1
        if any(_gap_rule_hits(text, rule) for text in pred_gaps_lower):
            report.gap_addressed += 1

    if not expected_keys:
        report.no_gap_notes += 1
        raised = any(_gap_rule_hits(text, rule) for text in pred_gaps_lower for rule in GAP_RULES.values())
        if raised:
            report.gap_false_positive_notes += 1


def run(gold_path: Path, mode: str, limit: int | None, grounded: bool = True) -> Report:
    report = Report()
    lines = gold_path.read_text(encoding="utf-8").splitlines()
    if limit:
        lines = lines[:limit]

    for line in lines:
        gold = json.loads(line)
        pred = analyze(gold["text"], mode=mode, grounded=grounded)
        report.n_notes += 1
        report.engine_seen.add(pred["engine"])

        _score_conditions(gold["conditions"], pred["conditions"], report)
        _score_hcc(gold["conditions"], pred["conditions"], report)
        _score_phi(gold["phi"], pred["phi"], report)
        _score_gaps(gold["expected_gap_keys"], pred["insights"]["gaps"], report)
        _score_retrieval(gold["text"], gold["conditions"], report)
        report.grounded_notes += bool(pred.get("grounded"))
        report.grounding_changed += sum(
            1 for c in pred["conditions"] if c.get("first_pass_code", c["code"]) != c["code"]
        )
        report.ungrounded += len(pred.get("ungrounded", []))
        report.per_note.append(
            {
                "id": gold["id"],
                "engine": pred["engine"],
                "gold_codes": sorted({c["code"] for c in gold["conditions"]}),
                "pred_codes": sorted({c["code"] for c in pred["conditions"]}),
                "first_pass_codes": sorted({c.get("first_pass_code", c["code"]) for c in pred["conditions"]}),
                "ungrounded_codes": sorted({c["code"] for c in pred.get("ungrounded", [])}),
            }
        )

    return report


def _recall_row(label: str, hits: dict[int, int], total: int) -> str:
    cells = " | ".join(f"{hits[k] / total:.1%}" if total else "n/a" for k in RETRIEVAL_KS)
    return f"| {label} | {cells} |"


def render_markdown(report: Report, mode: str, grounded: bool = True) -> str:
    if not grounded:
        grounding = "off"
    elif report.grounded_notes:
        grounding = f"on ({report.grounded_notes}/{report.n_notes} notes)"
    else:
        grounding = "not applied (rule-based sample engine)"
    lines = [
        "# ChartSight eval report",
        "",
        f"- Notes scored: **{report.n_notes}**",
        f"- Mode: `{mode}`  ·  Grounding: {grounding}  ·  "
        f"Engine(s) observed: {', '.join(sorted(report.engine_seen)) or 'n/a'}",
        "",
        "## Code extraction",
        "",
        "| Metric | Precision | Recall | F1 |",
        "|---|---|---|---|",
        f"| Exact code match | {report.code_exact.precision:.1%} | {report.code_exact.recall:.1%} | {report.code_exact.f1:.1%} |",
        f"| Category match (e.g. E11.*) | {report.code_category.precision:.1%} | {report.code_category.recall:.1%} | {report.code_category.f1:.1%} |",
        f"| HCC capture (CMS-HCC V28) | {report.hcc.precision:.1%} | {report.hcc.recall:.1%} | {report.hcc.f1:.1%} |",
        "",
        "HCCs are derived from codes via the official CMS V28 crosswalk for both gold and prediction.",
        "",
        "## Retrieval (grounding candidates)",
        "",
        "Is the gold code among the top-k official candidates? *Mention* queries with the evidence text alone; "
        "*oracle* adds the gold description (upper bound for a perfectly-described first pass).",
        "",
        "| Query | " + " | ".join(f"R@{k}" for k in RETRIEVAL_KS) + " |",
        "|---|" + "---|" * len(RETRIEVAL_KS),
        _recall_row("Mention", report.retrieval_hits_mention, report.retrieval_total),
        _recall_row(
            "Mention + gold description (oracle)", report.retrieval_hits_oracle, report.retrieval_total
        ),
        "",
        f"- Grounded pass changed the first-pass code: **{report.grounding_changed}** conditions; "
        f"answered NONE (dropped): **{report.ungrounded}**",
        "",
        "## PHI redaction",
        "",
        f"- **Recall (the metric that matters for de-id): {report.phi.recall:.1%}** "
        f"({report.phi.tp}/{report.phi.tp + report.phi.fn} entities found)",
        f"- Precision: {report.phi.precision:.1%} ({report.phi.fp} false-positive redactions)",
        "",
        "## Documentation-gap detection",
        "",
        f"- Recall: **{report.gap_recall:.1%}** ({report.gap_addressed}/{report.gap_expected} expected gaps flagged)",
        f"- False-positive rate: {report.gap_false_positive_rate:.1%} "
        f"({report.gap_false_positive_notes}/{report.no_gap_notes} fully-documented notes got a spurious gap)",
        "",
        "## Confidence calibration",
        "",
        "A well-calibrated model's accuracy in a bucket should roughly equal the bucket's confidence range.",
        "",
        "| Confidence | n | Accuracy |",
        "|---|---|---|",
    ]
    for (lo, hi), (n, correct) in report.calibration.items():
        acc = f"{correct / n:.1%}" if n else "n/a"
        hi_label = "1.0" if hi > 1 else f"{hi:.1f}"
        lines.append(f"| {lo:.1f}–{hi_label} | {n} | {acc} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=GOLD_DEFAULT)
    parser.add_argument("--mode", choices=["auto", "aws", "mock"], default="auto")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--no-grounding", action="store_true", help="single-pass Bedrock (no retrieval) for A/B comparison"
    )
    parser.add_argument("--out-md", type=Path, default=Path(__file__).resolve().parent / "report.md")
    parser.add_argument("--out-json", type=Path, default=Path(__file__).resolve().parent / "report.json")
    args = parser.parse_args()

    grounded = not args.no_grounding
    report = run(args.gold, args.mode, args.limit, grounded=grounded)
    md = render_markdown(report, args.mode, grounded=grounded)
    print(md)

    args.out_md.write_text(md, encoding="utf-8")
    args.out_json.write_text(
        json.dumps(
            {
                "n_notes": report.n_notes,
                "mode": args.mode,
                "grounded": grounded,
                "grounded_notes": report.grounded_notes,
                "engines": sorted(report.engine_seen),
                "code_exact": vars(report.code_exact),
                "code_category": vars(report.code_category),
                "hcc": vars(report.hcc),
                "retrieval": {
                    "total": report.retrieval_total,
                    "recall_mention": {
                        k: v / report.retrieval_total for k, v in report.retrieval_hits_mention.items()
                    }
                    if report.retrieval_total
                    else {},
                    "recall_oracle": {
                        k: v / report.retrieval_total for k, v in report.retrieval_hits_oracle.items()
                    }
                    if report.retrieval_total
                    else {},
                },
                "grounding_changed": report.grounding_changed,
                "ungrounded": report.ungrounded,
                "phi": vars(report.phi),
                "gap_recall": report.gap_recall,
                "gap_false_positive_rate": report.gap_false_positive_rate,
                "per_note": report.per_note,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {args.out_md} and {args.out_json}")


if __name__ == "__main__":
    main()
