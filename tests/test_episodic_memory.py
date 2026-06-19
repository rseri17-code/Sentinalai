"""Tests for EpisodicMemory and ResolutionKnowledge."""
from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest

from intelligence.episodic_memory import Episode, EpisodicMemory
from intelligence.resolution_knowledge import ResolutionRecord, ResolutionKnowledge, ResolutionRecommendation


def _tmp_path() -> str:
    return os.path.join(tempfile.mkdtemp(), f"{uuid.uuid4()}.jsonl")


def _make_episode(**kwargs) -> Episode:
    defaults = dict(
        episode_id=str(uuid.uuid4()),
        incident_id="INC-TEST-1",
        service="payment-service",
        incident_type="timeout",
        failure_signature="postgres connection pool exhausted",
        root_cause="Pool exhausted under load",
        confidence=0.85,
        resolution_action="increase pool size",
        resolved_by="SRE-on-call",
        time_to_resolve_ms=480_000,
        evidence_keys=["search_logs", "get_golden_signals"],
        outcome="resolved",
        tags=["database", "connection-pool"],
        recorded_at="2026-06-01T00:00:00+00:00",
    )
    defaults.update(kwargs)
    return Episode(**defaults)


def _make_record(**kwargs) -> ResolutionRecord:
    defaults = dict(
        record_id=str(uuid.uuid4()),
        failure_mode="connection_pool_exhausted",
        incident_type="timeout",
        service_tier=1,
        action_taken="increase_pool_size",
        action_description="Increase Postgres connection pool size",
        success=True,
        time_to_resolve_ms=480_000,
        confidence_before=0.75,
        confidence_after=0.91,
        recorded_at="2026-06-01T00:00:00+00:00",
    )
    defaults.update(kwargs)
    return ResolutionRecord(**defaults)


# ---------------------------------------------------------------------------
# 1. seed_creates_episodes
# ---------------------------------------------------------------------------

def test_seed_creates_episodes():
    """EpisodicMemory seeds 20 demo episodes when storage file doesn't exist."""
    path = _tmp_path()
    mem = EpisodicMemory(storage_path=path)
    assert len(mem._episodes) == 20
    # File was created
    assert os.path.exists(path)
    # Each line is valid JSON with required fields
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    assert len(lines) == 20
    first = json.loads(lines[0])
    assert "episode_id" in first
    assert "failure_signature" in first


# ---------------------------------------------------------------------------
# 2. record_and_retrieve
# ---------------------------------------------------------------------------

def test_record_and_retrieve():
    """record() appends to JSONL and is visible in _episodes."""
    path = _tmp_path()
    # Start fresh — no seeding (non-empty file)
    with open(path, "w") as f:
        pass  # empty but existing file prevents seed
    mem = EpisodicMemory(storage_path=path)
    assert len(mem._episodes) == 0

    ep = _make_episode(incident_id="INC-NEW-1", service="test-service")
    mem.record(ep)

    assert len(mem._episodes) == 1
    assert mem._episodes[0].incident_id == "INC-NEW-1"
    # Verify it's persisted to disk
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["incident_id"] == "INC-NEW-1"


# ---------------------------------------------------------------------------
# 3. search_by_service
# ---------------------------------------------------------------------------

def test_search_by_service():
    """search() filters episodes by service."""
    path = _tmp_path()
    mem = EpisodicMemory(storage_path=path)  # seeded with 20 episodes
    results = mem.search(service="payment-service", limit=10)
    assert len(results) > 0
    for ep in results:
        assert ep.service == "payment-service"


# ---------------------------------------------------------------------------
# 4. search_by_type
# ---------------------------------------------------------------------------

def test_search_by_type():
    """search() filters episodes by incident_type."""
    path = _tmp_path()
    mem = EpisodicMemory(storage_path=path)  # seeded
    results = mem.search(incident_type="error_spike", limit=10)
    assert len(results) > 0
    for ep in results:
        assert ep.incident_type == "error_spike"


# ---------------------------------------------------------------------------
# 5. get_similar_by_signature
# ---------------------------------------------------------------------------

