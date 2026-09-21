# ChartSight

**Evidence-linked HCC coding.** An LLM pipeline that reads a clinical note,
extracts diagnoses as **ICD-10-CM codes** with the *exact evidence span* that
supports each one, removes **PHI**, and flags **risk-adjustment (HCC)
documentation gaps** — powered by **Amazon Bedrock** (Anthropic Claude).

The output shape — *code + confidence + the exact evidence span* plus a
documentation-gap review — mirrors the risk-adjustment coding and
medical-record review work that payment-integrity vendors do for health plans.

## What it does

In a single Amazon Bedrock call, the app:

| Step | What it does |
| --- | --- |
| **De-identify** | Detects protected health information (names, MRNs, dates, phone numbers) and redacts it before it is shown. |
| **Code** | Extracts each condition, assigns the most specific ICD-10-CM code with a confidence, and returns the exact text span that supports it. |
| **Gap review** | Flags documentation-specificity gaps that reduce HCC capture (e.g. unspecified heart failure, diabetes without a linked complication, CKD without a stage). |
| **Guardrail** | Checks every returned code against the real ICD-10-CM code set; anything hallucinated is discarded before it reaches the UI. |

## Why Amazon Bedrock

Amazon Bedrock runs foundation models (here, Anthropic Claude) as a fully
managed AWS service — no servers, no ML training. Using a foundation model for
clinical coding shows how a general model can be steered into a specialist
payment-integrity task with a well-designed prompt, entirely inside AWS.

## Runs anywhere — graceful fallback

- **AWS live** — real Amazon Bedrock inference.
- **Sample mode** — a local keyword/rule engine when no AWS credentials are
  present, so the UI runs with zero setup and zero cost. The app never shows a
  setup error.

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -e .
streamlit run app.py
```

Without AWS credentials the app runs in **sample mode** immediately.

### Enable live AWS (Amazon Bedrock)

1. Create an IAM user with programmatic access + the `AmazonBedrockFullAccess` policy.
2. Configure credentials in `~/.aws/credentials` (region `us-east-1`).
3. In the Bedrock console → **Model access**, submit the Anthropic use-case form
   and enable a Claude model.
4. Relaunch `streamlit run app.py`.

Override the model with `BEDROCK_MODEL_ID` (default
`us.anthropic.claude-haiku-4-5-20251001-v1:0`) and its display name with
`BEDROCK_MODEL_LABEL`.

**Cost:** Claude Haiku 4.5 on Bedrock costs a fraction of a cent per note. A new
AWS account's free credits cover this project many times over.

## Structured output & the code guardrail

The Bedrock call uses **tool-use**, not prose scraping: `chartsight/schema.py`
defines the extraction as a Pydantic model, its JSON schema is passed as the
tool's `input_schema`, and the model's `tool_use` response is validated with
`model_validate()` before anything downstream sees it — a malformed code or an
out-of-range confidence raises immediately rather than silently corrupting a
result.

That still doesn't stop a model from confidently returning a code that simply
doesn't exist. `chartsight/guardrail.py` checks every code against the real
FY2026 ICD-10-CM code set (`data/icd10cm_valid_codes.txt`, ~74.7k codes,
sourced from the CDC/CMS public release — see `scripts/build_icd10_reference.py`
for provenance and how to refresh it for a new fiscal year). Anything not in
that set is pulled into `rejected_codes` instead of being trusted; the UI
surfaces it in a "discarded by the guardrail" panel rather than hiding it.

## Evaluation harness

`evals/` measures the pipeline instead of just demoing it:

- `evals/fragments.py` — ~25 condition fragments, each with known-correct
  ICD-10-CM code(s), CMS-HCC V28 mapping, and expected documentation gap (if
  any) — including adversarial cases (negation, family history, resolved
  conditions) a correct coder must *not* code.
- `evals/generate.py` — composes synthetic notes from the fragments with PHI
  inserted at known offsets. Because every fragment's ground truth is known,
  gold labels fall out by construction — no manual span tagging.
- `evals/run.py` — scores the pipeline against the gold set: code precision/
  recall/F1 (exact and category-level), **PHI recall** (the metric that
  actually matters for de-identification), documentation-gap detection recall
  and false-positive rate, and confidence calibration.

```bash
python -m evals.generate                 # writes evals/gold.jsonl
python -m evals.run --mode mock           # score the offline sample engine
python -m evals.run --mode auto           # score live Bedrock, if creds resolve
```

Writes `evals/report.md` and `evals/report.json`.

## Development

```bash
pip install -e ".[dev]"
ruff check .          # lint
ruff format .         # format
mypy                  # strict type check
pytest                # unit + eval-harness regression tests
```

CI (`.github/workflows/ci.yml`) runs all of the above plus the eval harness
against the sample engine on every push/PR.

## Files

- `app.py` — Streamlit UI.
- `chartsight/nlp.py` — Bedrock inference, redaction, the sample fallback engine.
- `chartsight/schema.py` — Pydantic extraction schema (Bedrock tool-use input).
- `chartsight/guardrail.py` — hallucinated-code guardrail.
- `data/notes.json` — synthetic clinical notes for the UI (no real PHI).
- `data/icd10cm_valid_codes.txt` — real FY2026 ICD-10-CM code set (guardrail reference data).
- `scripts/build_icd10_reference.py` — (re)generates the file above from the official CDC source.
- `evals/` — fragment library, gold-set generator, and scorer.
- `tests/` — pytest suite (pipeline smoke test + eval-harness regression tests).

## Synthetic data — no PHI

All notes — the UI samples and the eval gold set — are synthetically generated
or invented for the demo. None contain real patient data.
