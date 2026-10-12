# How the coding pipeline works

Back to the [README](../README.md).

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
one run each, 2026-10-04):

| Metric | Single pass | Grounded | Δ |
| --- | --- | --- | --- |
| Exact code F1 | 63.9% | **80.8%** | +16.9 pts |
| Exact code precision | 72.9% | **90.2%** | +17.3 pts |
| HCC capture F1 (V28) | 88.9% | **95.7%** | +6.8 pts |
| Accuracy of codes at ≥0.9 confidence | 83.5% | **96.7%** | better calibrated |
| Notes with the exact gold payment RAF | 51/60 | **53/60** | |
| Mean RAF error, per member per year | $719 | **$562** | |

The grounded pass changed 39 first-pass codes. Typical fixes: documented
chronicity the first pass ignored (I48.91 → I48.20, *chronic* atrial
fibrillation, 12 times), a code that doesn't exist (I73.921 → I70.211), a
non-billable category code replaced by the documented stage (N18.3 → N18.4),
and a wrong acuity (I50.23 → I50.22). Treat the numbers as directional: the
set is small and synthetic, and each mode ran once.

The payment impact is smaller than the code-level gain suggests, because most
fixes stay inside the same HCC: I50.23 and I50.22 both map to HCC 226, so the
payment doesn't change.

**Known limitation:** gap detection over-flags. Both modes find nearly every
real documentation gap (91–96% recall), but about half of fully documented
notes also get a spurious one. Tightening that is the next improvement.

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
