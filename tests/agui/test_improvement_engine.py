"""Operational Improvement Engine tests.

Proves the analysis logic with synthetic *test* fixtures (legitimate test data,
not fabricated findings): friction detection, root-cause classification, ROI
ranking by observed seconds-saveable, before/after impact, and the honest
NOT_MEASURED path when there is not enough pilot data.
"""
from __future__ import annotations

import os

os.environ.setdefault("AGUI_AUTH_REQUIRED", "false")

from agui import operator_telemetry as ot            # noqa: E402
from agui.improvement_engine import analyze, compare_before_after  # noqa: E402


def _ev(inv, milestone, at, **kw):
    return ot.operator_event(milestone, at=at, operator="op",
                             investigation_id=inv, **kw)


def _session(inv, *, evidence_first=800, understanding=2000, confidence=1500,
             escapes=None, repeats=0):
    base = 1_000
    evs = [_ev(inv, "investigation_opened", base)]
    evs.append(_ev(inv, "evidence_panel_opened", base + evidence_first))
    for r in range(repeats):
        evs.append(_ev(inv, "evidence_item_expanded", base + evidence_first + 50 + r))
    evs.append(_ev(inv, "confidence_viewed", base + confidence))
    evs.append(_ev(inv, "recommendation_viewed", base + understanding))
    for tool, away, reason in (escapes or []):
        evs.append(_ev(inv, "external_tool_opened", base + 300,
                       tool_name=tool, time_away_ms=away, reason=reason))
    evs.append(_ev(inv, "investigation_completed", base + understanding + 500))
    return evs


def _pilot(n=6, **kw):
    events = []
    for i in range(n):
        events += _session(f"INV-{i}", **kw)
    return events


class TestNotMeasured:
    def test_empty_is_not_measured(self):
        r = analyze([])
        assert r["status"] == "NOT_MEASURED"
        assert r["sessions"] == 0

    def test_below_threshold_is_not_measured(self):
        r = analyze(_pilot(n=3), min_sessions=5)
        assert r["status"] == "NOT_MEASURED"
        assert "3 session" in r["reason"]


class TestAnalysis:
    def test_measured_with_enough_sessions(self):
        r = analyze(_pilot(n=6))
        assert r["status"] == "measured"
        assert r["sessions"] == 6
        assert r["underpowered"] is True     # 6 < 30

    def test_escape_becomes_ranked_backlog_item(self):
        events = _pilot(n=6, escapes=[("Splunk", 40_000, "need raw logs")])
        r = analyze(events)
        splunk = [b for b in r["backlog"] if "Splunk" in b["signal"]]
        assert splunk, "escape should produce a backlog item"
        item = splunk[0]
        assert item["root_cause"] == "missing_evidence"
        assert item["seconds_saveable"] == 240.0   # 6 sessions * 40s
        assert item["evidence"]["reasons"] == ["need raw logs"]

    def test_ranking_by_seconds_saveable(self):
        # a big escape (heavy time away) should outrank a small one
        events = []
        for i in range(6):
            events += _session(
                f"INV-{i}",
                escapes=[("Splunk", 60_000, "logs"), ("GitHub", 5_000, "deploy")])
        r = analyze(events)
        tools = [b["signal"] for b in r["backlog"] if "escape" in b["signal"]]
        assert tools.index("external_tool_escape:Splunk") < \
            tools.index("external_tool_escape:GitHub")

    def test_repeated_evidence_is_navigation_friction(self):
        r = analyze(_pilot(n=6, repeats=3))
        nav = [b for b in r["backlog"] if b["signal"] == "repeated_evidence_lookups"]
        assert nav and nav[0]["root_cause"] == "poor_navigation"

    def test_every_backlog_item_has_evidence(self):
        r = analyze(_pilot(n=6, escapes=[("Grafana", 10_000, "metrics")], repeats=2))
        assert r["backlog"]
        for item in r["backlog"]:
            assert item["evidence"]          # never an unevidenced recommendation
            assert item["root_cause"] in (
                "missing_evidence", "poor_navigation", "poor_visualization",
                "low_confidence", "missing_ownership", "missing_recommendation",
                "missing_context")

    def test_effort_is_declared_not_multiplied_into_impact(self):
        r = analyze(_pilot(n=6, escapes=[("Splunk", 40_000, "logs")]))
        item = next(b for b in r["backlog"] if "Splunk" in b["signal"])
        # seconds_saveable derives purely from observed time away × frequency
        assert item["seconds_saveable"] == 240.0
        assert item["effort"] in ("low", "medium", "high", "unknown")


class TestBeforeAfter:
    def test_improved_when_segment_drops(self):
        before = analyze(_pilot(n=6, understanding=4000))
        after = analyze(_pilot(n=6, understanding=2000))
        cmp = compare_before_after(before, after)
        assert cmp["verdict"] == "IMPROVED"
        assert cmp["deltas_ms"]["time_to_understanding_ms"] > 0

    def test_no_impact_when_unchanged(self):
        before = analyze(_pilot(n=6))
        after = analyze(_pilot(n=6))
        assert compare_before_after(before, after)["verdict"] == "NO_IMPACT"
