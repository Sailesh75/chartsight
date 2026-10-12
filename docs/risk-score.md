# Risk score (RAF) and what a gap is worth

Back to the [README](../README.md).

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
