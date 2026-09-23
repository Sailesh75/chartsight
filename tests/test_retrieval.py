"""Tests for BM25 retrieval over the ICD-10-CM code set."""

from __future__ import annotations

from chartsight.reference import CodeEntry
from chartsight.retrieval import BM25Retriever, default_retriever, expand_query, tokenize
from evals.fragments import FRAGMENTS


def test_tokenize_lowercases_drops_stopwords_and_stems() -> None:
    assert tokenize("Atherosclerosis of the native Arteries") == ["atherosclerosis", "native", "artery"]


def test_expand_query_keeps_original_and_adds_abbreviation() -> None:
    expanded = expand_query("CHF, stable")
    assert expanded.startswith("CHF, stable")
    assert "congestive heart failure" in expanded


def test_code_scores_as_its_best_phrase_not_a_pooled_bag() -> None:
    corpus = [
        CodeEntry("A00.0", "Thing, unspecified", terms=("widget failure NOS",), index_terms=("x",) * 30),
        CodeEntry("B00.0", "Widget failure with gadget involvement and several more words"),
    ]
    results = BM25Retriever(corpus).search("widget failure", k=2)
    # A00.0's many index phrases don't dilute its short, exact inclusion-term match.
    assert results[0].code == "A00.0"


def test_equal_scores_prefer_the_shorter_parent_code() -> None:
    corpus = [CodeEntry("I50.89", "Other heart failure"), CodeEntry("I50.9", "Other heart failure")]
    assert BM25Retriever(corpus).search("heart failure", k=2)[0].code == "I50.9"


def test_real_index_ranks_specific_code_first_and_tags_hcc() -> None:
    top = default_retriever().search("chronic systolic congestive heart failure, NYHA class III", k=5)
    assert top[0].code == "I50.22"
    assert top[0].hcc == ("HCC226",)


def test_abbreviations_reach_the_right_family() -> None:
    codes = [c.code for c in default_retriever().search("CKD stage 4", k=5)]
    assert "N18.4" in codes


def test_fragment_library_recall_at_5() -> None:
    """Regression floor for retrieval quality, queried the way the grounded pass does it:
    evidence mention + the (first-pass) description of the code."""
    retriever = default_retriever()
    total = hits = 0
    for fragment in FRAGMENTS:
        for expected in fragment.codes:
            total += 1
            top5 = [c.code for c in retriever.search(f"{fragment.text} {expected.description}", k=5)]
            hits += expected.code in top5
    assert hits / total >= 0.95, f"recall@5 = {hits}/{total}"
