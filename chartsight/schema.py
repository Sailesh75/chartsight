"""Typed schema for a single Bedrock extraction — used as the tool-use
`input_schema` (so Claude returns already-structured, already-parsed JSON
instead of prose we have to scrape with `text.find("{")`) and to validate the
result before it ever reaches the rest of the pipeline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ICD-10-CM code shape: a letter, two digits, an optional 3rd category
# character, then an optional 1-4 character decimal suffix.
# Examples: I10, E11.9, N18.32, F32.A, I70.211
ICD10_PATTERN = r"^[A-Z]\d{2}[A-Z0-9]?(\.[A-Z0-9]{1,4})?$"

PHIType = Literal["NAME", "ID", "DATE", "PHONE", "ADDRESS", "EMAIL", "AGE"]


class PHIEntity(BaseModel):
    text: str = Field(min_length=1, description="Verbatim substring from the note.")
    type: PHIType


class CodedCondition(BaseModel):
    text: str = Field(min_length=1, description="Verbatim evidence substring from the note.")
    code: str = Field(pattern=ICD10_PATTERN, description="ICD-10-CM code, e.g. E11.9")
    description: str = Field(min_length=1, description="Official code description.")
    confidence: float = Field(ge=0.0, le=1.0)


class AnalysisExtraction(BaseModel):
    """The full structured extraction for one clinical note."""

    phi: list[PHIEntity] = Field(default_factory=list)
    conditions: list[CodedCondition] = Field(default_factory=list, max_length=8)
    gaps: list[str] = Field(default_factory=list)
