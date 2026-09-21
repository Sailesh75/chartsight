"""Build data/icd10cm_valid_codes.txt from the official CMS/CDC ICD-10-CM release.

Downloads the FY2026 "Code Descriptions" package from the CDC's public FTP
mirror of the CMS ICD-10-CM files, extracts the short-description flat file
(one billable code + description per line, code with no decimal point), and
writes out a lean, sorted, deduped list of dotted codes (e.g. E11.9, not
E119) — that's all the guardrail needs to check a code is real.

Re-run this to refresh for a new fiscal year:
    python scripts/build_icd10_reference.py
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from urllib.request import urlopen

SOURCE_URL = "https://ftp.cdc.gov/pub/health_statistics/nchs/publications/ICD10CM/2026/icd10cm-Code%20Descriptions-2026.zip"
SOURCE_MEMBER = "icd10cm-codes-2026.txt"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "icd10cm_valid_codes.txt"


def _dotted(raw_code: str) -> str:
    """E119 -> E11.9; E1100 -> E11.00; I10 -> I10 (categories have no decimal)."""
    return raw_code if len(raw_code) <= 3 else f"{raw_code[:3]}.{raw_code[3:]}"


def main() -> None:
    print(f"downloading {SOURCE_URL}")
    with urlopen(SOURCE_URL, timeout=60) as resp:
        raw_zip = resp.read()

    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        raw_text = zf.read(SOURCE_MEMBER).decode("utf-8")

    codes: set[str] = set()
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        raw_code = line.split(None, 1)[0]
        codes.add(_dotted(raw_code))

    if len(codes) < 50_000:
        print(f"only parsed {len(codes)} codes — source format may have changed", file=sys.stderr)
        sys.exit(1)

    OUT_PATH.parent.mkdir(exist_ok=True)
    header = (
        f"# ICD-10-CM valid codes — FY2026, source: {SOURCE_URL}\n"
        f"# {len(codes)} billable codes. Regenerate with scripts/build_icd10_reference.py\n"
    )
    OUT_PATH.write_text(header + "\n".join(sorted(codes)) + "\n", encoding="utf-8")
    print(f"wrote {len(codes)} codes -> {OUT_PATH}")


if __name__ == "__main__":
    main()
