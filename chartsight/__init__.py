"""ChartSight — evidence-linked HCC coding.

An LLM pipeline that codes clinical notes to ICD-10-CM with verbatim evidence
spans, redacts PHI, and flags risk-adjustment (HCC) documentation gaps.
"""

from chartsight.nlp import MODEL_LABEL, REGION, analyze, load_notes, redact

__all__ = ["MODEL_LABEL", "REGION", "analyze", "load_notes", "redact"]
__version__ = "0.1.0"
