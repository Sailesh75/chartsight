"""Retrieval over the official ICD-10-CM code set — the "R" in RAG grounding.

Given a condition mention ("chronic systolic CHF, NYHA III"), return the real,
billable ICD-10-CM codes whose official description, inclusion terms or
Alphabetic Index entries best match it, each tagged with its CMS-HCC V28 category. The grounded Bedrock pass
(chartsight.nlp) then makes the model *choose among these candidates* instead
of recalling a code from memory.

Lexical BM25, deliberately: ICD-10-CM descriptions are short, formulaic and
terminology-dense, so term matching (plus a clinical-abbreviation map for the
shorthand clinicians actually write) is a strong, explainable baseline that
needs no model, no API call and no extra dependency — it runs in CI. A dense
retriever can implement the same `Retriever` protocol and be compared on the
eval harness's retrieval-recall metric.
"""

from __future__ import annotations

import gc
import hashlib
import math
import os
import pickle
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from chartsight import reference

# Query-side expansion of shorthand that never appears in official descriptions.
ABBREVIATIONS: dict[str, str] = {
    "afib": "atrial fibrillation",
    "a-fib": "atrial fibrillation",
    "af": "atrial fibrillation",
    "aflutter": "atrial flutter",
    "bmi": "body mass index",
    "bph": "benign prostatic hyperplasia",
    "cad": "coronary artery disease atherosclerotic heart disease",
    "chf": "congestive heart failure",
    "ckd": "chronic kidney disease",
    "copd": "chronic obstructive pulmonary disease",
    "cva": "cerebral infarction stroke",
    "dm": "diabetes mellitus",
    "dm2": "type 2 diabetes mellitus",
    "t2dm": "type 2 diabetes mellitus",
    "t1dm": "type 1 diabetes mellitus",
    "dvt": "deep vein thrombosis",
    "esrd": "end stage renal disease",
    "gerd": "gastro-esophageal reflux disease",
    "hf": "heart failure",
    "hfref": "heart failure reduced ejection fraction systolic",
    "hfpef": "heart failure preserved ejection fraction diastolic",
    "hld": "hyperlipidemia",
    "htn": "hypertension",
    "mdd": "major depressive disorder",
    "mi": "myocardial infarction",
    "oa": "osteoarthritis",
    "osa": "obstructive sleep apnea",
    "pad": "peripheral arterial disease atherosclerosis",
    "pe": "pulmonary embolism",
    "pvd": "peripheral vascular disease",
    "ra": "rheumatoid arthritis",
    "tia": "transient cerebral ischemic attack",
    "uti": "urinary tract infection",
}

_STOPWORDS = frozenset({"a", "an", "and", "as", "at", "by", "for", "in", "is", "of", "on", "or", "the", "to"})
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)?")


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "is", "us")):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def expand_query(text: str) -> str:
    """Append expansions for any known abbreviation in the text (original kept)."""
    words = re.findall(r"[a-z0-9-]+", text.lower())
    extra = [ABBREVIATIONS[w] for w in words if w in ABBREVIATIONS]
    return " ".join([text, *extra])


@dataclass(frozen=True)
class Candidate:
    code: str
    description: str
    score: float
    hcc: tuple[str, ...] = ()  # V28 HCCs, e.g. ("HCC226",); () = does not risk-adjust


class Retriever(Protocol):
    def search(self, query: str, k: int = 10) -> list[Candidate]: ...


