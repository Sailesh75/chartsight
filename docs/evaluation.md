# Evaluation harness

Back to the [README](../README.md).

`evals/` measures the pipeline instead of just demoing it:

- `evals/fragments.py` — ~25 condition fragments, each with known-correct
  ICD-10-CM code(s), CMS-HCC V28 mapping, and expected documentation gap (if
  any) — including adversarial cases (negation, family history, resolved
  conditions) a correct coder must *not* code.
- `evals/generate.py` — composes synthetic notes from the fragments with PHI
  inserted at known offsets. Because every fragment's ground truth is known,
  gold labels fall out by construction — no manual span tagging.
- `evals/run.py` — scores the pipeline against the gold set: code precision/
  recall/F1 (exact and category-level), **HCC capture** (V28, the level that
  drives payment), **PHI recall** (the metric that actually matters for
  de-identification), documentation-gap detection recall and false-positive
  rate, confidence calibration, **retrieval recall@k** (is the gold code
  among the candidates the grounded pass is shown?), and **payment accuracy**
  (the RAF implied by predicted vs. gold codes, in RAF and dollars).

```bash
python -m evals.generate                        # writes evals/gold.jsonl
python -m evals.run --mode mock                  # score the offline sample engine
python -m evals.run --mode auto                  # score live Bedrock (grounded), if creds resolve
python -m evals.run --mode aws --no-grounding    # single-pass baseline for the A/B comparison
```

Writes `evals/report.md` and `evals/report.json`.
