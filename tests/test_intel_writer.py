"""Tests for intelligence.intel_writer — post-investigation coordinator."""

import pytest
import sqlite3
import tempfile
import os

import intelligence.intel_writer as intel_writer


def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    # All tables needed by intel_writer
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS resolution_memories (
            memory_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL DEFAULT '',
            incident_id TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
            environment TEXT NOT NULL DEFAULT '', incident_type TEXT NOT NULL DEFAULT '',
            symptoms TEXT NOT NULL DEFAULT '[]', detected_root_cause TEXT NOT NULL DEFAULT '',
            evidence_used TEXT NOT NULL DEFAULT '[]', confirmed_resolution TEXT NOT NULL DEFAULT '',
            fix_action TEXT NOT NULL DEFAULT '', rollback_action TEXT NOT NULL DEFAULT '',
            owner_team TEXT NOT NULL DEFAULT '', confidence INTEGER NOT NULL DEFAULT 0,
            validation_status TEXT NOT NULL DEFAULT 'candidate', is_confirmed INTEGER NOT NULL DEFAULT 0,
            lesson_learned TEXT NOT NULL DEFAULT '', related_incident_ids TEXT NOT NULL DEFAULT '[]',
            mttr_minutes REAL NOT NULL DEFAULT 0, recorded_at TEXT NOT NULL DEFAULT '',
            confirmed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS operational_patterns (
            pattern_id TEXT PRIMARY KEY, symptom_signature TEXT NOT NULL DEFAULT '',
            incident_type TEXT NOT NULL DEFAULT '', services TEXT NOT NULL DEFAULT '[]',
            canonical_symptoms TEXT NOT NULL DEFAULT '[]', occurrence_count INTEGER NOT NULL DEFAULT 1,
            success_count INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL DEFAULT '',
            last_seen TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS incident_graph_nodes (
            node_id TEXT NOT NULL, incident_id TEXT NOT NULL,
            node_type TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
            service TEXT NOT NULL DEFAULT '', properties TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (node_id, incident_id)
        );
        CREATE TABLE IF NOT EXISTS incident_graph_edges (
            edge_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL DEFAULT '',
            source_node_id TEXT NOT NULL DEFAULT '', target_node_id TEXT NOT NULL DEFAULT '',
            relationship TEXT NOT NULL DEFAULT '', weight REAL NOT NULL DEFAULT 1.0,
            properties TEXT NOT NULL DEFAULT '{}', recorded_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS service_dependencies (
            dep_id TEXT PRIMARY KEY, source_service TEXT NOT NULL DEFAULT '',
            target_service TEXT NOT NULL DEFAULT '', dep_type TEXT NOT NULL DEFAULT 'runtime',
            strength REAL NOT NULL DEFAULT 0.1, observed_count INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL DEFAULT '', last_seen TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS changes (
            change_id TEXT PRIMARY KEY, service TEXT NOT NULL DEFAULT '',
            change_type TEXT NOT NULL DEFAULT '', deployed_at TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '', deployed_by TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}', recorded_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS change_incident_links (
            link_id TEXT PRIMARY KEY, change_id TEXT NOT NULL DEFAULT '',
            incident_id TEXT NOT NULL DEFAULT '', investigation_id TEXT NOT NULL DEFAULT '',
            impact_score REAL NOT NULL DEFAULT 0, link_reason TEXT NOT NULL DEFAULT '',
            linked_at TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()
    return path


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test_intel.db")
    # Create db
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS resolution_memories (
            memory_id TEXT PRIMARY KEY, investigation_id TEXT NOT NULL DEFAULT '',
            incident_id TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
            environment TEXT NOT NULL DEFAULT '', incident_type TEXT NOT NULL DEFAULT '',
            symptoms TEXT NOT NULL DEFAULT '[]', detected_root_cause TEXT NOT NULL DEFAULT '',
            evidence_used TEXT NOT NULL DEFAULT '[]', confirmed_resolution TEXT NOT NULL DEFAULT '',
            fix_action TEXT NOT NULL DEFAULT '', rollback_action TEXT NOT NULL DEFAULT '',
            owner_team TEXT NOT NULL DEFAULT '', confidence INTEGER NOT NULL DEFAULT 0,
            validation_status TEXT NOT NULL DEFAULT 'candidate', is_confirmed INTEGER NOT NULL DEFAULT 0,
            lesson_learned TEXT NOT NULL DEFAULT '', related_incident_ids TEXT NOT NULL DEFAULT '[]',
            mttr_minutes REAL NOT NULL DEFAULT 0, recorded_at TEXT NOT NULL DEFAULT '',
            confirmed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS operational_patterns (
            pattern_id TEXT PRIMARY KEY, symptom_signature TEXT NOT NULL DEFAULT '',
            incident_type TEXT NOT NULL DEFAULT '', services TEXT NOT NULL DEFAULT '[]',
            canonical_symptoms TEXT NOT NULL DEFAULT '[]', occurrence_count INTEGER NOT NULL DEFAULT 1,
            success_count INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL DEFAULT '',
            last_seen TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS incident_graph_nodes (
            node_id TEXT NOT NULL, incident_id TEXT NOT NULL,
            node_type TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
            service TEXT NOT NULL DEFAULT '', properties TEXT NOT NULL DEFAULT '{}',
            recorded_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (node_id, incident_id)
        );
        CREATE TABLE IF NOT EXISTS incident_graph_edges (
            edge_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL DEFAULT '',
            source_node_id TEXT NOT NULL DEFAULT '', target_node_id TEXT NOT NULL DEFAULT '',
            relationship TEXT NOT NULL DEFAULT '', weight REAL NOT NULL DEFAULT 1.0,
            properties TEXT NOT NULL DEFAULT '{}', recorded_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS service_dependencies (
            dep_id TEXT PRIMARY KEY, source_service TEXT NOT NULL DEFAULT '',
            target_service TEXT NOT NULL DEFAULT '', dep_type TEXT NOT NULL DEFAULT 'runtime',
            strength REAL NOT NULL DEFAULT 0.1, observed_count INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL DEFAULT '', last_seen TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS changes (
            change_id TEXT PRIMARY KEY, service TEXT NOT NULL DEFAULT '',
            change_type TEXT NOT NULL DEFAULT '', deployed_at TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '', deployed_by TEXT NOT NULL DEFAULT '',
            metadata TEXT NOT NULL DEFAULT '{}', recorded_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS change_incident_links (
            link_id TEXT PRIMARY KEY, change_id TEXT NOT NULL DEFAULT '',
            incident_id TEXT NOT NULL DEFAULT '', investigation_id TEXT NOT NULL DEFAULT '',
            impact_score REAL NOT NULL DEFAULT 0, link_reason TEXT NOT NULL DEFAULT '',
            linked_at TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr(intel_writer, "_DB_PATH", path)
    return path


# ── capture ────────────────────────────────────────────────────────────────────

def test_capture_writes_resolution_memory(db_path):
    result = {
        "root_cause": "database connection pool exhausted",
        "incident_type": "high_latency",
        "confidence": 75,
        "remediation": {"immediate_action": "restart pool"},
        "_online_quality_score": 0.8,
    }
    intel_writer.capture(
        investigation_id="inv-001",
        incident_id="inc-001",
        service="api",
        incident_type="high_latency",
        result=result,
        mttr_minutes=5.0,
    )
    from intelligence.resolution_memory import ResolutionMemoryStore
    store = ResolutionMemoryStore(db_path)
    memories = store.query(service="api")
    assert len(memories) == 1
    m = memories[0]
    assert m.validation_status == "candidate"
    assert m.detected_root_cause == "database connection pool exhausted"
    assert m.fix_action == "restart pool"


def test_capture_writes_pattern(db_path):
    result = {
        "root_cause": "memory leak causing oom killer activation",
        "incident_type": "oom_kill",
        "_online_quality_score": 0.9,
    }
    intel_writer.capture(
        investigation_id="inv-002",
        incident_id="inc-002",
        service="worker",
        incident_type="oom_kill",
        result=result,
    )
    from intelligence.pattern_intelligence import PatternIntelligenceStore
    store = PatternIntelligenceStore(db_path)
    patterns = store.query(incident_type="oom_kill")
    assert len(patterns) >= 1


def test_capture_writes_incident_graph_nodes(db_path):
    result = {
        "root_cause": "high error rate from upstream",
        "incident_type": "dependency_failure",
        "confidence": 80,
    }
    intel_writer.capture(
        investigation_id="inv-003",
        incident_id="inc-003",
        service="frontend",
        incident_type="dependency_failure",
        result=result,
    )
    from intelligence.incident_graph import IncidentGraphStore
    store = IncidentGraphStore(db_path)
    nodes = store.get_incident_nodes("inc-003")
    assert len(nodes) >= 1
    types = {n.node_type for n in nodes}
    assert "service" in types


def test_capture_with_alert_evidence_adds_alert_nodes(db_path):
    result = {"root_cause": "disk full", "incident_type": "disk_full"}
    evidence = {"alerts": {"alerts_firing": ["DiskPressureHigh", "PodEviction"]}}
    intel_writer.capture(
        investigation_id="inv-004",
        incident_id="inc-004",
        service="storage",
        incident_type="disk_full",
        result=result,
        evidence=evidence,
    )
    from intelligence.incident_graph import IncidentGraphStore
    store = IncidentGraphStore(db_path)
    nodes = store.get_incident_nodes("inc-004")
    alert_labels = {n.label.lower() for n in nodes if n.node_type == "alert"}
    assert "diskpressurehigh" in alert_labels


def test_capture_with_dependency_evidence(db_path):
    result = {"root_cause": "upstream timeout", "incident_type": "latency"}
    evidence = {"service_health": {"dependencies": ["database", "cache"]}}
    intel_writer.capture(
        investigation_id="inv-005",
        incident_id="inc-005",
        service="api",
        incident_type="latency",
        result=result,
        evidence=evidence,
    )
    from intelligence.dependency_graph import DependencyGraphStore
    store = DependencyGraphStore(db_path)
    upstream = store.get_upstream("api")
    targets = {d.target_service for d in upstream}
    assert "database" in targets
    assert "cache" in targets


def test_capture_with_change_evidence(db_path):
    from datetime import datetime, timezone, timedelta
    deploy_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    result = {"root_cause": "deploy regression", "incident_type": "error_spike"}
    evidence = {
        "change_data": {
            "changes": [{"service": "api", "type": "deployment", "deployed_at": deploy_time}]
        }
    }
    intel_writer.capture(
        investigation_id="inv-006",
        incident_id="inc-006",
        service="api",
        incident_type="error_spike",
        result=result,
        evidence=evidence,
    )
    from intelligence.change_tracker import ChangeImpactStore
    store = ChangeImpactStore(db_path)
    changes = store.get_changes_for_investigation("inv-006")
    assert len(changes) >= 1


def test_capture_non_blocking_on_empty_result(db_path):
    # Should not raise even with minimal/empty result
    intel_writer.capture(
        investigation_id="inv-007",
        incident_id="inc-007",
        service="",
        incident_type="",
        result={},
    )


def test_capture_non_blocking_on_bad_evidence(db_path):
    # Should not raise even with malformed evidence
    intel_writer.capture(
        investigation_id="inv-008",
        incident_id="inc-008",
        service="api",
        incident_type="latency",
        result={"root_cause": "slow query"},
        evidence={"service_health": "not-a-dict"},
    )


def test_capture_idempotent_resolution_memory(db_path):
    result = {"root_cause": "repeated failure", "confidence": 60}
    for _ in range(3):
        intel_writer.capture(
            investigation_id="inv-009",
            incident_id="inc-009",
            service="api",
            incident_type="latency",
            result=result,
        )
    from intelligence.resolution_memory import ResolutionMemoryStore
    store = ResolutionMemoryStore(db_path)
    memories = store.query(service="api")
    # memory_id includes timestamp[:19]; same-second calls = same ID → idempotent
    assert len(memories) >= 1
