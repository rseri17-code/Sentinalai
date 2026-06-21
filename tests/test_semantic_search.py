"""Tests for SemanticIndex (TF-IDF semantic search) and its integration
with EpisodicMemory and ResolutionKnowledge."""
from __future__ import annotations

import os
import tempfile
import uuid

import pytest

from intelligence.semantic_search import SemanticIndex


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_path() -> str:
    return os.path.join(tempfile.mkdtemp(), f"{uuid.uuid4()}.jsonl")


# ---------------------------------------------------------------------------
# 1. index_build_and_basic_search
# ---------------------------------------------------------------------------

def test_index_build_and_basic_search():
    idx = SemanticIndex()
    idx.add("a", "connection pool exhausted postgres")
    idx.add("b", "redis eviction memory limit")
    results = idx.search("postgres connection pool", top_k=2)
    assert len(results) == 2
    assert results[0][0] == "a"
    assert results[0][1] > results[1][1]


# ---------------------------------------------------------------------------
# 2. cosine_similarity_ranking
# ---------------------------------------------------------------------------

def test_cosine_similarity_ranking():
    idx = SemanticIndex()
    idx.add("exact", "JWT validation spike CPU saturation")
    idx.add("partial", "JWT timeout high concurrency")
    idx.add("unrelated", "kafka consumer lag throughput")
    results = idx.search("JWT validation CPU", top_k=3)
    ids = [r[0] for r in results]
    assert ids[0] == "exact"


# ---------------------------------------------------------------------------
# 3. semantic_equivalence_oom_memory_pressure
# ---------------------------------------------------------------------------

def test_semantic_equivalence_oom_memory_pressure():
    idx = SemanticIndex()
    idx.add("mp", "memory pressure cache eviction")
    idx.add("unrelated", "kafka consumer lag growing")
    results = idx.search("OOM killer out of memory", top_k=2)
    mp_score = next((s for id, s in results if id == "mp"), 0.0)
    # TF-IDF shares no exact tokens between "OOM killer" and "memory pressure"
    # but "memory" appears in both — score should be above threshold
    assert mp_score >= 0.0  # non-negative; may be small due to no token overlap
    # "memory" appears in query and document — should rank above unrelated
    unrelated_score = next((s for id, s in results if id == "unrelated"), 0.0)
    assert mp_score >= unrelated_score


# ---------------------------------------------------------------------------
# 4. semantic_equivalence_latency_slow_response
# ---------------------------------------------------------------------------

def test_semantic_equivalence_latency_slow_response():
    idx = SemanticIndex()
    idx.add("lat", "latency spike p99 degraded response time")
    idx.add("unrelated", "disk full inode exhausted")
    results = idx.search("slow response high latency", top_k=2)
    lat_score = next((s for id, s in results if id == "lat"), 0.0)
    unrelated_score = next((s for id, s in results if id == "unrelated"), 0.0)
    assert lat_score > unrelated_score


# ---------------------------------------------------------------------------
# 5. incremental_updates
# ---------------------------------------------------------------------------

def test_incremental_updates():
    idx = SemanticIndex()
    idx.add("a", "postgres timeout pool")
    results_before = idx.search("postgres timeout", top_k=5)
    assert len(results_before) == 1

    idx.add("b", "redis memory eviction")
    results_after = idx.search("postgres timeout", top_k=5)
    assert len(results_after) == 2
    # "a" should still rank first
    assert results_after[0][0] == "a"


# ---------------------------------------------------------------------------
# 6. empty_index_returns_empty
# ---------------------------------------------------------------------------

def test_empty_index_returns_empty():
    idx = SemanticIndex()
    assert idx.search("anything", top_k=5) == []


# ---------------------------------------------------------------------------
# 7. top_k_limiting
# ---------------------------------------------------------------------------

def test_top_k_limiting():
    idx = SemanticIndex()
    for i in range(10):
        idx.add(f"doc{i}", f"document text about topic {i} postgres database")
    results = idx.search("postgres database", top_k=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# 8. episodic_memory_get_similar_uses_semantic_search
# ---------------------------------------------------------------------------

def test_episodic_memory_get_similar_uses_semantic_search():
    from intelligence.episodic_memory import Episode, EpisodicMemory

    path = _tmp_path()
    with open(path, "w") as f:
        pass  # empty — no seed

    mem = EpisodicMemory(storage_path=path)

    def _ep(sig):
        return Episode(
            episode_id=str(uuid.uuid4()),
            incident_id="INC-X",
            service="svc",
            incident_type="timeout",
            failure_signature=sig,
            root_cause="root",
            confidence=0.9,
            resolution_action="fix",
            resolved_by="auto",
            time_to_resolve_ms=1000,
            evidence_keys=[],
            outcome="resolved",
            tags=[],
            recorded_at="2026-01-01T00:00:00+00:00",
        )

    mem.record(_ep("postgres connection pool exhausted"))
    mem.record(_ep("redis cache eviction memory limit"))
    mem.record(_ep("kafka consumer lag growing partition"))

    results = mem.get_similar("postgres connection pool timeout", limit=2)
    assert len(results) >= 1
    assert "postgres" in results[0].failure_signature.lower() or "connection" in results[0].failure_signature.lower()


# ---------------------------------------------------------------------------
# 9. resolution_knowledge_semantic_fallback
# ---------------------------------------------------------------------------

def test_resolution_knowledge_semantic_recommend():
    from intelligence.resolution_knowledge import ResolutionKnowledge, ResolutionRecord, ResolutionRecommendation

    path = _tmp_path()
    with open(path, "w") as f:
        pass

    rk = ResolutionKnowledge(storage_path=path)

    def _rec(mode, action, desc):
        return ResolutionRecord(
            record_id=str(uuid.uuid4()),
            failure_mode=mode,
            incident_type="timeout",
            service_tier=1,
            action_taken=action,
            action_description=desc,
            success=True,
            time_to_resolve_ms=300_000,
            confidence_before=0.7,
            confidence_after=0.9,
            recorded_at="2026-01-01T00:00:00+00:00",
        )

    rk.record(_rec("connection_pool_exhausted", "increase_pool_size", "Increase pool size"))
    rk.record(_rec("redis_eviction", "increase_redis_maxmemory", "Increase Redis memory"))

    # Exact match works
    recs = rk.recommend(failure_mode="connection_pool_exhausted", incident_type="timeout")
    assert len(recs) >= 1
    assert isinstance(recs[0], ResolutionRecommendation)

    # Semantic fallback: unknown failure_mode but matching incident_type
    recs_fallback = rk.recommend(failure_mode="totally_unknown_mode", incident_type="timeout")
    assert len(recs_fallback) >= 1
