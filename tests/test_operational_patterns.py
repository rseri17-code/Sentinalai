"""Tests for intelligence.pattern_intelligence — OperationalPattern + PatternIntelligenceStore."""

import pytest
import sqlite3
import tempfile
import os

from intelligence.pattern_intelligence import (
    OperationalPattern,
    PatternIntelligenceStore,
    _canonical_tokens,
    _symptom_signature,
)


# ── Schema fixture ─────────────────────────────────────────────────────────────

def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operational_patterns (
            pattern_id          TEXT PRIMARY KEY,
            symptom_signature   TEXT NOT NULL DEFAULT '',
            incident_type       TEXT NOT NULL DEFAULT '',
            services            TEXT NOT NULL DEFAULT '[]',
            canonical_symptoms  TEXT NOT NULL DEFAULT '[]',
            occurrence_count    INTEGER NOT NULL DEFAULT 1,
            success_count       INTEGER NOT NULL DEFAULT 0,
            first_seen          TEXT NOT NULL DEFAULT '',
            last_seen           TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def db_path():
    path = _make_db()
    yield path
    os.unlink(path)


@pytest.fixture()
def store(db_path):
    return PatternIntelligenceStore(db_path)


# ── canonical_tokens ───────────────────────────────────────────────────────────

def test_canonical_tokens_removes_stopwords():
    tokens = _canonical_tokens("the database is failing due to memory pressure")
    assert "the" not in tokens
    assert "due" not in tokens
    assert "database" in tokens
    assert "memory" in tokens
    assert "pressure" in tokens


def test_canonical_tokens_deduplicates():
    tokens = _canonical_tokens("memory memory memory pressure pressure")
    assert tokens.count("memory") == 1
    assert tokens.count("pressure") == 1


def test_canonical_tokens_sorted():
    tokens = _canonical_tokens("zebra apple mango")
    assert tokens == sorted(tokens)


def test_canonical_tokens_caps_at_30():
    text = " ".join(f"token{i:04d}" for i in range(50))
    tokens = _canonical_tokens(text)
    assert len(tokens) <= 30


def test_canonical_tokens_strips_punctuation():
    tokens = _canonical_tokens("database, (connection) [pool] exhausted!")
    assert "database" in tokens
    assert "connection" in tokens
    assert "pool" in tokens
    for tok in tokens:
        assert not any(c in tok for c in ".,;:()[]!\"'")


# ── _symptom_signature ─────────────────────────────────────────────────────────

def test_symptom_signature_deterministic():
    tokens = ["connection", "database", "exhausted", "pool"]
    s1 = _symptom_signature(tokens)
    s2 = _symptom_signature(tokens)
    assert s1 == s2
    assert len(s1) == 16


def test_symptom_signature_order_sensitive():
    s1 = _symptom_signature(["alpha", "beta"])
    s2 = _symptom_signature(["beta", "alpha"])
    assert s1 != s2


# ── record_occurrence ──────────────────────────────────────────────────────────

def test_record_occurrence_returns_pattern_id(store):
    pid = store.record_occurrence("high_latency", "database connection pool exhausted", "api", True)
    assert len(pid) == 16
    assert all(c in "0123456789abcdef" for c in pid)


def test_record_occurrence_same_root_cause_increments_count(store):
    root_cause = "memory leak in worker threads"
    pid1 = store.record_occurrence("oom", root_cause, "worker", True)
    pid2 = store.record_occurrence("oom", root_cause, "worker", False)
    pid3 = store.record_occurrence("oom", root_cause, "worker", True)
    assert pid1 == pid2 == pid3  # same signature → same pattern_id
    p = store.get(pid1)
    assert p is not None
    assert p.occurrence_count == 3


def test_record_occurrence_tracks_success(store):
    root_cause = "disk full on data partition"
    store.record_occurrence("disk_full", root_cause, "storage", True)
    store.record_occurrence("disk_full", root_cause, "storage", False)
    pid = store.record_occurrence("disk_full", root_cause, "storage", True)
    p = store.get(pid)
    assert p.success_count == 2
    assert p.occurrence_count == 3
    assert abs(p.success_rate - 2 / 3) < 0.01


def test_record_occurrence_empty_root_cause_returns_empty(store):
    pid = store.record_occurrence("latency", "", "api", True)
    assert pid == ""


# ── get ────────────────────────────────────────────────────────────────────────

def test_get_returns_none_for_missing(store):
    assert store.get("does-not-exist") is None


def test_get_returns_pattern(store):
    pid = store.record_occurrence("cpu_spike", "high cpu from runaway process", "compute", True)
    p = store.get(pid)
    assert p is not None
    assert p.pattern_id == pid
    assert p.incident_type == "cpu_spike"


# ── query ──────────────────────────────────────────────────────────────────────

def test_query_by_incident_type(store):
    store.record_occurrence("latency", "slow database queries", "api", True)
    store.record_occurrence("latency", "network saturation spike", "api", True)
    store.record_occurrence("crash", "null pointer in request handler", "api", False)

    results = store.query(incident_type="latency")
    assert len(results) == 2
    assert all(r.incident_type == "latency" for r in results)


def test_query_min_occurrences_filter(store):
    root = "repeated database error under load"
    for _ in range(3):
        store.record_occurrence("latency", root, "svc", True)
    store.record_occurrence("latency", "single occurrence error", "svc", False)

    results = store.query(min_occurrences=2)
    assert all(r.occurrence_count >= 2 for r in results)


def test_query_empty_returns_no_results(store):
    results = store.query()
    assert results == []


# ── find_similar ───────────────────────────────────────────────────────────────

def test_find_similar_returns_high_overlap_patterns(store):
    store.record_occurrence("latency", "database connection pool exhausted under load", "api", True)
    store.record_occurrence("latency", "redis cache timeout network issue", "api", True)

    similar = store.find_similar("database connection pool saturated")
    assert len(similar) >= 1
    assert "database" in similar[0].canonical_symptoms or "connection" in similar[0].canonical_symptoms


def test_find_similar_returns_empty_on_no_match(store):
    store.record_occurrence("latency", "database pool exhausted", "api", True)
    similar = store.find_similar("kubernetes pod eviction oom")
    assert isinstance(similar, list)


def test_find_similar_min_jaccard_threshold(store):
    store.record_occurrence("latency", "memory pressure causing oom kill", "worker", True)
    similar = store.find_similar("network latency spike timeout", min_jaccard=0.9)
    assert similar == []


# ── OperationalPattern.to_dict ─────────────────────────────────────────────────

def test_pattern_to_dict_includes_success_rate(store):
    root_cause = "high error rate from upstream dependency"
    store.record_occurrence("dependency_failure", root_cause, "api", True)
    store.record_occurrence("dependency_failure", root_cause, "api", False)
    pid = store.record_occurrence("dependency_failure", root_cause, "api", True)
    p = store.get(pid)
    d = p.to_dict()
    assert "success_rate" in d
    assert 0.0 <= d["success_rate"] <= 1.0
    assert d["occurrence_count"] == 3
    assert isinstance(d["canonical_symptoms"], list)
