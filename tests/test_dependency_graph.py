"""Tests for intelligence.dependency_graph — service topology."""

import pytest
import sqlite3
import tempfile
import os

from intelligence.dependency_graph import (
    ServiceDependency,
    DependencyGraphStore,
    _dep_id,
)


def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS service_dependencies (
            dep_id         TEXT PRIMARY KEY,
            source_service TEXT NOT NULL DEFAULT '',
            target_service TEXT NOT NULL DEFAULT '',
            dep_type       TEXT NOT NULL DEFAULT 'runtime',
            strength       REAL NOT NULL DEFAULT 0.1,
            observed_count INTEGER NOT NULL DEFAULT 1,
            first_seen     TEXT NOT NULL DEFAULT '',
            last_seen      TEXT NOT NULL DEFAULT ''
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
    return DependencyGraphStore(db_path)


# ── _dep_id ────────────────────────────────────────────────────────────────────

def test_dep_id_deterministic():
    id1 = _dep_id("api", "database", "runtime")
    id2 = _dep_id("api", "database", "runtime")
    assert id1 == id2
    assert len(id1) == 16


def test_dep_id_differs_by_type():
    id1 = _dep_id("api", "cache", "runtime")
    id2 = _dep_id("api", "cache", "async")
    assert id1 != id2


# ── record_dependency ──────────────────────────────────────────────────────────

def test_record_dependency_returns_dep_id(store):
    dep_id = store.record_dependency("api", "database", "runtime")
    assert len(dep_id) == 16


def test_record_dependency_increments_count_on_repeat(store):
    store.record_dependency("api", "database", "runtime")
    store.record_dependency("api", "database", "runtime")
    deps = store.get_upstream("api")
    assert len(deps) == 1
    assert deps[0].observed_count == 2


def test_record_dependency_strengthens_on_repeat(store):
    store.record_dependency("api", "database", "runtime", strength_delta=0.1)
    store.record_dependency("api", "database", "runtime", strength_delta=0.1)
    deps = store.get_upstream("api")
    assert deps[0].strength > 0.1  # increased from initial


def test_record_dependency_strength_caps_at_1(store):
    for _ in range(20):
        store.record_dependency("api", "database", "runtime", strength_delta=0.3)
    deps = store.get_upstream("api")
    assert deps[0].strength <= 1.0


# ── get_upstream ───────────────────────────────────────────────────────────────

def test_get_upstream_returns_targets(store):
    store.record_dependency("api", "database", "runtime")
    store.record_dependency("api", "cache", "cache")
    upstream = store.get_upstream("api")
    targets = {d.target_service for d in upstream}
    assert targets == {"database", "cache"}


def test_get_upstream_empty_for_no_deps(store):
    assert store.get_upstream("nonexistent") == []


# ── get_downstream ─────────────────────────────────────────────────────────────

def test_get_downstream_returns_sources(store):
    store.record_dependency("api", "database", "runtime")
    store.record_dependency("worker", "database", "runtime")
    downstream = store.get_downstream("database")
    sources = {d.source_service for d in downstream}
    assert sources == {"api", "worker"}


def test_get_downstream_empty_for_leaf(store):
    store.record_dependency("api", "database", "runtime")
    assert store.get_downstream("api") == []


# ── get_affected_services ──────────────────────────────────────────────────────

def test_get_affected_services_when_database_fails(store):
    store.record_dependency("api", "database", "runtime", strength_delta=0.3)
    store.record_dependency("worker", "database", "runtime", strength_delta=0.1)
    affected = store.get_affected_services("database")
    assert "api" in affected
    assert "worker" in affected
    # api has higher strength — should appear first
    assert affected.index("api") < affected.index("worker")


def test_get_affected_services_empty_for_leaf(store):
    store.record_dependency("api", "database", "runtime")
    assert store.get_affected_services("api") == []


# ── all_dependencies ───────────────────────────────────────────────────────────

def test_all_dependencies_returns_all(store):
    store.record_dependency("a", "b", "runtime")
    store.record_dependency("b", "c", "async")
    deps = store.all_dependencies()
    services = {(d.source_service, d.target_service) for d in deps}
    assert ("a", "b") in services
    assert ("b", "c") in services


def test_all_dependencies_empty_store(store):
    assert store.all_dependencies() == []


# ── Serialization round-trip ───────────────────────────────────────────────────

def test_dependency_to_dict(store):
    store.record_dependency("frontend", "api", "runtime")
    deps = store.get_upstream("frontend")
    d = deps[0].to_dict()
    assert d["source_service"] == "frontend"
    assert d["target_service"] == "api"
    assert d["dep_type"] == "runtime"
    assert "strength" in d
    assert "observed_count" in d
    assert "first_seen" in d
    assert "last_seen" in d
