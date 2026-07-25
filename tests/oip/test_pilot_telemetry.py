"""OIP pilot instrumentation — operator-event recorder tests.

Verifies the produce-only pilot telemetry: deterministic event records with
caller-supplied timestamps, append-only persistence, round-trip load, and a
reporting rollup — with no wall-clock, no scoring, and no new architecture.
"""
from __future__ import annotations

import json

import pytest

from sentinel_core.oip import pilot_telemetry as pt


def test_event_is_deterministic_and_json_safe():
    a = pt.pilot_event("operator_interaction", at="2026-07-06T09:00:00",
                       operator="alice", surface="daily_operations_brief")
    b = pt.pilot_event("operator_interaction", at="2026-07-06T09:00:00",
                       operator="alice", surface="daily_operations_brief")
    assert a == b
    assert a == json.loads(json.dumps(a))
    assert a["event_id"] == b["event_id"]


def test_event_id_changes_with_content():
    a = pt.pilot_event("operator_interaction", at="2026-07-06T09:00:00",
                       operator="alice", surface="operational_health")
    b = pt.pilot_event("operator_interaction", at="2026-07-06T09:00:00",
                       operator="bob", surface="operational_health")
    assert a["event_id"] != b["event_id"]


def test_rejects_unknown_kind():
    with pytest.raises(ValueError):
        pt.pilot_event("bogus", at="t", operator="alice")


def test_rejects_unknown_surface():
    with pytest.raises(ValueError):
        pt.pilot_event("operator_interaction", at="t", operator="alice",
                       surface="not_a_surface")


def test_all_required_event_kinds_supported():
    assert set(pt.EVENT_KINDS) == {
        "operator_interaction", "recommendation_usage", "operator_feedback"}


def test_append_load_roundtrip(tmp_path):
    path = str(tmp_path / "pilot" / "events.jsonl")
    e1 = pt.pilot_event("operator_interaction", at="2026-07-06T09:00:00",
                        operator="alice", surface="service_reliability",
                        incident_id="INC-1")
    e2 = pt.pilot_event("recommendation_usage", at="2026-07-06T09:05:00",
                        operator="alice", surface="service_reliability",
                        incident_id="INC-1", payload={"action": "followed"})
    pt.append_event(path, e1)
    pt.append_event(path, e2)
    loaded = pt.load_events(path)
    assert loaded == [e1, e2]


def test_load_missing_file_is_empty():
    assert pt.load_events("/nonexistent/pilot/events.jsonl") == []


def test_summarize_counts_and_acceptance():
    events = [
        pt.pilot_event("operator_interaction", at="t1", operator="a",
                       surface="daily_operations_brief"),
        pt.pilot_event("operator_interaction", at="t2", operator="a",
                       surface="operational_health"),
        pt.pilot_event("recommendation_usage", at="t3", operator="a",
                       surface="incident_trends", payload={"action": "followed"}),
        pt.pilot_event("recommendation_usage", at="t4", operator="a",
                       surface="incident_trends", payload={"action": "followed"}),
        pt.pilot_event("recommendation_usage", at="t5", operator="a",
                       surface="incident_trends", payload={"action": "dismissed"}),
        pt.pilot_event("operator_feedback", at="t6", operator="a",
                       payload={"trust": 4}),
    ]
    s = pt.summarize(events)
    assert s["events"] == 6
    assert s["by_kind"]["recommendation_usage"] == 3
    assert s["by_surface"]["incident_trends"] == 3
    assert s["recommendation_followed"] == 2
    assert s["recommendation_dismissed"] == 1
    assert s["recommendation_acceptance_rate"] == round(2 / 3, 4)


def test_summarize_no_decisions_acceptance_none():
    events = [pt.pilot_event("operator_feedback", at="t", operator="a",
                             payload={"clarity": 5})]
    assert pt.summarize(events)["recommendation_acceptance_rate"] is None


def test_not_exported_as_oip_service():
    # instrumentation, not a sixth OIP service — must not leak into __all__.
    import sentinel_core.oip as oip
    assert "pilot_telemetry" not in oip.__all__
    assert "pilot_event" not in oip.__all__


def test_no_wall_clock_or_scoring():
    import inspect
    src = inspect.getsource(pt)
    # timestamps are caller-supplied; no clock, no randomness, no scoring
    for banned in ("time.time", "datetime.now", "utcnow", "monotonic",
                   "random", "def _score", "compute_confidence"):
        assert banned not in src
