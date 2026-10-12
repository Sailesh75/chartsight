# Running ChartSight

Back to the [README](../README.md).

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


To deploy it publicly, see [`infra/README.md`](../infra/README.md).
