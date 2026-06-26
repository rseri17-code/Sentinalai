"""Tests for Phase 1C — TF-IDF warm cache in EpisodicMemory and ResolutionKnowledge.

Verifies:
- SemanticIndex is NOT rebuilt on repeated calls with the same candidates
- SemanticIndex IS rebuilt when records change (cache invalidation)
- Correct results still returned after caching
- get_similar() with service filter uses the same cache
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, call

import pytest

from intelligence.episodic_memory import EpisodicMemory, Episode
from intelligence.resolution_knowledge import ResolutionKnowledge, ResolutionRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _episode(service="svc", incident_type="timeout", sig="timeout error", eid=None):
    import uuid
    return Episode(
        episode_id=eid or str(uuid.uuid4()),
        incident_id="inc-1",
        service=service,
        incident_type=incident_type,
        failure_signature=sig,
        root_cause="unknown",
        confidence=0.7,
        resolution_action="restart",
        resolved_by="auto",
        time_to_resolve_ms=60000,
        evidence_keys=[],
        outcome="resolved",
        tags=[],
        recorded_at="2024-01-01T00:00:00+00:00",
    )


def _record(failure_mode="timeout", action="restart"):
    import uuid
    return ResolutionRecord(
        record_id=str(uuid.uuid4()),
        failure_mode=failure_mode,
        incident_type="timeout",
        service_tier=2,
        action_taken=action,
        action_description=f"Description for {action}",
        success=True,
        time_to_resolve_ms=30000,
        confidence_before=0.4,
        confidence_after=0.9,
        recorded_at="2024-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# EpisodicMemory — search() cache
# ---------------------------------------------------------------------------

class TestEpisodicMemorySearchCache:

    @pytest.fixture()
    def mem(self, tmp_path):
        path = str(tmp_path / "episodes.jsonl")
        m = EpisodicMemory(storage_path=path)
        # Clear any demo episodes so we have a clean slate
        m._episodes.clear()
        m._filtered_cache.clear()
        m._index = None
        # Add deterministic test episodes
        for i in range(3):
            ep = _episode(service="my-service", incident_type="timeout",
                          sig=f"timeout error on endpoint {i}", eid=f"ep-{i}")
            m._episodes.append(ep)
        return m

    def test_cache_populated_on_first_search(self, mem):
        assert len(mem._filtered_cache) == 0
        mem.search(service="my-service", failure_signature="timeout")
        assert len(mem._filtered_cache) == 1

    def test_cached_index_reused_on_second_search(self, mem):
        mem.search(service="my-service", failure_signature="timeout")
        cache_key = list(mem._filtered_cache.keys())[0]
        first_idx = mem._filtered_cache[cache_key]

        mem.search(service="my-service", failure_signature="timeout error")
        assert mem._filtered_cache[cache_key] is first_idx, (
            "SemanticIndex must be reused, not rebuilt, on second search"
        )

    def test_cache_cleared_on_record(self, mem, tmp_path):
        # Populate cache
        mem.search(service="my-service", failure_signature="timeout")
        assert len(mem._filtered_cache) == 1

        # Record a new episode — must invalidate the cache
        new_ep = _episode(service="my-service", incident_type="timeout",
                          sig="new error pattern", eid="ep-new")
        mem._path = str(tmp_path / "episodes.jsonl")
        os.makedirs(os.path.dirname(mem._path), exist_ok=True)
        # Patch the file write to avoid actual I/O error
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: False
            mock_open.return_value.write = lambda x: None
            mem.record(new_ep)

        assert len(mem._filtered_cache) == 0, (
            "Cache must be cleared after record() so stale index is not reused"
        )

    def test_different_filter_combinations_cached_separately(self, mem):
        mem.search(service="my-service", failure_signature="timeout")
        mem.search(incident_type="timeout", failure_signature="timeout")
        # Two distinct filter combos → two distinct cache entries
        assert len(mem._filtered_cache) == 2

    def test_search_results_unchanged_after_caching(self, mem):
        r1 = mem.search(service="my-service", failure_signature="timeout error on endpoint 0")
        r2 = mem.search(service="my-service", failure_signature="timeout error on endpoint 0")
        assert r1 == r2

    def test_empty_candidates_does_not_cache(self, mem):
        results = mem.search(service="nonexistent-service", failure_signature="timeout")
        assert results == []
        assert len(mem._filtered_cache) == 0, "Empty candidate list must not populate cache"


# ---------------------------------------------------------------------------
# EpisodicMemory — get_similar() with service filter cache
# ---------------------------------------------------------------------------

class TestEpisodicMemoryGetSimilarCache:

    @pytest.fixture()
    def mem(self, tmp_path):
        path = str(tmp_path / "episodes.jsonl")
        m = EpisodicMemory(storage_path=path)
        m._episodes.clear()
        m._filtered_cache.clear()
        m._index = None
        for i in range(3):
            ep = _episode(service="my-service", incident_type="timeout",
                          sig=f"timeout error pattern {i}", eid=f"ep-{i}")
            m._episodes.append(ep)
        return m

    def test_get_similar_populates_filtered_cache(self, mem):
        mem.get_similar("timeout error", service="my-service")
        # The (service, "", n) cache entry must be present
        keys = list(mem._filtered_cache.keys())
        assert any(k[0] == "my-service" for k in keys)

    def test_get_similar_reuses_cached_index(self, mem):
        mem.get_similar("timeout error", service="my-service")
        cache_before = dict(mem._filtered_cache)
        mem.get_similar("latency spike", service="my-service")
        # The same index should be reused
        for k, v in cache_before.items():
            if k[0] == "my-service":
                assert mem._filtered_cache[k] is v

    def test_get_similar_without_service_uses_self_index(self, mem):
        # Without service, get_similar() uses self._index (the full index).
        # It must NOT use _filtered_cache.
        mem.get_similar("timeout error")
        # _filtered_cache should remain empty (full index goes to self._index)
        assert len(mem._filtered_cache) == 0


# ---------------------------------------------------------------------------
# ResolutionKnowledge — _semantic_candidates() cache
# ---------------------------------------------------------------------------

class TestResolutionKnowledgeSemCache:

    @pytest.fixture()
    def rk(self, tmp_path):
        path = str(tmp_path / "resolution.jsonl")
        r = ResolutionKnowledge(storage_path=path)
        r._records.clear()
        r._sem_index = None
        r._sem_index_len = 0
        # Add deterministic records
        for mode in ("timeout", "oom", "latency"):
            rec = _record(failure_mode=mode, action=f"action_{mode}")
            r._records.append(rec)
        return r

    def test_sem_index_built_on_first_call(self, rk):
        assert rk._sem_index is None
        rk._semantic_candidates("timeout spike")
        assert rk._sem_index is not None

    def test_sem_index_reused_on_second_call(self, rk):
        rk._semantic_candidates("timeout spike")
        first_idx = rk._sem_index
        rk._semantic_candidates("oom error")
        assert rk._sem_index is first_idx, (
            "SemanticIndex must be reused, not rebuilt, on second _semantic_candidates call"
        )

    def test_sem_index_cleared_on_record(self, rk, tmp_path):
        rk._semantic_candidates("timeout spike")
        assert rk._sem_index is not None

        new_rec = _record(failure_mode="disk_full", action="expand_disk")
        rk._path = str(tmp_path / "resolution.jsonl")
        os.makedirs(os.path.dirname(rk._path), exist_ok=True)
        with patch("builtins.open", create=True) as mock_open:
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = lambda s, *a: False
            mock_open.return_value.write = lambda x: None
            rk.record(new_rec)

        assert rk._sem_index is None, (
            "sem_index must be cleared after record() so new failure modes are indexed"
        )

    def test_sem_index_rebuilt_after_invalidation(self, rk, tmp_path):
        rk._semantic_candidates("timeout spike")
        old_idx = rk._sem_index

        # Force invalidation
        rk._sem_index = None

        rk._semantic_candidates("timeout spike")
        new_idx = rk._sem_index
        assert new_idx is not old_idx, "A new index must be built after invalidation"

    def test_empty_records_returns_empty(self, rk):
        rk._records.clear()
        result = rk._semantic_candidates("timeout")
        assert result == []
        assert rk._sem_index is None, "No index should be built for empty records"
