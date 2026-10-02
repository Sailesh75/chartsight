# ChartSight

**Evidence-linked HCC coding.** An LLM pipeline that reads a clinical note,
extracts diagnoses as **ICD-10-CM codes** with the *exact evidence span* that
supports each one, removes **PHI**, and flags **risk-adjustment (HCC)
documentation gaps** powered by **Amazon Bedrock** (Anthropic Claude).

The output shape *code + confidence + the exact evidence span* plus a
documentation-gap review mirrors the risk-adjustment coding and
medical-record review work that payment-integrity vendors do for health plans.

## What it does

For each note, the app:

| Step | What it does |
| --- | --- |
| **De-identify** | Detects protected health information (names, MRNs, dates, phone numbers) and redacts it before it is shown. |
| **Code** | Extracts each condition, assigns the most specific ICD-10-CM code with a confidence, and returns the exact text span that supports it. |
| **Ground (RAG)** | Retrieves real candidate codes for each condition from the official FY2026 code set and has the model re-select its code from those candidates only. |
| **HCC tagging** | Tags every code with its CMS-HCC V28 category straight from the CMS crosswalk. The model never supplies HCCs. |
| **Risk score** | Computes the patient's V28 risk score (RAF) and shows what documenting each unspecified condition more specifically would be worth. |
| **Gap review** | Flags documentation-specificity gaps (e.g. unspecified heart failure, diabetes without a linked complication, CKD without a stage). |
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
pip install -e ".[api]"
chartsight serve                  # open http://localhost:8000
```

Without AWS credentials the app runs in **sample mode** immediately. To force
sample mode when credentials are present (no Bedrock cost), set `FORCE_MOCK=1`.

**The web UI** (`chartsight/web/`) is a single static page served by the API at
`/`. Each piece of coded evidence is highlighted in the note and linked to its
code card: hover one and the other lights up. A de-identified view swaps PHI
for type chips. The risk-score panel re-scores instantly through `/v1/raf` as
you change age, sex, segment or base rate, with no model call. It has light and
dark themes and plain HTML/CSS/JS with no build step. Every node is built with
`textContent`, so a pasted note can never inject markup.

## API, batch processing and Docker

The pipeline is a library. The web UI, the batch CLI and the eval harness are
all clients of it.

| Extra | Gives you |
| --- | --- |
| *(none)* | the pipeline and the `chartsight` batch CLI |
| `api` | the FastAPI service and the web UI it serves (`chartsight serve`) |

**HTTP API** (`chartsight/api.py`). Run it with `pip install -e ".[api]"` and
then `chartsight serve`. Interactive docs are at http://localhost:8000/docs.

| Endpoint | What it does |
| --- | --- |
| `GET /health` | Liveness, plus which engine a request would use |
| `POST /v1/analyze` | Full pipeline for one note: PHI, codes, grounding, HCCs, RAF, gaps. Optional `demographics`, `segment`, `base_rate_pmpm`, `mode`, `grounded` |
| `POST /v1/raf` | V28 risk score and documentation opportunities for a list of codes. Deterministic, no LLM call |
| `GET /v1/codes/search?q=…` | Retrieval over the official ICD-10-CM code set |
| `GET /v1/samples` | The synthetic demo notes the UI offers |

```bash
curl -s localhost:8000/v1/raf -H 'content-type: application/json' \
  -d '{"codes": ["E11.9", "I50.9", "N18.9"], "demographics": {"age": 72, "sex": "M"}}'
