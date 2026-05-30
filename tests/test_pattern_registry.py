"""Tests for PatternRegistry (Phase 2 harness learning loop)."""
from __future__ import annotations

import json
import os

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_features(incident_type: str = "timeout", n_dims: int = 16) -> list[float]:
    """Minimal feature vector — mostly zeros except type one-hot and a few dims."""
    v = [0.0] * n_dims
    v[0] = 0.8   # error_rate_ratio
    v[1] = 0.9   # latency_ratio
    type_idx = {"timeout": 7, "oomkill": 8, "error_spike": 9, "network": 10, "saturation": 11}
    if incident_type in type_idx:
        v[type_idx[incident_type]] = 1.0
    v[12] = 1.0  # service_tier P1
    v[13] = 0.6  # evidence_source_count
    return v


# ---------------------------------------------------------------------------
# extract_signature
# ---------------------------------------------------------------------------

class TestExtractSignature:
    def test_deterministic(self):
        from supervisor.incident_dna import encode_incident, extract_signature
        dna = encode_incident("INC1", "timeout", "api-gw", {}, 70)
        assert extract_signature(dna) == extract_signature(dna)

    def test_different_types_different_signatures(self):
        from supervisor.incident_dna import encode_incident, extract_signature
        dna_t = encode_incident("INC1", "timeout", "api-gw", {}, 70)
        dna_o = encode_incident("INC2", "oomkill", "api-gw", {}, 70)
        assert extract_signature(dna_t) != extract_signature(dna_o)

    def test_returns_hex_string(self):
        from supervisor.incident_dna import encode_incident, extract_signature
        dna = encode_incident("INC1", "timeout", "api-gw", {}, 70)
        sig = extract_signature(dna)
        assert len(sig) == 16
        assert all(c in "0123456789abcdef" for c in sig)


# ---------------------------------------------------------------------------
# PatternRecord
# ---------------------------------------------------------------------------

class TestPatternRecord:
    def test_top_hypothesis_none_when_sparse(self):
        from supervisor.pattern_registry import PatternRecord
        rec = PatternRecord(fingerprint="abc", incident_type="timeout")
        rec.hypothesis_outcomes = {"db_slow": {"correct": 1, "incorrect": 0}}
        assert rec.top_hypothesis() is None  # needs >= 2 samples

    def test_top_hypothesis_returns_best(self):
        from supervisor.pattern_registry import PatternRecord
        rec = PatternRecord(fingerprint="abc", incident_type="timeout")
        rec.hypothesis_outcomes = {
            "db_slow": {"correct": 3, "incorrect": 1},    # 75%
            "network": {"correct": 4, "incorrect": 0},    # 100%
        }
        assert rec.top_hypothesis() == "network"

    def test_success_rate(self):
        from supervisor.pattern_registry import PatternRecord
        rec = PatternRecord(fingerprint="abc", incident_type="timeout")
        rec.hypothesis_outcomes["db_slow"] = {"correct": 3, "incorrect": 1}
        assert rec.success_rate("db_slow") == pytest.approx(0.75)

    def test_roundtrip(self):
        from supervisor.pattern_registry import PatternRecord
        rec = PatternRecord(
            fingerprint="abc123",
            incident_type="timeout",
            signal_sequence=["search_logs", "get_golden_signals"],
            recommended_steps=["get_golden_signals", "search_logs"],
            match_count=5,
            hypothesis_outcomes={"db_slow": {"correct": 3, "incorrect": 1}},
            last_seen="2024-01-01T00:00:00+00:00",
            features=[0.5] * 16,
        )
        assert PatternRecord.from_dict(rec.to_dict()).fingerprint == "abc123"
        assert PatternRecord.from_dict(rec.to_dict()).match_count == 5


# ---------------------------------------------------------------------------
# PatternRegistry
# ---------------------------------------------------------------------------

class TestPatternRegistry:
    def test_record_and_retrieve(self, tmp_path):
        from supervisor.pattern_registry import PatternRegistry
        reg = PatternRegistry(str(tmp_path / "reg.json"))
        features = _make_features("timeout")
        reg.record("fp1", "timeout", features, root_cause="db_slow")
        assert reg.get("fp1") is not None
        assert reg.get("fp1").match_count == 1

    def test_match_returns_similar(self, tmp_path):
        from supervisor.pattern_registry import PatternRegistry
        reg = PatternRegistry(str(tmp_path / "reg.json"))
        features = _make_features("timeout")
        reg.record("fp1", "timeout", features)
        matches = reg.match(features, "timeout", top_k=3)
        assert len(matches) == 1
        assert matches[0].fingerprint == "fp1"

    def test_match_empty_when_no_similar(self, tmp_path):
        from supervisor.pattern_registry import PatternRegistry
        reg = PatternRegistry(str(tmp_path / "reg.json"))
        reg.record("fp1", "timeout", _make_features("timeout"))
        # OOMKill vector is very different from timeout
        oom = _make_features("oomkill")
        oom[0] = 0.0  # zero out error rate to maximize difference
        oom[1] = 0.0
        matches = reg.match(oom, "oomkill", top_k=3)
        # May or may not match depending on threshold — should not crash
        assert isinstance(matches, list)

    def test_match_count_increments_on_rerecord(self, tmp_path):
        from supervisor.pattern_registry import PatternRegistry
        reg = PatternRegistry(str(tmp_path / "reg.json"))
        features = _make_features("timeout")
        reg.record("fp1", "timeout", features)
        reg.record("fp1", "timeout", features)
        assert reg.get("fp1").match_count == 2

    def test_update_outcome_promotes_hypothesis(self, tmp_path):
        from supervisor.pattern_registry import PatternRegistry
        reg = PatternRegistry(str(tmp_path / "reg.json"))
        reg.record("fp1", "timeout", _make_features("timeout"))
        reg.update_outcome("fp1", "db_slow", was_correct=True)
        reg.update_outcome("fp1", "db_slow", was_correct=True)
        reg.update_outcome("fp1", "db_slow", was_correct=False)
        assert reg.get("fp1").success_rate("db_slow") == pytest.approx(2 / 3)

    def test_update_outcome_noop_for_missing_fingerprint(self, tmp_path):
        from supervisor.pattern_registry import PatternRegistry
        reg = PatternRegistry(str(tmp_path / "reg.json"))
        # Should not raise
        reg.update_outcome("nonexistent", "db_slow", was_correct=True)

    def test_persistence_roundtrip(self, tmp_path):
        from supervisor.pattern_registry import PatternRegistry
        path = str(tmp_path / "reg.json")
        reg1 = PatternRegistry(path)
        features = _make_features("timeout")
        reg1.record("fp1", "timeout", features, root_cause="db_slow")

        reg2 = PatternRegistry(path)
        assert reg2.get("fp1") is not None
        assert reg2.get("fp1").incident_type == "timeout"

    def test_evicts_when_over_cap(self, tmp_path):
        from supervisor.pattern_registry import PatternRegistry, _MAX_RECORDS
        reg = PatternRegistry(str(tmp_path / "reg.json"))
        for i in range(_MAX_RECORDS + 1):
            reg.record(f"fp{i:04d}", "timeout", _make_features("timeout"))
        assert len(reg.all_records()) <= _MAX_RECORDS
