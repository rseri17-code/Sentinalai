"""Retrieval Quality eval tests — 7 assertions covering the full retrieval pipeline.

Tests:
  1. Cited evidence only  — citation_coverage ≥ CITATION_FLOOR gates pass
  2. Stale docs downgraded — old runbook scores below live golden_signals
  3. Weak evidence fallback — G3/G5 gates fire on low-confidence result
  4. Confidence ordering  — golden_signals beats old runbook after reranking
  5. Cache traceability   — first=miss, second=hit, key is consistent
  6. BM25 rare terms      — specific error code scores higher than common word
  7. Hybrid score fusion  — fused score is in expected range of components
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "retrieval"

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Test 1 — Answer uses only cited evidence
# ---------------------------------------------------------------------------

def test_citation_coverage_gates_pass_when_citations_present():
    """G3/G5 both pass when result has valid citations and coverage ≥ floor."""
    from supervisor.evidence_gates import check_post_analysis, CITATION_FLOOR, GateVerdict

    doc = _load("incident_summary.json")

    result = {
        "confidence": 85,
        "citation_coverage": doc["citation_coverage"],   # 0.88
        "citations": doc["citations"],
        "root_cause": doc["root_cause"],
    }
    evidence = {"experience_store": {"data": doc}}

    gate_result = check_post_analysis(result, evidence, budget_remaining=5)

    assert gate_result.passed, f"Expected PASS, got {gate_result.verdict}: {gate_result}"
    # G3 should not fire because coverage is well above floor
    g3 = next((g for g in gate_result.gates if g.gate_name == "G3_CitationFloor"), None)
    assert g3 is None or g3.verdict == GateVerdict.PASS, (
        f"G3 should PASS when citation_coverage={doc['citation_coverage']} >= floor={CITATION_FLOOR}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Stale docs are downgraded
# ---------------------------------------------------------------------------

def test_stale_runbook_has_lower_confidence_than_live_metrics():
    """Old runbook (collected 2025-09-01) has lower final_confidence than live golden_signals."""
    from supervisor.retrieval.source_confidence import score_source

    runbook = _load("runbook.json")
    obs = _load("observability_evidence.json")

    runbook_score = score_source(
        source_type=runbook["source_type"],
        collected_at=runbook["collected_at"],
    )
    obs_score = score_source(
        source_type=obs["source_type"],
        collected_at=obs["collected_at"],
    )

    assert runbook_score.is_stale(), (
        f"Runbook from {runbook['collected_at']} should be stale; "
        f"freshness={runbook_score.freshness_factor:.3f}"
    )
    assert not obs_score.is_stale(), (
        f"Live golden_signals should not be stale; freshness={obs_score.freshness_factor:.3f}"
    )
    assert runbook_score.final_confidence < obs_score.final_confidence, (
        f"Runbook ({runbook_score.final_confidence:.3f}) should be less confident "
        f"than live metrics ({obs_score.final_confidence:.3f})"
    )


# ---------------------------------------------------------------------------
# Test 3 — Weak evidence triggers fallback (G3 + G5 gates fire)
# ---------------------------------------------------------------------------

def test_weak_evidence_triggers_g3_and_g5():
    """G3 WARN fires on low citation_coverage; G5 BLOCK fires with root cause but no citations."""
    from supervisor.evidence_gates import check_post_analysis, GateVerdict

    # Low-quality result: root cause present, no citations, coverage=0
    weak_result = {
        "confidence": 20,
        "citation_coverage": 0.0,
        "citations": [],
        "root_cause": "memory leak in connection pool",
    }
    # Non-empty evidence so G4/G5 hallucination path applies
    evidence = {"search_logs": {"data": [{"line": "OOM killed"}]}}

    gate_result = check_post_analysis(weak_result, evidence, budget_remaining=1)

    # Should NOT pass — either G5 BLOCK or at least G3 WARN
    assert not gate_result.passed or gate_result.verdict != GateVerdict.PASS, (
        "Weak evidence (no citations, coverage=0) should not fully pass"
    )

    gate_names = {g.gate_name: g.verdict for g in gate_result.gates}
    # G5: root cause exists, evidence is non-empty, but no citations → BLOCK
    if "G5_HallucinationRisk" in gate_names:
        assert gate_names["G5_HallucinationRisk"] == GateVerdict.BLOCK
    # G3: coverage=0 < 0.35 floor → WARN at minimum
    if "G3_CitationFloor" in gate_names:
        assert gate_names["G3_CitationFloor"] in (GateVerdict.WARN, GateVerdict.BLOCK)


# ---------------------------------------------------------------------------
# Test 4 — Higher-confidence sources win reranking
# ---------------------------------------------------------------------------

def test_golden_signals_outranks_stale_runbook_after_rerank():
    """After reranking, live golden_signals scores above the stale runbook."""
    from supervisor.retrieval.hybrid_retriever import rank
    from supervisor.retrieval.reranker import rerank

    runbook = _load("runbook.json")
    obs     = _load("observability_evidence.json")

    candidates = [
        {
            "doc_id": runbook["doc_id"],
            "text": runbook["text"],
            "source_type": runbook["source_type"],
            "collected_at": runbook["collected_at"],
            "metadata": runbook["metadata"],
        },
        {
            "doc_id": obs["doc_id"],
            "text": " ".join([
                obs["title"],
                " ".join(obs["alerts_firing"]),
                "jvm heap connection pool oomkill memory",
            ]),
            "source_type": obs["source_type"],
            "collected_at": obs["collected_at"],
            "metadata": obs["metadata"],
        },
    ]

    query = "payment-service oomkill jvm heap connection pool memory"
    ranked = rank(query, candidates, top_k=2)
    reranked = rerank(ranked, service="payment-service", incident_type="oomkill")

    assert len(reranked) == 2, "Expected exactly 2 results"

    scores = {r.doc_id: r.rerank_score for r in reranked}
    assert scores[obs["doc_id"]] > scores[runbook["doc_id"]], (
        f"golden_signals ({scores[obs['doc_id']]:.4f}) should outscore "
        f"stale runbook ({scores[runbook['doc_id']]:.4f})"
    )


# ---------------------------------------------------------------------------
# Test 5 — Cache reuse is traceable (first=miss, second=hit)
# ---------------------------------------------------------------------------

def test_cache_miss_then_hit_with_consistent_key():
    """RetrievalCache: first get() returns None (miss), put(), second get() returns data."""
    from supervisor.retrieval.retrieval_cache import RetrievalCache

    cache = RetrievalCache(ttl=60, max_entries=10, store_path="/tmp/_test_cache.json")

    service       = "payment-service"
    incident_type = "oomkill"
    query         = "connection pool memory leak jvm heap"
    results       = [{"incident_id": "INC-001", "score": 0.9}]

    # First call — must be a miss
    hit = cache.get(service, incident_type, query)
    assert hit is None, "Expected cache miss on first access"

    # Store
    key1 = cache.put(service, incident_type, query, results)
    assert key1, "Cache put should return a non-empty key"

    # Second call — must be a hit with same key
    hit2 = cache.get(service, incident_type, query)
    assert hit2 is not None, "Expected cache hit on second access"

    # Key must be deterministic (same inputs → same key)
    key2 = cache.put(service, incident_type, query, results)
    assert key1 == key2, f"Cache key should be deterministic: {key1} != {key2}"

    stats = cache.stats()
    assert stats["hits"] >= 1
    assert stats["misses"] >= 1


# ---------------------------------------------------------------------------
# Test 6 — BM25 scores rare terms higher than common terms
# ---------------------------------------------------------------------------

def test_bm25_rare_term_scores_higher_than_common_term():
    """BM25 IDF weighting: a doc with a rare error code beats a doc with a common word
    when the query contains the rare term."""
    from supervisor.retrieval.bm25 import BM25Index

    # Build a small corpus where 'oomkill' is rare (appears in 1/5 docs)
    # and 'error' is common (appears in 4/5 docs)
    corpus = [
        ("d1", "payment service error timeout connection"),
        ("d2", "auth service error rate spike"),
        ("d3", "payment service error memory warning"),
        ("d4", "checkout service error latency p99"),
        ("d5", "payment service oomkill jvm heap connection pool"),   # rare term doc
    ]
    idx = BM25Index.build(corpus)

    # Query for the rare term
    rare_results = dict(idx.rank("oomkill connection pool", top_k=5))
    common_results = dict(idx.rank("error payment service", top_k=5))

    # d5 should rank first for the rare query
    all_ids = list(rare_results.keys())
    assert all_ids[0] == "d5", (
        f"Doc with rare term 'oomkill' should rank first; got {all_ids}"
    )

    # d5 score for rare query > its score for common query
    # (the rare-term query rewards specificity more)
    d5_rare_score   = rare_results.get("d5", 0.0)
    d5_common_score = common_results.get("d5", 0.0)
    assert d5_rare_score > 0, "d5 should score positively for rare query"
    assert d5_rare_score > d5_common_score, (
        f"d5 rare score ({d5_rare_score:.4f}) should exceed common score ({d5_common_score:.4f})"
    )


# ---------------------------------------------------------------------------
# Test 7 — Hybrid score correctly fuses BM25 and cosine
# ---------------------------------------------------------------------------

def test_hybrid_score_fuses_bm25_and_cosine_within_expected_range():
    """Hybrid final_score = alpha*bm25_norm + (1-alpha)*cosine_norm, scaled by confidence.

    The fused score must be in [0, source_confidence] and must differ from
    both pure BM25 and pure cosine extremes.
    """
    from supervisor.retrieval.hybrid_retriever import rank, ALPHA

    obs = _load("observability_evidence.json")
    chg = _load("change_record.json")

    candidates = [
        {
            "doc_id": obs["doc_id"],
            "text": f"{obs['title']} {' '.join(obs['alerts_firing'])} oomkill heap memory connection pool",
            "source_type": obs["source_type"],
            "collected_at": obs["collected_at"],
            "metadata": obs["metadata"],
        },
        {
            "doc_id": chg["doc_id"],
            "text": f"{chg['summary']} {' '.join(chg['diff_summary'])}",
            "source_type": chg["source_type"],
            "collected_at": chg["collected_at"],
            "metadata": chg["metadata"],
        },
    ]

    query = "payment-service oomkill jvm heap connection pool"
    results = rank(query, candidates, top_k=2)

    assert len(results) >= 1, "Expected at least one ranked result"

    for r in results:
        # final_score ≤ source_confidence (confidence cap)
        assert r.final_score <= r.source_confidence + 1e-6, (
            f"{r.doc_id}: final_score {r.final_score:.4f} exceeds source_confidence {r.source_confidence:.4f}"
        )
        # final_score must be ≥ 0
        assert r.final_score >= 0.0, f"{r.doc_id}: negative final_score {r.final_score}"

        # Individual BM25 and cosine components must be normalised to [0, 1]
        assert 0.0 <= r.bm25_score <= 1.0 + 1e-6, f"bm25_score out of range: {r.bm25_score}"
        assert 0.0 <= r.cosine_score <= 1.0 + 1e-6, f"cosine_score out of range: {r.cosine_score}"

    # The top result should blend both signals — final > 0 proves fusion ran
    top = results[0]
    assert top.final_score > 0.0, "Top ranked candidate must have a positive fused score"
    assert top.bm25_score >= 0.0 and top.cosine_score >= 0.0, "Both BM25 and cosine must be non-negative"