```

Set `CHARTSIGHT_API_KEY` to require an `X-API-Key` header on `/v1/*`;
`/health` stays open for load balancers. Notes are PHI, so they travel only in
POST bodies, which are never logged, and note length is capped
(`CHARTSIGHT_MAX_NOTE_CHARS`, default 20,000).

**Batch** (`chartsight/cli.py`), for a JSONL file of `{"id", "text"}` records
or a directory of `.txt` files:

```bash
chartsight analyze-batch notes.jsonl -o results.jsonl --mode aws --workers 4
```

Results are written as each note finishes. The run is **resumable**: re-running
skips ids already completed, including after a crash left a half-written line,
so an interrupted Bedrock run never re-bills finished notes. One failing note
is recorded as an error and doesn't stop the batch. The exit code is non-zero
if any note failed.

**Python client** (`chartsight/client.py`), for scripts and notebooks. It uses
only the standard library: `get_backend()` returns an HTTP client when
`CHARTSIGHT_API_URL` is set, else runs the pipeline in-process. A test runs
both, one against a real uvicorn server, and checks they return identical
results.

**Docker.** One slim, non-root image serves the API and the web UI, with a
healthcheck.

```bash
docker compose up --build        # UI http://localhost:8000, API docs /docs
```

Compose mounts `~/.aws` read-only into the container for live Bedrock (pick a
profile with `AWS_PROFILE`). Without credentials it runs on the sample engine.
CI builds the image and smoke-tests the container: health check, the UI page,
API-key enforcement, a real `/v1/analyze` call, and the non-root user.

When `CHARTSIGHT_API_KEY` is set, the UI asks for the key once and keeps it in
the browser's local storage.

### Enable live AWS (Amazon Bedrock)

1. Create an IAM user with programmatic access + the `AmazonBedrockFullAccess` policy.
2. Configure credentials in `~/.aws/credentials` (region `us-east-1`).
3. In the Bedrock console → **Model access**, submit the Anthropic use-case form
   and enable a Claude model.
4. Restart `chartsight serve`.

Override the model with `BEDROCK_MODEL_ID` (default
`us.anthropic.claude-haiku-4-5-20251001-v1:0`) and its display name with
`BEDROCK_MODEL_LABEL`.

**Cost:** Claude Haiku 4.5 on Bedrock costs a fraction of a cent per note (two
calls per note with grounding on). A new AWS account's free credits cover this
project many times over.

## Deploy to AWS (Lambda)

`infra/` holds the Terraform for a serverless deployment. The same image runs on
**AWS Lambda** through the Lambda Web Adapter, behind a public Function URL,
so an idle demo costs about nothing. The function's IAM role can call only the
one Claude model, so no AWS keys live in the app. GitHub Actions deploys on
every push to `master` with short-lived **OIDC** credentials, so no keys live in
GitHub either.

A public page that calls Claude needs a cost ceiling. `chartsight/quota.py`
caps live analyses per UTC day with an atomic DynamoDB counter
(`CHARTSIGHT_LIVE_DAILY_LIMIT`, default 100 in the Terraform). Past the cap,
the free sample engine answers and the UI says so. A concurrency limit and a
budget alert back it up. Step-by-step setup is in [`infra/README.md`](infra/README.md).

## Structured output & the code guardrail

The Bedrock call uses **tool-use**, not prose scraping: `chartsight/schema.py`
defines the extraction as a Pydantic model, its JSON schema is passed as the
tool's `input_schema`, and the model's `tool_use` response is validated with
`model_validate()` before anything downstream sees it — a malformed code or an
out-of-range confidence raises immediately rather than silently corrupting a
result.

That still doesn't stop a model from confidently returning a code that simply
doesn't exist. `chartsight/guardrail.py` checks every code against the real
FY2026 ICD-10-CM code set (`data/icd10cm_codes.tsv`, ~74.7k billable codes).
Anything not in that set is pulled into `rejected_codes` instead of being
trusted; the UI surfaces it in a "discarded by the guardrail" panel rather than
hiding it.

## RAG grounding on the official code set

The guardrail catches codes that don't exist. It can't catch a real code that is
wrong for the note, such as I50.9 when the note says "chronic systolic". Grounding
goes after that:

1. **Extract**: the first Bedrock call finds each condition mention, as before.
2. **Retrieve**: `chartsight/retrieval.py` looks up the top 8 real candidate
   codes for each mention, querying with the mention plus the model's own
   description.
3. **Select**: a second Bedrock call chooses one code per condition. The tool's
   schema gives each condition its own `enum` of candidates, so a code outside
   the list can't be expressed at all. The model can also answer `NONE`, for
   example when the mention turns out to be negated.
4. **Tag**: `chartsight/reference.py` attaches the V28 HCC by crosswalk lookup.

Two deliberate choices:

- **HCCs are never shown to the model.** Telling it which candidate risk-adjusts
  would push it toward the paying code. That's upcoding, which is exactly what
  payment integrity is meant to catch.
- **Retrieval is lexical (BM25), not embeddings.** ICD-10-CM descriptions are
  short and formulaic, so BM25 is strong, explainable, needs no API call or
  extra dependency, and runs in CI. The corpus is each code's official
  description, its tabular inclusion terms (e.g. I50.9 ← "Congestive heart
  failure NOS") and its CDC Alphabetic Index entries (e.g. N18.9 ←
  "Failure, failed renal chronic"). That's the same lookup a human coder does.
  Each phrase is indexed separately and a code scores as its best-matching
  phrase. Pooling all of a code's phrases into one document measurably hurt
  recall, because heavily indexed codes got buried by length normalization.
  A dense retriever can implement the same `Retriever` protocol and be
  compared on the harness's retrieval metric.

Pass `grounded=False` to `analyze()`, or `--no-grounding` to the eval runner,
for the single-pass baseline so the two can be A/B-scored.

**Measured impact** (Claude Haiku 4.5 on Bedrock, 60-note synthetic gold set,
one run each, 2026-09-23):

| Metric | Single pass | Grounded | Δ |
| --- | --- | --- | --- |
| Exact code F1 | 62.8% | **78.5%** | +15.7 pts |
| Exact code precision | 71.7% | **86.4%** | +14.7 pts |
| HCC capture F1 (V28) | 88.5% | **94.9%** | +6.4 pts |
| Accuracy of codes at ≥0.9 confidence | 75.2% | **93.1%** | better calibrated |

The grounded pass changed 45 first-pass codes. Typical fixes: a non-billable
category code (N18.3 → N18.32), a code that doesn't exist (I73.911 → I70.211),
and a wrong acuity (I50.23 → I50.22). Treat the numbers as directional: the
set is small and synthetic, and each mode ran once.

The payment impact is smaller than the code-level gain suggests. Scored with
the RAF calculator below, grounding moves notes with the exact gold RAF from
51/60 to 54/60, and the mean RAF error from $507 to $475 per member per year.
Most of its fixes stay inside the same HCC: I50.23 and I50.22 both map to
HCC 226, so the payment doesn't change.

### Reference data

All of these are built from official public releases by
`scripts/build_reference.py` and committed as plain text, so fiscal-year
updates show up as readable diffs:

| File | Source |
| --- | --- |
| `data/icd10cm_codes.tsv` | CDC/NCHS FY2026 ICD-10-CM code descriptions, tabular XML (inclusion terms) and Alphabetic Index XML |
| `data/hcc_v28.tsv` | ICD-10 → HCC mapping and HCC labels from the CMS-HCC V28 2026 midyear/final model software (the mapping the payment model itself uses), with CMS's age/sex edits verbatim |
| `data/cms_hcc_v28/*.csv` | The V28 model's own tables, copied verbatim from the same package: relative factors, hierarchies, diagnosis categories, interactions |

```bash
python scripts/build_reference.py --cache-dir .cache   # refresh for a new year (update URLs first)
```

Grounding HCCs on the crosswalk rather than on memory fixed real errors. The
first version of the eval fragment library had wrong V28 HCCs for 9 of 26
fragments. Examples: E11.9 and I50.9 *do* risk-adjust under V28 (HCC 38 and
HCC 226), and claudication-only PAD (I70.211) no longer does.
`tests/test_reference.py` now checks every fragment against the CMS crosswalk.

## Risk score (RAF) and what a gap is worth

`chartsight/raf.py` computes the CMS-HCC V28 risk score the way CMS's own
PY2026 model software does: ICD-10 → HCC with age/sex edits, the HCC 223
recode, hierarchies, demographic cell, originally-disabled and LTI-Medicaid
terms, interactions (Diabetes x HF, HF x Kidney, …) and the payment-HCC count
term, for all seven segments.

**It matches CMS exactly.** `tests/fixtures/cms_v28_reference_scores.json`
holds scores produced by running CMS's software on 14 synthetic beneficiaries.
They exercise hierarchies, interactions, the 10+ HCC count term, age-split
cancer mappings, originally-disabled and institutional cases, and the HCC 223
recode. `tests/test_raf.py` asserts all 98 scores (14 × 7 segments) are
identical.

The segment defaults by age, as CMS assigns it: community non-dual **aged** at
65+, and **disabled** below 65, where the patient is treated as entitled by
disability. The aged segments have no rates under 65, so scoring a 64-year-old
there would silently drop the demographic term.

The payment RAF applies the CY2026 Rate Announcement adjustments: normalization
factor **1.067** and the **5.9%** MA coding-pattern adjustment. Dollars use the
CY2026 national FFS USPCC (**$1,230.52** PMPM) as an illustrative base rate. A
plan's real revenue depends on its county benchmarks and bid, so the app lets
you set your own rate.

**Documentation opportunities.** For each unspecified code, every more
specific sibling code is scored for the whole patient, so hierarchies,
interactions and count terms all count. For example, staging CKD is worth more
when heart failure is also present, because of the HF x Kidney interaction.
Two rules keep the numbers honest:

- **Same condition only.** A candidate code only counts if its HCCs stay
  within the documented condition's HCC family, meaning the connected chain in
  CMS's hierarchy table. E11.9 → E11.52 adds HCC 263 (gangrene), which is a
  different diagnosis, not a more specific diabetes, so it isn't offered.
- **Ranges, not an "up to" headline.** Unspecified heart failure shows $0 for
  every acuity and a large figure only for end-stage heart failure. The UI
  presents the full range and what each outcome requires.

The calculator also corrected the project's own gap text. Under V28, **linking a diabetes complication is worth $0** (HCCs 36–38
share one coefficient), and **heart-failure acuity is worth $0** (HCCs 224–226
share one coefficient in every segment). Both are still worth documenting for
coding accuracy, and the gap text now says so. The real money is in
**staging CKD** and **documenting depression severity**.

Use this to prioritize provider queries. A code is justified only by what the
record supports, never by what it pays.

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

## Development

```bash
pip install -e ".[dev]"   # includes the api extra
ruff check .          # lint
ruff format .         # format
mypy                  # strict type check
pytest                # unit + eval-harness regression tests
```

CI (`.github/workflows/ci.yml`) runs all of the above plus the eval harness
against the sample engine on every push/PR, then builds and smoke-tests the
Docker image. On `master` it then deploys to Lambda (once configured; see
`infra/README.md`) and smoke-tests the live URL.

## Files

- `chartsight/web/` — the web UI (static HTML/CSS/JS, served by the API at `/`).
- `chartsight/api.py` — FastAPI service.
- `chartsight/cli.py` — `chartsight` command: resumable batch analysis, `serve`.
- `chartsight/client.py` — Python client: in-process or over HTTP.
- `chartsight/quota.py` — daily cap on live (billed) analyses for a public deployment.
- `chartsight/nlp.py` — Bedrock inference, redaction, the sample fallback engine.
- `chartsight/schema.py` — Pydantic extraction schema (Bedrock tool-use input).
- `chartsight/guardrail.py` — hallucinated-code guardrail.
- `chartsight/retrieval.py` — BM25 retrieval over the official code set (RAG grounding).
- `chartsight/reference.py` — loaders for the ICD-10-CM code set and the CMS-HCC V28 crosswalk.
- `chartsight/raf.py` — CMS-HCC V28 risk score and documentation-opportunity valuation.
- `data/notes.json` — synthetic clinical notes for the UI (no real PHI).
- `data/icd10cm_codes.tsv` — FY2026 ICD-10-CM codes, descriptions, inclusion terms, index entries.
- `data/hcc_v28.tsv` — CMS-HCC V28 ICD-10 → HCC crosswalk with labels and age/sex edits.
- `data/cms_hcc_v28/` — V28 model tables (relative factors, hierarchies, categories, interactions).
- `scripts/build_reference.py` — (re)generates the data files from the official CDC/CMS sources.
- `Dockerfile`, `docker-compose.yml` — one image for the API + UI; `docker compose up` runs it.
- `infra/` — Terraform for AWS Lambda + Function URL, ECR, DynamoDB quota, budget, GitHub OIDC deploy role.
- `evals/` — fragment library, gold-set generator, and scorer.
- `tests/` — pytest suite: pipeline, grounding, RAF (vs. CMS's software), API, CLI, eval harness.

## Synthetic data — no PHI

All notes — the UI samples and the eval gold set — are synthetically generated
or invented for the demo. None contain real patient data.
