"""Tests for the three remaining harness gaps:

Gap 1: Blast radius history — predicted vs actual persistence + Jaccard accuracy
Gap 2: Cascade tracker — multi-hop propagation chains
Gap 3: Environment factors — time_of_day and traffic_deviation in IncidentDNA
"""
from __future__ import annotations

import pytest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Gap 1 — Blast radius history
# ---------------------------------------------------------------------------

class TestBlastRadiusHistory:
    def test_record_and_retrieve(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        rec = h.record("INC-1", "payment-api", ["db", "cache"], ["db", "auth"])
        assert rec.incident_id == "INC-1"
        assert rec.target_service == "payment-api"
        assert rec.accuracy == pytest.approx(1 / 3, abs=1e-4)   # {db} / {db, cache, auth}

    def test_perfect_prediction_accuracy_one(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        rec = h.record("INC-2", "svc-a", ["b", "c"], ["b", "c"])
        assert rec.accuracy == pytest.approx(1.0)

    def test_no_overlap_accuracy_zero(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        rec = h.record("INC-3", "svc-a", ["b"], ["c"])
        assert rec.accuracy == pytest.approx(0.0)

    def test_empty_actual_jaccard(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        rec = h.record("INC-4", "svc-a", ["b", "c"], [])
        assert rec.accuracy == pytest.approx(0.0)

    def test_both_empty_jaccard_one(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        rec = h.record("INC-5", "svc-a", [], [])
        assert rec.accuracy == pytest.approx(1.0)

    def test_get_accuracy_for_service(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        # Two incidents: accuracy 1.0 and 0.0 → avg 0.5
        h.record("INC-A", "svc-x", ["b"], ["b"])       # acc=1.0
        h.record("INC-B", "svc-x", ["c"], ["d"])       # acc=0.0
        assert h.get_accuracy_for_service("svc-x") == pytest.approx(0.5)

    def test_get_accuracy_for_unknown_service_returns_zero(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        assert h.get_accuracy_for_service("nonexistent") == 0.0

    def test_persistence_roundtrip(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        path = str(tmp_path / "brh.json")
        h1 = BlastRadiusHistory(path)
        h1.record("INC-P", "svc-a", ["b"], ["b", "c"])

        h2 = BlastRadiusHistory(path)
        history = h2.get_history()
        assert len(history) == 1
        assert history[0].incident_id == "INC-P"

    def test_get_history_newest_first(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        h.record("INC-1", "svc", ["a"], ["a"])
        h.record("INC-2", "svc", ["b"], ["b"])
        history = h.get_history(limit=2)
        assert history[0].incident_id == "INC-2"
        assert history[1].incident_id == "INC-1"

    def test_get_summary_groups_by_service(self, tmp_path):
        from supervisor.blast_radius_history import BlastRadiusHistory
        h = BlastRadiusHistory(str(tmp_path / "brh.json"))
        h.record("INC-1", "svc-a", ["b"], ["b"])    # acc=1.0
        h.record("INC-2", "svc-b", ["c"], ["d"])    # acc=0.0
        summary = h.get_summary()
        assert "svc-a" in summary
        assert summary["svc-a"]["avg_accuracy"] == pytest.approx(1.0)
        assert "svc-b" in summary


# ---------------------------------------------------------------------------
# Gap 2 — Cascade tracker
# ---------------------------------------------------------------------------

class TestCascadeTracker:
    def test_record_creates_prefix_chains(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        t.record("A", ["B", "C"])
        # Should record: A→B and A→B→C
        chains = t.get_chains_from("A", min_count=1)
        depths = {len(c.chain) for c in chains}
        assert 2 in depths   # A→B
        assert 3 in depths   # A→B→C

    def test_count_increments_on_repeat(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        t.record("A", ["B"])
        t.record("A", ["B"])
        t.record("A", ["B"])
        chains = t.get_chains_from("A", min_count=1)
        assert chains[0].count == 3

    def test_min_count_filter(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        t.record("A", ["B"])      # count=1
        t.record("A", ["C"])      # count=1
        t.record("A", ["B"])      # A→B count=2
        # Only A→B meets min_count=2
        chains = t.get_chains_from("A", min_count=2)
        assert len(chains) == 1
        assert chains[0].chain == ["A", "B"]

    def test_get_likely_next_single_hop(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        for _ in range(3):
            t.record("A", ["B", "C"])
        for _ in range(2):
            t.record("A", ["D"])
        # Given A has failed, most likely next is B (count=3 for A→B)
        predictions = t.get_likely_next(["A"], min_count=2)
        assert predictions[0] == "B"

    def test_get_likely_next_two_hop(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        for _ in range(3):
            t.record("A", ["B", "C"])
        # Given A and B have already failed, predict C
        predictions = t.get_likely_next(["A", "B"], min_count=2)
        assert "C" in predictions

    def test_self_not_recorded_as_co_failure(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        t.record("A", ["A", "B"])   # A should be deduplicated out
        chains = t.get_chains_from("A", min_count=1)
        for c in chains:
            assert c.chain.count("A") == 1   # only at position 0

    def test_empty_co_failures_noop(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        t.record("A", [])
        assert t.get_chains_from("A", min_count=1) == []

    def test_persistence_roundtrip(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        path = str(tmp_path / "ct.json")
        t1 = CascadeTracker(path)
        t1.record("X", ["Y", "Z"])
        t1.record("X", ["Y", "Z"])

        t2 = CascadeTracker(path)
        chains = t2.get_chains_from("X", min_count=1)
        assert any(c.chain == ["X", "Y"] for c in chains)
        assert any(c.chain == ["X", "Y", "Z"] for c in chains)

    def test_chains_sorted_by_count_descending(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        for _ in range(5):
            t.record("A", ["B"])
        for _ in range(2):
            t.record("A", ["C"])
        chains = t.get_chains_from("A", min_count=1)
        counts = [c.count for c in chains]
        assert counts == sorted(counts, reverse=True)

    def test_get_summary(self, tmp_path):
        from supervisor.cascade_tracker import CascadeTracker
        t = CascadeTracker(str(tmp_path / "ct.json"))
        t.record("A", ["B", "C"])
        summary = t.get_summary()
        assert summary["total_chains"] == 2
        assert summary["max_depth"] == 3


# ---------------------------------------------------------------------------
# Gap 3 — Environment factors in IncidentDNA
# ---------------------------------------------------------------------------

class TestEnvironmentFactors:
    def test_peak_hours_encodes_one(self):
        from supervisor.incident_dna import encode_incident
        dna = encode_incident("INC-1", "timeout", "svc", {"incident_hour": 12})
        assert dna.features[14] == pytest.approx(1.0)   # peak (9-18)

    def test_shoulder_hours_encodes_point_six(self):
        from supervisor.incident_dna import encode_incident
        dna = encode_incident("INC-1", "timeout", "svc", {"incident_hour": 8})
        assert dna.features[14] == pytest.approx(0.6)   # shoulder (7-9)

    def test_off_peak_encodes_point_two(self):
        from supervisor.incident_dna import encode_incident
        dna = encode_incident("INC-1", "timeout", "svc", {"incident_hour": 3})
        assert dna.features[14] == pytest.approx(0.2)   # off-peak

    def test_unknown_hour_encodes_zero(self):
        from supervisor.incident_dna import encode_incident
        dna = encode_incident("INC-1", "timeout", "svc", {})   # no incident_hour
        assert dna.features[14] == pytest.approx(0.0)

    def test_traffic_spike_encodes_one(self):
        from supervisor.incident_dna import encode_incident
        dna = encode_incident("INC-1", "timeout", "svc", {"traffic_ratio": 3.0})
        assert dna.features[15] == pytest.approx(1.0)

    def test_traffic_normal_encodes_low(self):
        from supervisor.incident_dna import encode_incident
        dna = encode_incident("INC-1", "timeout", "svc", {"traffic_ratio": 1.0})
        assert dna.features[15] == pytest.approx(0.25)

    def test_no_traffic_info_encodes_zero(self):
        from supervisor.incident_dna import encode_incident
        dna = encode_incident("INC-1", "timeout", "svc", {})
        assert dna.features[15] == pytest.approx(0.0)

    def test_feature_names_updated(self):
        from supervisor.incident_dna import FEATURE_NAMES
        assert FEATURE_NAMES[14] == "time_of_day"
        assert FEATURE_NAMES[15] == "traffic_deviation"

    def test_peak_and_spike_produce_different_signature_than_off_peak_normal(self):
        from supervisor.incident_dna import encode_incident, extract_signature
        dna_peak = encode_incident(
            "INC-1", "timeout", "svc",
            {"incident_hour": 14, "traffic_ratio": 2.5}
        )
        dna_offpeak = encode_incident(
            "INC-2", "timeout", "svc",
            {"incident_hour": 3, "traffic_ratio": 0.5}
        )
        # Different environment → different fingerprints
        assert extract_signature(dna_peak) != extract_signature(dna_offpeak)

    def test_environment_features_improve_similarity_matching(self):
        """Two incidents with same type but different environments should be less similar."""
        from supervisor.incident_dna import encode_incident, _cosine_similarity
        base = {
            "observed_error_rate": 0.5, "baseline_error_rate": 0.1,
            "observed_p95": 2.0, "baseline_p95": 0.5,
        }
        peak_spike = {**base, "incident_hour": 14, "traffic_ratio": 2.5}
        off_low    = {**base, "incident_hour": 3,  "traffic_ratio": 0.3}

        dna_ps = encode_incident("INC-A", "timeout", "svc", peak_spike)
        dna_ol = encode_incident("INC-B", "timeout", "svc", off_low)
        dna_ps2 = encode_incident("INC-C", "timeout", "svc", peak_spike)

        same_env = _cosine_similarity(dna_ps.features, dna_ps2.features)
        diff_env = _cosine_similarity(dna_ps.features, dna_ol.features)
        assert same_env > diff_env