class BM25Retriever:
    """Okapi BM25 where every *phrase* — a code's official description, each tabular
    inclusion term, each Alphabetic Index path — is its own document, and a code
    scores as its best-matching phrase.

    Pooling a code's phrases into one bag-of-words document (the obvious approach)
    measurably hurts: well-indexed codes like N18.9 carry dozens of index paths, so
    length normalization buries them under codes with one short description. Max-
    over-phrases lets "chronic kidney disease" match "Chronic renal disease" on its
    own merits. (Compared on the fragment library via evals.run's retrieval metric.)
    """

    def __init__(self, entries: list[reference.CodeEntry], k1: float = 1.2, b: float = 0.75) -> None:
        self._entries = entries
        self._k1 = k1
        self._b = b
        self._owner: list[int] = []  # phrase id -> entry index
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._phrase_len: list[int] = []
        for entry_id, entry in enumerate(entries):
            for phrase in (entry.description, *entry.terms, *entry.index_terms):
                tokens = tokenize(phrase)
                phrase_id = len(self._owner)
                self._owner.append(entry_id)
                self._phrase_len.append(len(tokens))
                for token, tf in Counter(tokens).items():
                    self._postings.setdefault(token, []).append((phrase_id, tf))
        n_phrases = len(self._owner)
        self._avg_len = sum(self._phrase_len) / n_phrases if n_phrases else 0.0
        self._idf = {
            token: math.log(1 + (n_phrases - len(posts) + 0.5) / (len(posts) + 0.5))
            for token, posts in self._postings.items()
        }

    def search(self, query: str, k: int = 10) -> list[Candidate]:
        phrase_scores: dict[int, float] = {}
        for token in set(tokenize(expand_query(query))):
            idf = self._idf.get(token)
            if idf is None:
                continue
            for phrase_id, tf in self._postings[token]:
                norm = self._k1 * (1 - self._b + self._b * self._phrase_len[phrase_id] / self._avg_len)
                phrase_scores[phrase_id] = phrase_scores.get(phrase_id, 0.0) + idf * tf * (self._k1 + 1) / (
                    tf + norm
                )

        code_scores: dict[int, float] = {}
        for phrase_id, score in phrase_scores.items():
            entry_id = self._owner[phrase_id]
            if score > code_scores.get(entry_id, 0.0):
                code_scores[entry_id] = score

        # Tie-break on the shorter code: among equal scores the less-specific
        # parent-level code (e.g. I50.9 over I50.89) is the safer default.
        ranked = sorted(code_scores.items(), key=lambda kv: (-kv[1], len(self._entries[kv[0]].code)))
        results = []
        for entry_id, score in ranked[:k]:
            entry = self._entries[entry_id]
            hcc = tuple(dict.fromkeys(m.hcc for m in reference.hcc_for(entry.code)))
            results.append(Candidate(entry.code, entry.description, round(score, 3), hcc))
        return results


# --------------------------------------------------------------------------- #
# The default index, optionally pre-built at image build time
# --------------------------------------------------------------------------- #
# Building the index takes ~1-1.5 s locally and several times that on a cold Lambda with a
# fraction of a vCPU. The Docker build saves it (save_index) and CHARTSIGHT_BM25_INDEX points
# at the file; loading it is ~5x faster. The file is only ever written by our own build, and it
# carries the code table's fingerprint, so an index built from different data is never used.
INDEX_ENV = "CHARTSIGHT_BM25_INDEX"


@contextmanager
def _gc_paused() -> Iterator[None]:
    # Building or unpickling allocates millions of small objects; the cyclic GC would rescan
    # them repeatedly for cycles that don't exist. Pausing it roughly halves the time.
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def _fingerprint() -> str:
    return hashlib.sha256(reference.CODES_PATH.read_bytes()).hexdigest()


def save_index(path: Path) -> None:
    """Build the default index and write it, with the code table's fingerprint, to `path`."""
    with _gc_paused():
        retriever = BM25Retriever(list(reference.icd10_codes().values()))
    path.write_bytes(pickle.dumps((_fingerprint(), retriever), protocol=pickle.HIGHEST_PROTOCOL))


def _load_index(path: Path) -> BM25Retriever | None:
    with _gc_paused():
        fingerprint, retriever = pickle.loads(path.read_bytes())  # our own build artifact
    return retriever if fingerprint == _fingerprint() and isinstance(retriever, BM25Retriever) else None


@lru_cache(maxsize=1)
def default_retriever() -> BM25Retriever:
    prebuilt = os.environ.get(INDEX_ENV)
    if prebuilt and Path(prebuilt).is_file():
        loaded = _load_index(Path(prebuilt))
        if loaded is not None:
            return loaded
    with _gc_paused():
        return BM25Retriever(list(reference.icd10_codes().values()))
