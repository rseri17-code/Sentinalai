"""MTTI instrumentation tests — decision-acceleration timing.

Proves the MTTI timeline is computed from recorded event timestamps only:
time to first evidence / root cause / owner / recommendation / completion,
with missing milestones left null (never estimated) and cross-workflow
baseline comparison reported NOT_MEASURED.
"""
from __future__ import annotations

import os

os.environ.setdefault("AGUI_AUTH_REQUIRED", "false")

from agui.mtti import compute_mtti, summarize_mtti  # noqa: E402


def _ev(etype, ms):
    return {"event_type": etype, "timestamp_epoch_ms": ms}


def _full_stream():
    return [
        _ev("investigation.started", 1_000),
        _ev("incident.classified", 1_200),
        _ev("tool.called", 1_300),
        _ev("tool.responded", 1_800),      # first evidence
        _ev("tool.responded", 2_400),
        _ev("hypothesis.selected", 3_000),
        _ev("rca.generated", 3_500),       # root cause / owner / recommendation
        _ev("investigation.completed", 4_000),
    ]


class TestComputeMtti:
    def test_segments_from_timestamps(self):
        m = compute_mtti(_full_stream())
        s = m["segments_ms"]
        assert s["time_to_first_evidence_ms"] == 800     # 1800 - 1000
        assert s["time_to_root_cause_ms"] == 2500        # 3500 - 1000
        assert s["time_to_owner_ms"] == 2500             # owner at RCA
        assert s["time_to_recommendation_ms"] == 2500
        assert s["total_ms"] == 3000                     # 4000 - 1000
        assert m["actionable"] is True

    def test_milestones_are_epoch_ms(self):
        m = compute_mtti(_full_stream())
        assert m["milestones"]["started"] == 1_000
        assert m["milestones"]["first_evidence"] == 1_800
        assert m["milestones"]["root_cause"] == 3_500
        assert m["milestones"]["completed"] == 4_000

    def test_missing_milestone_is_null_not_estimated(self):
        # no RCA emitted -> root_cause/owner/recommendation null, not guessed
        stream = [_ev("investigation.started", 1_000),
                  _ev("tool.responded", 1_500),
                  _ev("investigation.completed", 2_000)]
        m = compute_mtti(stream)
        assert m["milestones"]["root_cause"] is None
        assert m["segments_ms"]["time_to_root_cause_ms"] is None
        assert m["segments_ms"]["time_to_first_evidence_ms"] == 500
        assert m["actionable"] is False

    def test_started_falls_back_to_earliest_event(self):
        stream = [_ev("tool.responded", 2_000), _ev("rca.generated", 3_000)]
        m = compute_mtti(stream)
        assert m["milestones"]["started"] == 2_000
        assert m["segments_ms"]["time_to_root_cause_ms"] == 1_000

    def test_hypothesis_selected_is_root_cause_fallback(self):
        stream = [_ev("investigation.started", 100),
                  _ev("hypothesis.selected", 700)]
        m = compute_mtti(stream)
        assert m["milestones"]["root_cause"] == 700
        assert m["segments_ms"]["time_to_root_cause_ms"] == 600

    def test_memory_result_counts_as_evidence(self):
        stream = [_ev("investigation.started", 0),
                  _ev("memory.result", 250),
                  _ev("rca.generated", 900)]
        m = compute_mtti(stream)
        assert m["segments_ms"]["time_to_first_evidence_ms"] == 250

    def test_never_negative(self):
        # completed before started (clock skew) -> null, not negative
        stream = [_ev("investigation.started", 5_000),
                  _ev("investigation.completed", 1_000)]
        m = compute_mtti(stream)
        assert m["segments_ms"]["total_ms"] is None

    def test_empty_stream(self):
        m = compute_mtti([])
        assert m["events_observed"] == 0
        assert all(v is None for v in m["segments_ms"].values())
        assert m["actionable"] is False

    def test_enum_event_type_supported(self):
        class _E:
            value = "rca.generated"
        stream = [{"event_type": "investigation.started", "timestamp_epoch_ms": 0},
                  {"event_type": _E(), "timestamp_epoch_ms": 400}]
        m = compute_mtti(stream)
        assert m["milestones"]["root_cause"] == 400


class TestSummarize:
    def test_median_and_not_measured_baseline(self):
        rows = [compute_mtti(_full_stream()),
                compute_mtti([_ev("investigation.started", 0),
                              _ev("tool.responded", 1_000),
                              _ev("rca.generated", 4_000),
                              _ev("investigation.completed", 5_000)])]
        summ = summarize_mtti(rows)
        assert summ["investigations"] == 2
        # medians of [2500, 4000] = 3250 for root cause
        assert summ["median"]["time_to_root_cause_ms"] == 3250.0
        # honest: no baseline comparison without a controlled pilot arm
        assert summ["baseline_comparison"] == "NOT_MEASURED"

    def test_summary_ignores_null_segments(self):
        rows = [compute_mtti([_ev("investigation.started", 0),
                              _ev("investigation.completed", 100)])]
        summ = summarize_mtti(rows)
        assert summ["median"]["time_to_root_cause_ms"] is None
        assert summ["median"]["total_ms"] == 100.0