def test_get_similar_by_signature():
    """get_similar() returns episodes ordered by token-overlap similarity."""
    path = _tmp_path()
    mem = EpisodicMemory(storage_path=path)  # seeded

    results = mem.get_similar("postgres connection pool exhausted", limit=3)
    assert 1 <= len(results) <= 3
    # The first result should mention 'postgres' or 'connection' or 'pool'
    sig = results[0].failure_signature.lower()
    assert any(tok in sig for tok in ("postgres", "connection", "pool"))


# ---------------------------------------------------------------------------
# 6. summary_for_service
# ---------------------------------------------------------------------------

def test_summary_for_service():
    """summary_for_service() returns correct aggregate stats."""
    path = _tmp_path()
    mem = EpisodicMemory(storage_path=path)  # seeded with payment-service episodes

    summary = mem.summary_for_service("payment-service")
    assert summary["total_incidents"] > 0
    assert summary["avg_ttresolve_ms"] > 0
    assert summary["most_common_type"] != ""
    assert summary["most_successful_action"] != ""


# ---------------------------------------------------------------------------
# 7. resolution_success_rate
# ---------------------------------------------------------------------------

def test_resolution_success_rate():
    """get_resolution_success_rate() returns correct fraction."""
    path = _tmp_path()
    mem = EpisodicMemory(storage_path=path)  # seeded

    # payment-service has 3x timeout + "increase pool size" → 3 or 4 resolved, 0 failures in seed
    rate = mem.get_resolution_success_rate("timeout", "increase pool size")
    assert 0.0 <= rate <= 1.0
    # There are resolved timeouts with "increase pool size" in seed data
    assert rate > 0.5


# ---------------------------------------------------------------------------
# 8. recommend_returns_top3
# ---------------------------------------------------------------------------

def test_recommend_returns_top3():
    """ResolutionKnowledge.recommend() returns at most 3 recommendations."""
    path = _tmp_path()
    rk = ResolutionKnowledge(storage_path=path)  # seeded with 30 records

    recs = rk.recommend(failure_mode="connection_pool_exhausted", incident_type="timeout")
    assert 1 <= len(recs) <= 3
    for rec in recs:
        assert isinstance(rec, ResolutionRecommendation)
        assert 0.0 <= rec.success_rate <= 1.0
        assert rec.times_tried > 0


# ---------------------------------------------------------------------------
# 9. resolution_leaderboard
# ---------------------------------------------------------------------------

def test_resolution_leaderboard():
    """get_leaderboard() returns actions sorted by success rate for incident type."""
    path = _tmp_path()
    rk = ResolutionKnowledge(storage_path=path)  # seeded

    lb = rk.get_leaderboard("timeout")
    assert len(lb) > 0
    # Should be sorted by success_rate descending
    rates = [entry["success_rate"] for entry in lb]
    assert rates == sorted(rates, reverse=True)
    # Each entry has required fields
    for entry in lb:
        assert "action" in entry
        assert "success_rate" in entry
        assert "times_tried" in entry


# ---------------------------------------------------------------------------
# 10. empty_store_no_crash
# ---------------------------------------------------------------------------

def test_empty_store_no_crash():
    """All public methods on an empty store return sensible defaults, no exceptions."""
    path = _tmp_path()
    # Create a genuinely empty but existing file (prevents seeding)
    with open(path, "w") as f:
        pass

    mem = EpisodicMemory(storage_path=path)
    assert mem.search(service="nonexistent") == []
    assert mem.get_similar("no match here") == []
    assert mem.get_resolution_success_rate("timeout", "restart") == 0.0
    summary = mem.summary_for_service("nonexistent")
    assert summary["total_incidents"] == 0

    rk_path = _tmp_path()
    with open(rk_path, "w") as f:
        pass
    rk = ResolutionKnowledge(storage_path=rk_path)
    assert rk.recommend(failure_mode="unknown", incident_type="unknown") == []
    assert rk.get_leaderboard("unknown") == []
