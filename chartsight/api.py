"""HTTP API for the ChartSight pipeline (FastAPI).

    uvicorn chartsight.api:app            # or: chartsight serve

Endpoints
  GET  /health              liveness + which engine a request would use
  POST /v1/analyze          full pipeline for one note (PHI, codes, grounding, HCCs, RAF, gaps)
  POST /v1/raf              CMS-HCC V28 risk score for a list of codes. Deterministic, no LLM.
  GET  /v1/codes/search     retrieval over the official ICD-10-CM code set

Configuration (environment)
  CHARTSIGHT_API_KEY        when set, every /v1 request must send it in the X-API-Key header
  CHARTSIGHT_MAX_NOTE_CHARS max accepted note length (default 20000)
  AWS_REGION, BEDROCK_MODEL_ID, FORCE_MOCK — as for the library (see chartsight.nlp)

Notes are PHI. They travel only in POST bodies, which are never logged. uvicorn's
access log records method, path, query string and status, so /v1/codes/search terms
do appear in it: search with clinical terms, not patient text.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, model_validator

from chartsight import __version__, raf
from chartsight.nlp import MODEL_LABEL, analyze, aws_available
from chartsight.retrieval import default_retriever

MAX_NOTE_CHARS = int(os.environ.get("CHARTSIGHT_MAX_NOTE_CHARS", "20000"))
Segment = Literal[
    "COMMUNITY_NA",
    "COMMUNITY_ND",
    "COMMUNITY_FBA",
    "COMMUNITY_FBD",
    "COMMUNITY_PBA",
    "COMMUNITY_PBD",
    "INSTITUTIONAL",
]


# --------------------------------------------------------------------------- #
# Request / response models (these also drive the OpenAPI docs at /docs)
# --------------------------------------------------------------------------- #
class DemographicsIn(BaseModel):
    age: int = Field(ge=0, le=125)
    sex: Literal["M", "F"]
    orec: Literal[0, 1, 2, 3] = Field(default=0, description="Original reason for entitlement (CMS OREC)")
    ltimcaid: bool = Field(default=False, description="Long-term-institutional Medicaid")

    def to_model(self) -> raf.Demographics:
        return raf.Demographics(self.age, 1 if self.sex == "M" else 2, self.orec, self.ltimcaid)


class RiskSettings(BaseModel):
    demographics: DemographicsIn | None = Field(
        default=None, description="Defaults to the age/sex documented in the note, else a flagged assumption."
    )
    segment: Segment = "COMMUNITY_NA"  # == raf.DEFAULT_SEGMENT (asserted in tests)
    base_rate_pmpm: float = Field(
        default=raf.USPCC_PMPM,
        gt=0,
        description="$ per member per month; defaults to the CY2026 national FFS USPCC.",
    )


class AnalyzeRequest(RiskSettings):
    text: str = Field(min_length=1, description="The clinical note.")
    mode: Literal["auto", "aws", "mock"] = "auto"
    grounded: bool = True

    @model_validator(mode="after")
    def _limit_length(self) -> AnalyzeRequest:
        if len(self.text) > MAX_NOTE_CHARS:
            raise ValueError(f"note exceeds {MAX_NOTE_CHARS} characters")
        return self


class RafRequest(RiskSettings):
    codes: list[str] = Field(min_length=0, max_length=200, description="ICD-10-CM codes, dotted or not.")
    demographics: DemographicsIn  # required here: there is no note to read it from


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")  # forward-compatible: new pipeline fields pass through


class PHISpan(_Open):
    text: str
    type: str
    begin: int
    end: int


class HCCTag(_Open):
    hcc: str
    label: str
    condition: str = ""


class CodeRef(_Open):
    code: str
    description: str


class Condition(_Open):
    text: str
    code: str
    description: str
    confidence: float
    begin: int
    end: int
    hcc_v28: list[HCCTag] = []
    first_pass_code: str | None = None
    candidates: list[CodeRef] = []


class RafTerm(_Open):
    variable: str
    label: str
    coefficient: float


class Outcome(_Open):
    hccs: list[str]
    label: str
    example_codes: list[str]
    delta_raf: float
    delta_dollars: float


class OpportunityOut(_Open):
    code: str
    description: str
    current_hccs: list[str]
    max_delta_dollars: float
    outcomes: list[Outcome]


class RafResult(_Open):
    segment: str
    segment_label: str
    demographics: dict[str, Any]
    raw: float
    payment: float
    annual_dollars: float
    base_rate_pmpm: float
    terms: list[RafTerm]
    dropped_by_hierarchy: dict[str, str]
    opportunities: list[OpportunityOut]


class AnalyzeResponse(_Open):
    engine: str
    region: str | None
    fallback_reason: str | None
    grounded: bool
    phi: list[PHISpan]
    redacted: str
    conditions: list[Condition]
    rejected_codes: list[dict[str, Any]]
    ungrounded: list[dict[str, Any]]
    raf: RafResult
    gaps: list[str]


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    engine: str
    model: str


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    default_retriever()  # build the ~1.5 s BM25 index at startup, not on the first request
    yield


app = FastAPI(
    title="ChartSight API",
    version=__version__,
    description="Evidence-linked ICD-10-CM coding, RAG grounding, CMS-HCC V28 risk scoring.",
    lifespan=_lifespan,
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: Annotated[str | None, Security(_api_key_header)]) -> None:
    expected = os.environ.get("CHARTSIGHT_API_KEY")
    if expected and not (key and secrets.compare_digest(key, expected)):
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


@app.get("/health", response_model=Health)
def health() -> Health:
    engine = f"Amazon Bedrock ({MODEL_LABEL})" if aws_available() else "local sample engine"
    return Health(status="ok", version=__version__, engine=engine, model=MODEL_LABEL)


@app.post("/v1/analyze", response_model=AnalyzeResponse, dependencies=[Depends(require_api_key)])
def analyze_note(req: AnalyzeRequest) -> dict[str, Any]:
    # Sync def on purpose: boto3 blocks, so FastAPI runs this in its threadpool.
    result = analyze(
        req.text,
        mode=req.mode,
        grounded=req.grounded,
        demographics=req.demographics.to_model() if req.demographics else None,
        segment=req.segment,
        base_rate_pmpm=req.base_rate_pmpm,
    )
    return {**result, "gaps": result["insights"]["gaps"]}


@app.post("/v1/raf", response_model=RafResult, dependencies=[Depends(require_api_key)])
def score_codes(req: RafRequest) -> dict[str, Any]:
    return raf.assess(req.codes, req.demographics.to_model(), req.segment, req.base_rate_pmpm)


@app.get("/v1/codes/search", response_model=list[CodeRef], dependencies=[Depends(require_api_key)])
def search_codes(
    q: Annotated[str, Query(min_length=2, max_length=300)],
    k: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[dict[str, Any]]:
    return [
        {"code": c.code, "description": c.description, "score": c.score, "hcc_v28": list(c.hcc)}
        for c in default_retriever().search(q, k=k)
    ]
