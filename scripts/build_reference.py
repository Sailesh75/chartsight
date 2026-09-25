"""Build the reference data the pipeline grounds itself on, from official public releases.

Outputs (all committed, all plain text so diffs across fiscal years are readable):

  data/icd10cm_codes.tsv   code <TAB> description <TAB> inclusion terms <TAB> index terms
      Every billable FY2026 ICD-10-CM code (CDC/NCHS "Code Descriptions" file),
      enriched with two official synonym sources (" | "-joined):
        * inclusion terms from the tabular list XML (e.g. I50.9 gets "Congestive
          heart failure NOS"); terms on a category/subcategory apply to every code
          beneath it, so they are inherited down the tree;
        * Alphabetic Index entries, flattened main-term -> subterm paths that lead
          to the code (e.g. N18.9 gets "Disease, diseased kidney chronic") — the
          lookup a human coder does, and where the default ("NOS") code for plain
          clinical phrasing lives.
      This is the retrieval corpus (chartsight.retrieval) and the guardrail's code set.

  data/hcc_v28.tsv         code <TAB> HCC <TAB> label <TAB> age edit <TAB> sex edit <TAB> MCE age edit
      The ICD-10 → HCC mapping from the CMS-HCC V28 2026 midyear/final model
      software (the mapping the payment model itself uses), with labels from the
      same package. The three edit columns are CMS's own conditions, verbatim
      (e.g. "age < 50"; sex 1 = male, 2 = female): a mapping applies only when
      every non-empty edit holds. Codes absent from this file do not risk-adjust.

  data/cms_hcc_v28/*.csv   the V28 payment model's own tables, copied verbatim
      from the same package: relative factors (coefficients) for continuing
      enrollees, HCC hierarchies, diagnosis categories and interactions. These
      drive chartsight.raf, which is tested against scores produced by the CMS
      software itself (tests/fixtures/cms_v28_reference_scores.json).

Re-run to refresh for a new fiscal year (update the URLs):
    python scripts/build_reference.py [--cache-dir DIR]
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

CDC_BASE = "https://ftp.cdc.gov/pub/health_statistics/nchs/publications/ICD10CM/2026"
CODES_URL = f"{CDC_BASE}/icd10cm-Code%20Descriptions-2026.zip"
CODES_MEMBER = "icd10cm-codes-2026.txt"
TABULAR_URL = f"{CDC_BASE}/icd10cm-Table%20and%20Index-2026.zip"
TABULAR_MEMBER = "icd10cm-tabular-2026.xml"
INDEX_MEMBER = "icd10cm-index-2026.xml"
V28_URL = "https://www.cms.gov/files/zip/2026-midyear-final-model-software-python.zip"
V28_PACKAGE = "CMS_HCC_v28_2026_T_package_v3.zip"
V28_INTERNAL = "software/CMS_HCC_v28/data/input/internal"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CODES_OUT = DATA_DIR / "icd10cm_codes.tsv"
HCC_OUT = DATA_DIR / "hcc_v28.tsv"
MODEL_OUT = DATA_DIR / "cms_hcc_v28"
MODEL_TABLES = (
    "V28_CE_Relative_Factors.csv",
    "V28_HCC_Hierarchies.csv",
    "V28_Diagnosis_Categories.csv",
    "V28_Interactions.csv",
)


def _dotted(raw_code: str) -> str:
    """E119 -> E11.9; E1100 -> E11.00; I10 -> I10 (categories have no decimal)."""
    raw_code = raw_code.replace(".", "")
    return raw_code if len(raw_code) <= 3 else f"{raw_code[:3]}.{raw_code[3:]}"


def _fetch(url: str, cache_dir: Path | None) -> bytes:
    if cache_dir is not None:
        cached = cache_dir / url.rsplit("/", 1)[-1].replace("%20", " ")
        if cached.exists():
            print(f"using cached {cached}")
            return cached.read_bytes()
    print(f"downloading {url}")
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=600) as resp:
        data: bytes = resp.read()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    return data


def _member(zip_bytes: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        match = next((n for n in zf.namelist() if n.endswith(name)), None)
        if match is None:
            raise SystemExit(f"{name} not found in archive — source layout may have changed")
        return zf.read(match)


def load_descriptions(cache_dir: Path | None) -> dict[str, str]:
    raw = _member(_fetch(CODES_URL, cache_dir), CODES_MEMBER).decode("utf-8")
    descriptions: dict[str, str] = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            descriptions[_dotted(parts[0])] = parts[1].strip()
    return descriptions


def load_inclusion_terms(cache_dir: Path | None) -> dict[str, list[str]]:
    """code -> inclusion terms of the code itself and every ancestor <diag>."""
    root = ET.fromstring(_member(_fetch(TABULAR_URL, cache_dir), TABULAR_MEMBER))
    terms: dict[str, list[str]] = {}

    def walk(diag: ET.Element, inherited: list[str]) -> None:
        name = (diag.findtext("name") or "").strip()
        own = [(note.text or "").strip() for note in diag.findall("inclusionTerm/note")]
        combined = inherited + [t for t in own if t and t not in inherited]
        if name:
            terms[_dotted(name)] = combined
        for child in diag.findall("diag"):
            walk(child, combined)

    for top in root.iter("section"):
        for diag in top.findall("diag"):
            walk(diag, [])
    return terms


def load_index_terms(cache_dir: Path | None) -> dict[str, list[str]]:
    """code -> Alphabetic Index paths ("Failure, failed heart congestive") ending at it."""
    root = ET.fromstring(_member(_fetch(TABULAR_URL, cache_dir), INDEX_MEMBER))
    terms: dict[str, list[str]] = {}

    def walk(node: ET.Element, path: list[str]) -> None:
        title_el = node.find("title")
        title = " ".join("".join(title_el.itertext()).split()) if title_el is not None else ""
        here = [*path, title] if title else path
        code = (node.findtext("code") or "").strip()
        if code and not code.endswith("-"):
            terms.setdefault(_dotted(code), []).append(" ".join(here))
        for child in node.findall("term"):
            walk(child, here)

    for main_term in root.iter("mainTerm"):
        walk(main_term, [])
    return terms


def load_v28(cache_dir: Path | None) -> tuple[list[tuple[str, ...]], dict[str, bytes]]:
    """(crosswalk rows, {model table name: verbatim bytes}) from the V28 model software."""
    package = _member(_fetch(V28_URL, cache_dir), V28_PACKAGE)
    mapping_csv = _member(package, f"{V28_INTERNAL}/ICD10_CC_mappings_CMS_HCC_2026_v28.csv")
    tables = {name: _member(package, f"{V28_INTERNAL}/{name}") for name in MODEL_TABLES}

    labels = {
        row["Variable"]: row["Label"].strip()
        for row in csv.DictReader(io.StringIO(tables["V28_CE_Relative_Factors.csv"].decode("utf-8-sig")))
        if row["Variable"].startswith("HCC")
    }
    rows: list[tuple[str, ...]] = []
    for row in csv.DictReader(io.StringIO(mapping_csv.decode("utf-8-sig"))):
        hcc = f"HCC{int(float(row['CC']))}"
        sex = row.get("SEX_EDIT_CONDITION", "").strip()
        rows.append(
            (
                _dotted(row["ICD10"]),
                hcc,
                labels.get(hcc, ""),
                row.get("AGE_EDIT_CONDITION", "").strip(),
                str(int(float(sex))) if sex else "",
                row.get("MCE_AGE_CONDITION", "").strip(),
            )
        )
    return rows, tables


def _clean(text: str) -> str:
    return " ".join(text.replace("\t", " ").replace("|", "/").split())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=None, help="reuse/keep downloaded zips here")
    args = parser.parse_args()

    descriptions = load_descriptions(args.cache_dir)
    if len(descriptions) < 50_000:
        print(f"only parsed {len(descriptions)} codes — source format may have changed", file=sys.stderr)
        sys.exit(1)
    inclusion = load_inclusion_terms(args.cache_dir)
    index = load_index_terms(args.cache_dir)
    v28, model_tables = load_v28(args.cache_dir)

    DATA_DIR.mkdir(exist_ok=True)
    with_terms = with_index = 0
    code_lines = []
    for code in sorted(descriptions):
        terms = [_clean(t) for t in inclusion.get(code, [])]
        index_terms = list(dict.fromkeys(_clean(t) for t in index.get(code, [])))
        with_terms += bool(terms)
        with_index += bool(index_terms)
        code_lines.append(
            f"{code}\t{_clean(descriptions[code])}\t{' | '.join(terms)}\t{' | '.join(index_terms)}"
        )
    CODES_OUT.write_text(
        f"# ICD-10-CM FY2026 billable codes — sources: {CODES_URL} ; {TABULAR_URL}\n"
        f"# {len(code_lines)} codes ({with_terms} with inclusion terms, {with_index} with index terms). "
        "Columns: code, description, inclusion terms, index terms. "
        "Regenerate with scripts/build_reference.py\n" + "\n".join(code_lines) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(code_lines)} codes ({with_terms} w/ inclusion, {with_index} w/ index) -> {CODES_OUT}")

    unknown = sorted({row[0] for row in v28 if row[0] not in descriptions})
    if unknown:
        print(f"warning: {len(unknown)} V28 codes not in the FY2026 code set, e.g. {unknown[:5]}")
    hcc_lines = ["\t".join((code, hcc, _clean(label), *edits)) for code, hcc, label, *edits in sorted(v28)]
    HCC_OUT.write_text(
        f"# CMS-HCC V28 ICD-10 -> HCC crosswalk, 2026 midyear/final model software — source: {V28_URL}\n"
        f"# {len(hcc_lines)} mappings. Columns: code, HCC, label, age edit, sex edit (1=M, 2=F), "
        "MCE age edit. Regenerate with scripts/build_reference.py\n" + "\n".join(hcc_lines) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(hcc_lines)} V28 mappings -> {HCC_OUT}")

    MODEL_OUT.mkdir(exist_ok=True)
    for name, raw in model_tables.items():
        (MODEL_OUT / name).write_bytes(raw)
    print(f"wrote {len(model_tables)} V28 model tables -> {MODEL_OUT}")


if __name__ == "__main__":
    main()
