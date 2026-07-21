"""Operator-timeline telemetry tests — how long the OPERATOR needed.

Proves the operator journey is measured from recorded interaction timestamps,
kept separate from system MTTI, with missing milestones null (never estimated),
plus external-tool escape analysis, decision quality, and an honest
NOT_MEASURED baseline delta.
"""
from __future__ import annotations

import os

os.environ.setdefault("AGUI_AUTH_REQUIRED", "false")

import pytest  # noqa: E402

from agui import operator_telemetry as ot  # noqa: E402


def _ev(milestone, at, **kw):
    return ot.operator_event(milestone, at=at, operator="alice",
                             investigation_id="INV-1", **kw)


def _journey():
    return [
        _ev("investigation_opened", 1_000),
        _ev("evidence_panel_opened", 1_500),
        _ev("evidence_item_expanded", 1_800),   # first useful evidence
        _ev("graph_opened", 2_000),
        _ev("confidence_viewed", 2_500),
        _ev("owner_viewed", 2_700),
        _ev("recommendation_viewed", 3_000),    # understanding
        _ev("recommendation_accepted", 3_400),  # decision
        _ev("next_action_started", 3_600),
        _ev("investigation_completed", 4_000),
    ]


class TestOperatorEvent:
    def test_reuses_pilot_telemetry_kind(self):
        e = _ev("investigation_opened", 1_000)
        assert e["kind"] == "operator_interaction"       # no new framework
        assert e["payload"]["milestone"] == "investigation_opened"
        assert e["payload"]["at_ms"] == 1_000
        assert e["incident_id"] == "INV-1"

    def test_rejects_unknown_milestone(self):
        with pytest.raises(ValueError):
            _ev("teleported", 1)

    def test_deterministic(self):
        assert _ev("graph_opened", 5) == _ev("graph_opened", 5)


class TestOperatorMtti:
    def test_operator_segments(self):
        m = ot.compute_operator_mtti(_journey())
        s = m["operator_segments_ms"]
        assert s["time_to_first_useful_evidence_ms"] == 800   # 1800 - 1000
        assert s["time_to_confidence_ms"] == 1500             # 2500 - 1000
        assert s["time_to_understanding_ms"] == 2000          # 3000 - 1000
        assert s["time_to_decision_ms"] == 2400               # 3400 - 1000
        assert s["time_to_next_action_ms"] == 2600            # 3600 - 1000
        assert s["total_ms"] == 3000                          # 4000 - 1000

    def test_first_evidence_prefers_expanded_over_panel(self):
        evs = [_ev("investigation_opened", 0),
               _ev("evidence_panel_opened", 500),
               _ev("evidence_item_expanded", 200)]  # earlier expand wins
        m = ot.compute_operator_mtti(evs)
        assert m["operator_segments_ms"]["time_to_first_useful_evidence_ms"] == 200

    def test_missing_milestone_null_not_estimated(self):
        evs = [_ev("investigation_opened", 0), _ev("evidence_panel_opened", 400)]
        m = ot.compute_operator_mtti(evs)
        assert m["operator_segments_ms"]["time_to_decision_ms"] is None
        assert m["milestones"]["decision"] is None

    def test_resumed_counts_as_opened(self):
        evs = [_ev("investigation_resumed", 100),
               _ev("recommendation_viewed", 600)]
        m = ot.compute_operator_mtti(evs)
        assert m["milestones"]["opened"] == 100
        assert m["operator_segments_ms"]["time_to_understanding_ms"] == 500

    def test_never_negative(self):
        evs = [_ev("investigation_opened", 5_000),
               _ev("investigation_completed", 1_000)]
        assert ot.compute_operator_mtti(evs)["operator_segments_ms"]["total_ms"] is None

    def test_empty(self):
        m = ot.compute_operator_mtti([])
        assert m["events_observed"] == 0
        assert all(v is None for v in m["operator_segments_ms"].values())


class TestEscapeAnalysis:
    def test_external_tool_escapes(self):
        evs = [
            _ev("external_tool_opened", 2_000, tool_name="Splunk",
                reason="need raw logs", time_away_ms=45_000),
            _ev("external_tool_opened", 3_000, tool_name="Splunk",
                reason="need raw logs", time_away_ms=30_000),
            _ev("external_tool_opened", 3_500, tool_name="GitHub",
                reason="check deploy", time_away_ms=20_000),
        ]
        esc = ot.external_tool_escapes(evs)
        assert esc["escapes"] == 3
        assert esc["total_time_away_ms"] == 95_000
        assert esc["by_tool"]["Splunk"]["count"] == 2
        assert esc["by_tool"]["Splunk"]["time_away_ms"] == 75_000
        assert "need raw logs" in esc["by_tool"]["Splunk"]["reasons"]


class TestDecisionQuality:
    def test_acceptance_rate(self):
        evs = [_ev("recommendation_accepted", 1),
               _ev("recommendation_accepted", 2),
               _ev("recommendation_rejected", 3)]
        dq = ot.decision_quality(evs)
        assert dq["recommendation_accepted"] == 2
        assert dq["recommendation_rejected"] == 1
        assert dq["acceptance_rate"] == round(2 / 3, 4)

    def test_no_decisions_rate_none(self):
        assert ot.decision_quality([])["acceptance_rate"] is None


class TestBaseline:
    def test_not_measured_without_baseline(self):
        assert ot.baseline_delta(None, 5000)["status"] == "NOT_MEASURED"

    def test_seconds_saved_when_both_present(self):
        d = ot.baseline_delta(120_000, 45_000)
        assert d["status"] == "measured"
        assert d["seconds_saved"] == 75.0
