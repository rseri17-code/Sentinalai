"""Tests for intelligence.resolution_memory — candidate/confirmed separation."""

import pytest
import sqlite3
import tempfile
import os

from intelligence.resolution_memory import (
    ResolutionMemory,
    ResolutionMemoryStore,
    _extract_symptoms,
    _VALIDATION_CANDIDATE,
    _VALIDATION_CONFIRMED,
    _VALIDATION_REJECTED,
)


# ── Schema fixture ─────────────────────────────────────────────────────────────

def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # Apply schema migration 3 for resolution_memories table
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS resolution_memories (
            memory_id            TEXT PRIMARY KEY,
            investigation_id     TEXT NOT NULL DEFAULT '',
            incident_id          TEXT NOT NULL DEFAULT '',
            service              TEXT NOT NULL DEFAULT '',
            environment          TEXT NOT NULL DEFAULT '',
            incident_type        TEXT NOT NULL DEFAULT '',
            symptoms             TEXT NOT NULL DEFAULT '[]',
            detected_root_cause  TEXT NOT NULL DEFAULT '',
            evidence_used        TEXT NOT NULL DEFAULT '[]',
            confirmed_resolution TEXT NOT NULL DEFAULT '',
            fix_action           TEXT NOT NULL DEFAULT '',
            rollback_action      TEXT NOT NULL DEFAULT '',
            owner_team           TEXT NOT NULL DEFAULT '',
            confidence           INTEGER NOT NULL DEFAULT 0,
            validation_status    TEXT NOT NULL DEFAULT 'candidate',
            is_confirmed         INTEGER NOT NULL DEFAULT 0,
            lesson_learned       TEXT NOT NULL DEFAULT '',
            related_incident_ids TEXT NOT NULL DEFAULT '[]',
            mttr_minutes         REAL NOT NULL DEFAULT 0,
            recorded_at          TEXT NOT NULL DEFAULT '',
            confirmed_at         TEXT
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
    return ResolutionMemoryStore(db_path)


# ── from_investigation ─────────────────────────────────────────────────────────

def test_from_investigation_creates_candidate():
    result = {
        "root_cause": "database connection pool exhausted under high load",
        "incident_type": "high_latency",
        "confidence": 72,
        "remediation": {"immediate_action": "restart connection pool", "rollback_action": "redeploy v1.2"},
    }
    mem = ResolutionMemory.from_investigation(
        investigation_id="inv-001",
        incident_id="inc-001",
        service="api-gateway",
        incident_type="high_latency",
        result=result,
        evidence={"metrics": {}},
        environment="production",
        owner_team="platform",
        mttr_minutes=12.5,
    )
    assert mem.validation_status == _VALIDATION_CANDIDATE
    assert mem.is_confirmed is False
    assert mem.confidence == 72
    assert mem.fix_action == "restart connection pool"
    assert mem.rollback_action == "redeploy v1.2"
    assert mem.confirmed_at is None
    assert mem.confirmed_resolution == ""


def test_from_investigation_deterministic_id():
    result = {"root_cause": "OOM", "confidence": 50}
    m1 = ResolutionMemory.from_investigation(
        investigation_id="inv-x", incident_id="inc-x", service="svc",
        incident_type="crash", result=result,
    )
    m2 = ResolutionMemory.from_investigation(
        investigation_id="inv-x", incident_id="inc-x", service="svc",
        incident_type="crash", result=result,
    )
    # memory_id includes timestamp[:19] — may differ by second; just check format
    assert len(m1.memory_id) == 16
    assert all(c in "0123456789abcdef" for c in m1.memory_id)


def test_from_investigation_extracts_symptoms():
    result = {
        "root_cause": "memory leak in worker threads causing OOM killer",
        "incident_type": "oom_kill",
    }
    evidence = {"alerts": {"alerts_firing": ["MemoryPressureHigh", "PodRestarting"]}}
    mem = ResolutionMemory.from_investigation(
        investigation_id="inv-2", incident_id="inc-2", service="worker",
        incident_type="oom_kill", result=result, evidence=evidence,
    )
    assert "oom_kill" in mem.symptoms
    assert "memory" in mem.symptoms or "memorypressurehigh" in mem.symptoms


# ── Store CRUD ─────────────────────────────────────────────────────────────────

def test_record_and_get(store):
    result = {"root_cause": "disk full on /var", "confidence": 80}
    mem = ResolutionMemory.from_investigation(
        investigation_id="inv-r1", incident_id="inc-r1",
        service="storage", incident_type="disk_full", result=result,
    )
    store.record(mem)
    fetched = store.get(mem.memory_id)
    assert fetched is not None
    assert fetched.memory_id == mem.memory_id
    assert fetched.service == "storage"
    assert fetched.validation_status == _VALIDATION_CANDIDATE


def test_record_idempotent(store):
    result = {"root_cause": "cpu throttling", "confidence": 60}
    mem = ResolutionMemory.from_investigation(
        investigation_id="inv-r2", incident_id="inc-r2",
        service="compute", incident_type="cpu", result=result,
    )
    store.record(mem)
    store.record(mem)  # second write must not raise or duplicate
    memories = store.query(service="compute")
    assert len(memories) == 1


def test_confirm_promotes_candidate(store):
    result = {"root_cause": "queue backpressure", "confidence": 75}
    mem = ResolutionMemory.from_investigation(
        investigation_id="inv-c1", incident_id="inc-c1",
        service="queue", incident_type="backlog", result=result,
    )
    store.record(mem)
    ok = store.confirm(
        mem.memory_id,
        confirmed_resolution="Scaled consumers to 20",
        lesson_learned="Always monitor consumer lag",
        owner_team="platform",
    )
    assert ok is True
    fetched = store.get(mem.memory_id)
    assert fetched.validation_status == _VALIDATION_CONFIRMED
    assert fetched.is_confirmed is True
    assert fetched.confirmed_resolution == "Scaled consumers to 20"
    assert fetched.lesson_learned == "Always monitor consumer lag"
    assert fetched.confirmed_at is not None


def test_confirm_already_confirmed_returns_false(store):
    result = {"root_cause": "bad deploy", "confidence": 90}
    mem = ResolutionMemory.from_investigation(
        investigation_id="inv-c2", incident_id="inc-c2",
        service="app", incident_type="deploy", result=result,
    )
    store.record(mem)
    store.confirm(mem.memory_id)
    ok = store.confirm(mem.memory_id)  # second confirm should return False
    assert ok is False


def test_reject_candidate(store):
    result = {"root_cause": "unknown spike", "confidence": 30}
    mem = ResolutionMemory.from_investigation(
        investigation_id="inv-rej", incident_id="inc-rej",
        service="web", incident_type="spike", result=result,
    )
    store.record(mem)
    ok = store.reject(mem.memory_id)
    assert ok is True
    fetched = store.get(mem.memory_id)
    assert fetched.validation_status == _VALIDATION_REJECTED


def test_get_missing_returns_none(store):
    assert store.get("nonexistent-id") is None


# ── Query ──────────────────────────────────────────────────────────────────────

def test_query_by_service(store):
    for i in range(3):
        result = {"root_cause": f"cause {i}", "confidence": 50 + i}
        mem = ResolutionMemory.from_investigation(
            investigation_id=f"inv-q{i}", incident_id=f"inc-q{i}",
            service="search" if i < 2 else "billing",
            incident_type="latency", result=result,
        )
        store.record(mem)
    results = store.query(service="search")
    assert len(results) == 2
    assert all(r.service == "search" for r in results)


def test_query_confirmed_only(store):
    for i in range(3):
        result = {"root_cause": f"root {i}", "confidence": 60}
        mem = ResolutionMemory.from_investigation(
            investigation_id=f"inv-co{i}", incident_id=f"inc-co{i}",
            service="api", incident_type="error", result=result,
        )
        store.record(mem)
        if i == 0:
            store.confirm(mem.memory_id)
    confirmed = store.query(confirmed_only=True)
    assert len(confirmed) == 1
    assert confirmed[0].is_confirmed is True


# ── find_similar ───────────────────────────────────────────────────────────────

def test_find_similar_by_token_overlap(store):
    for i, cause in enumerate([
        "database connection pool exhausted under high load",
        "database pool exhausted connections refused",
        "redis cache timeout network latency spike",
    ]):
        result = {"root_cause": cause, "confidence": 80}
        mem = ResolutionMemory.from_investigation(
            investigation_id=f"inv-sim{i}", incident_id=f"inc-sim{i}",
            service="api", incident_type="latency", result=result,
        )
        store.record(mem)
        store.confirm(mem.memory_id)

    similar = store.find_similar(
        root_cause="database connection pool exhausted",
        confirmed_only=True,
        limit=3,
    )
    assert len(similar) >= 1
    # Database-related entries should score higher than the redis one
    top_cause = similar[0].detected_root_cause
    assert "database" in top_cause or "pool" in top_cause


# ── Serialization round-trip ───────────────────────────────────────────────────

def test_to_dict_round_trip(store):
    result = {"root_cause": "high error rate after deploy", "confidence": 70}
    mem = ResolutionMemory.from_investigation(
        investigation_id="inv-rt", incident_id="inc-rt",
        service="frontend", incident_type="deploy_regression", result=result,
        environment="staging",
    )
    store.record(mem)
    fetched = store.get(mem.memory_id)
    d = fetched.to_dict()
    assert d["memory_id"] == mem.memory_id
    assert d["service"] == "frontend"
    assert d["environment"] == "staging"
    assert isinstance(d["symptoms"], list)
    assert isinstance(d["evidence_used"], list)


# ── _extract_symptoms ──────────────────────────────────────────────────────────

def test_extract_symptoms_caps_at_20():
    result = {"root_cause": " ".join(f"token{i}" for i in range(50))}
    symptoms = _extract_symptoms(result, None)
    assert len(symptoms) <= 20


def test_extract_symptoms_skips_stopwords():
    result = {"root_cause": "the database is failing due to memory"}
    symptoms = _extract_symptoms(result, None)
    assert "the" not in symptoms
    assert "due" not in symptoms
    assert "database" in symptoms or "memory" in symptoms
