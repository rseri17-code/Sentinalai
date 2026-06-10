"""Tests for intelligence.change_tracker — Change entities + deterministic scoring."""

import pytest
import sqlite3
import tempfile
import os
from datetime import datetime, timedelta, timezone

from intelligence.change_tracker import (
    Change,
    ChangeImpactLink,
    ChangeImpactStore,
    score_change_impact,
    _change_id,
    _link_id,
)


def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS changes (
            change_id    TEXT PRIMARY KEY,
            service      TEXT NOT NULL DEFAULT '',
            change_type  TEXT NOT NULL DEFAULT '',
            deployed_at  TEXT NOT NULL DEFAULT '',
            description  TEXT NOT NULL DEFAULT '',
            deployed_by  TEXT NOT NULL DEFAULT '',
            metadata     TEXT NOT NULL DEFAULT '{}',
            recorded_at  TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS change_incident_links (
            link_id          TEXT PRIMARY KEY,
            change_id        TEXT NOT NULL DEFAULT '',
            incident_id      TEXT NOT NULL DEFAULT '',
            investigation_id TEXT NOT NULL DEFAULT '',
            impact_score     REAL NOT NULL DEFAULT 0,
            link_reason      TEXT NOT NULL DEFAULT '',
            linked_at        TEXT NOT NULL DEFAULT ''
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
    return ChangeImpactStore(db_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(h: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


# ── _change_id / _link_id ──────────────────────────────────────────────────────

def test_change_id_deterministic():
    id1 = _change_id("api", "deployment", "2026-01-01T00:00:00+00:00")
    id2 = _change_id("api", "deployment", "2026-01-01T00:00:00+00:00")
    assert id1 == id2
    assert len(id1) == 16


def test_link_id_deterministic():
    id1 = _link_id("change-001", "inc-001")
    id2 = _link_id("change-001", "inc-001")
    assert id1 == id2
    assert len(id1) == 16


# ── score_change_impact ────────────────────────────────────────────────────────

def test_score_critical_window_same_service():
    change = Change(
        change_id="c1", service="api", change_type="deployment",
        deployed_at=_hours_ago(0.5), description="", deployed_by="",
        recorded_at=_now_iso(),
    )
    score, reason = score_change_impact(change, "api", _now_iso())
    assert score > 0.6, f"Expected high score for recent same-service deploy, got {score}"
    assert "deployed" in reason or "same service" in reason


def test_score_outside_window_returns_low():
    change = Change(
        change_id="c2", service="api", change_type="deployment",
        deployed_at=_hours_ago(48.0), description="", deployed_by="",
        recorded_at=_now_iso(),
    )
    score, _ = score_change_impact(change, "api", _now_iso())
    assert score < 0.5, f"Expected low score for 48h-old change, got {score}"


def test_score_different_service_reduces_score():
    change = Change(
        change_id="c3", service="billing", change_type="deployment",
        deployed_at=_hours_ago(0.5), description="", deployed_by="",
        recorded_at=_now_iso(),
    )
    same_svc_score, _ = score_change_impact(change, "billing", _now_iso())
    diff_svc_score, _ = score_change_impact(change, "api", _now_iso())
    assert same_svc_score > diff_svc_score


def test_score_high_risk_change_type_boosts_score():
    dep_change = Change(
        change_id="c4a", service="api", change_type="deployment",
        deployed_at=_hours_ago(0.5), description="", deployed_by="",
        recorded_at=_now_iso(),
    )
    doc_change = Change(
        change_id="c4b", service="api", change_type="documentation",
        deployed_at=_hours_ago(0.5), description="", deployed_by="",
        recorded_at=_now_iso(),
    )
    dep_score, _ = score_change_impact(dep_change, "api", _now_iso())
    doc_score, _ = score_change_impact(doc_change, "api", _now_iso())
    assert dep_score > doc_score


def test_score_dependency_match_boosts_score():
    change = Change(
        change_id="c5", service="database", change_type="schema",
        deployed_at=_hours_ago(0.5), description="", deployed_by="",
        recorded_at=_now_iso(),
    )
    # api depends on database → database is in affected_services of api
    score_with_dep, _ = score_change_impact(change, "api", _now_iso(), affected_services=["database"])
    score_without_dep, _ = score_change_impact(change, "api", _now_iso(), affected_services=[])
    assert score_with_dep > score_without_dep


def test_score_returns_float_between_0_and_1():
    change = Change(
        change_id="c6", service="api", change_type="config",
        deployed_at=_hours_ago(2.0), description="", deployed_by="",
        recorded_at=_now_iso(),
    )
    score, _ = score_change_impact(change, "api", _now_iso())
    assert 0.0 <= score <= 1.0


# ── make_change / record_change ────────────────────────────────────────────────

def test_make_and_record_change(store):
    change = store.make_change(
        service="api", change_type="deployment",
        deployed_at=_hours_ago(1.0),
        description="Deploy v2.3", deployed_by="ci-bot",
    )
    assert len(change.change_id) == 16
    store.record_change(change)
    changes = store.recent_changes(service="api", hours=2.0)
    assert len(changes) == 1
    assert changes[0].description == "Deploy v2.3"


def test_record_change_idempotent(store):
    change = store.make_change(
        service="api", change_type="deployment",
        deployed_at=_hours_ago(0.5),
    )
    store.record_change(change)
    store.record_change(change)
    changes = store.recent_changes(service="api", hours=1.0)
    assert len(changes) == 1


# ── link_to_incident ───────────────────────────────────────────────────────────

def test_link_to_incident(store):
    change = store.make_change("api", "deployment", _hours_ago(0.5))
    store.record_change(change)
    link = store.make_link(change.change_id, "inc-001", "inv-001", 0.75, "same service")
    store.link_to_incident(link)
    linked = store.get_changes_for_investigation("inv-001")
    assert len(linked) == 1
    assert linked[0]["change_id"] == change.change_id
    assert linked[0]["impact_score"] == 0.75


def test_link_to_incident_idempotent(store):
    change = store.make_change("api", "config", _hours_ago(1.0))
    store.record_change(change)
    link = store.make_link(change.change_id, "inc-002", "inv-002", 0.5, "config change")
    store.link_to_incident(link)
    store.link_to_incident(link)
    linked = store.get_changes_for_investigation("inv-002")
    assert len(linked) == 1


def test_get_changes_for_investigation_min_score_filter(store):
    change_a = store.make_change("api", "deployment", _hours_ago(0.3))
    change_b = store.make_change("api", "documentation", _hours_ago(1.5))
    store.record_change(change_a)
    store.record_change(change_b)
    store.link_to_incident(store.make_link(change_a.change_id, "inc-003", "inv-003", 0.8, "critical"))
    store.link_to_incident(store.make_link(change_b.change_id, "inc-003", "inv-003", 0.1, "low"))
    high_only = store.get_changes_for_investigation("inv-003", min_score=0.5)
    assert len(high_only) == 1
    assert high_only[0]["impact_score"] >= 0.5


# ── recent_changes ─────────────────────────────────────────────────────────────

def test_recent_changes_time_window(store):
    store.record_change(store.make_change("api", "deployment", _hours_ago(1.0)))
    store.record_change(store.make_change("api", "config", _hours_ago(25.0)))  # outside 24h
    recent = store.recent_changes(service="api", hours=24.0)
    assert len(recent) == 1


def test_recent_changes_service_filter(store):
    store.record_change(store.make_change("api", "deployment", _hours_ago(1.0)))
    store.record_change(store.make_change("billing", "config", _hours_ago(1.0)))
    api_changes = store.recent_changes(service="api", hours=2.0)
    assert all(c.service == "api" for c in api_changes)


# ── Change.to_dict ─────────────────────────────────────────────────────────────

def test_change_to_dict_round_trip(store):
    change = store.make_change(
        service="frontend", change_type="deployment",
        deployed_at=_hours_ago(2.0),
        description="v3.1 release",
        deployed_by="ci",
        metadata={"pr": "123", "tag": "v3.1"},
    )
    d = change.to_dict()
    assert d["service"] == "frontend"
    assert d["change_type"] == "deployment"
    assert d["metadata"]["pr"] == "123"
    assert "change_id" in d
