# Development

Back to the [README](../README.md).

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
[`infra/README.md`](../infra/README.md)) and smoke-tests the live URL.


## Project layout

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
