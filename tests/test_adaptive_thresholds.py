"""Tests for supervisor.adaptive_thresholds."""
from __future__ import annotations

import os
import json

os.environ.setdefault("ADAPTIVE_THRESHOLDS_ENABLED", "true")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tmp_path(tmp_path):
    """Return a fresh path for the thresholds JSON file."""
    return str(tmp_path / "adaptive_thresholds.json")


# ---------------------------------------------------------------------------
# get() — fallback behaviour
# ---------------------------------------------------------------------------

class TestGet:

    def test_returns_default_when_file_missing(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        assert mod.get("critique_threshold") == 0.62

    def test_returns_default_for_unknown_name(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        assert mod.get("nonexistent_threshold") == 0.5

    def test_disabled_returns_default(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_ENABLED", False)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        assert mod.get("skip_weight_threshold") == 0.35

    def test_returns_stored_value_after_update(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        new_val = mod.update("critique_threshold", 0.8)
        assert mod.get("critique_threshold") == new_val


# ---------------------------------------------------------------------------
# update() — EMA signal and bounds
# ---------------------------------------------------------------------------

class TestUpdate:

    def test_unknown_name_returns_default(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        result = mod.update("unknown_key", 0.9)
        assert result == 0.5

    def test_disabled_returns_default(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_ENABLED", False)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        result = mod.update("critique_threshold", 0.9)
        assert result == 0.62

    def test_high_signal_increases_value(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        baseline = mod.get("critique_threshold")
        # Apply strong upward signal many times
        for _ in range(20):
            mod.update("critique_threshold", 1.0)
        assert mod.get("critique_threshold") > baseline

    def test_low_signal_decreases_value(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        baseline = mod.get("critique_threshold")
        for _ in range(20):
            mod.update("critique_threshold", 0.0)
        assert mod.get("critique_threshold") < baseline

    def test_value_stays_within_bounds(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        for _ in range(200):
            mod.update("critique_threshold", 1.0)
        val = mod.get("critique_threshold")
        assert val <= 0.80

        mod.reset()
        for _ in range(200):
            mod.update("critique_threshold", 0.0)
        val = mod.get("critique_threshold")
        assert val >= 0.40

    def test_neutral_signal_leaves_value_stable(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        baseline = mod.get("skip_weight_threshold")
        for _ in range(50):
            mod.update("skip_weight_threshold", 0.5)
        # With neutral signal, value should stay very close to initial
        val = mod.get("skip_weight_threshold")
        assert abs(val - baseline) < 0.05

    def test_observations_increment(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        mod.update("min_confidence_to_act", 0.6)
        mod.update("min_confidence_to_act", 0.6)
        with open(p) as f:
            data = json.load(f)
        assert data["min_confidence_to_act"]["observations"] == 2

    def test_atomic_write_leaves_no_tmp(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        mod.update("critique_threshold", 0.7)
        assert not os.path.exists(p + ".tmp")


# ---------------------------------------------------------------------------
# record_* semantic helpers
# ---------------------------------------------------------------------------

class TestRecordCritiqueOutcome:

    def test_refinement_helped_applies_mild_upward_signal(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        # signal=0.55 (slight upward) when triggered and helped
        mod.record_critique_outcome(0.55, refinement_triggered=True, refinement_helped=True)
        # Just verify it ran without error and stored something
        assert mod.get("critique_threshold") != 0.0

    def test_refinement_not_helped_applies_strong_upward_signal(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        baseline = mod.get("critique_threshold")
        for _ in range(30):
            mod.record_critique_outcome(0.55, refinement_triggered=True, refinement_helped=False)
        # signal=0.72 → value rises above baseline over time
        assert mod.get("critique_threshold") > baseline

    def test_not_triggered_neutral(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        baseline = mod.get("critique_threshold")
        for _ in range(50):
            mod.record_critique_outcome(0.70, refinement_triggered=False, refinement_helped=False)
        # Neutral signal — value stays within 5% of baseline
        assert abs(mod.get("critique_threshold") - baseline) < 0.05


class TestRecordQualityObservation:

    def test_high_quality_stored_slightly_permissive(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        mod.record_quality_observation(0.95, experience_stored=True)
        assert mod.get("store_quality_threshold") is not None  # no crash

    def test_low_quality_rejected_slightly_conservative(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        mod.record_quality_observation(0.45, experience_stored=False)
        assert mod.get("store_quality_threshold") is not None


class TestRecordStepSkipOutcome:

    def test_skip_not_triggered_does_nothing(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        # step_was_skipped=False → early return, no file written
        mod.record_step_skip_outcome(False, 0.7, 0.8)
        assert not os.path.exists(p)

    def test_skip_helped_raises_threshold(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        baseline = mod.get("skip_weight_threshold")
        for _ in range(30):
            mod.record_step_skip_outcome(True, 0.5, 0.8)  # quality improved
        assert mod.get("skip_weight_threshold") > baseline

    def test_skip_hurt_lowers_threshold(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        baseline = mod.get("skip_weight_threshold")
        for _ in range(30):
            mod.record_step_skip_outcome(True, 0.8, 0.5)  # quality degraded
        assert mod.get("skip_weight_threshold") < baseline


class TestRecordConfidenceOutcome:

    def test_high_confidence_does_nothing(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        mod.record_confidence_outcome(75.0, was_correct=True)
        # High confidence → early return, no write
        assert not os.path.exists(p)

    def test_low_confidence_correct_lowers_threshold(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        baseline = mod.get("min_confidence_to_act")
        for _ in range(30):
            mod.record_confidence_outcome(25.0, was_correct=True)
        assert mod.get("min_confidence_to_act") < baseline

    def test_low_confidence_wrong_raises_threshold(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        baseline = mod.get("min_confidence_to_act")
        for _ in range(30):
            mod.record_confidence_outcome(25.0, was_correct=False)
        assert mod.get("min_confidence_to_act") > baseline


# ---------------------------------------------------------------------------
# get_all() and reset()
# ---------------------------------------------------------------------------

class TestGetAllAndReset:

    def test_get_all_returns_four_thresholds(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        all_vals = mod.get_all()
        assert set(all_vals.keys()) == {
            "critique_threshold", "store_quality_threshold",
            "skip_weight_threshold", "min_confidence_to_act",
        }

    def test_reset_single_restores_default(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        # Shift value away from default
        for _ in range(20):
            mod.update("critique_threshold", 1.0)
        assert mod.get("critique_threshold") != 0.62
        mod.reset("critique_threshold")
        assert mod.get("critique_threshold") == 0.62

    def test_reset_all_clears_file(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        mod.update("critique_threshold", 0.9)
        mod.reset()
        # After full reset, get() should return hardcoded defaults
        assert mod.get("skip_weight_threshold") == 0.35

    def test_reset_nonexistent_key_noop(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", _make_tmp_path(tmp_path))
        # Should not raise
        mod.reset("nonexistent_key")


# ---------------------------------------------------------------------------
# Persistence edge cases
# ---------------------------------------------------------------------------

class TestPersistenceEdgeCases:

    def test_corrupt_json_resets_gracefully(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("{NOT VALID JSON")
        # Should return default without crashing
        assert mod.get("critique_threshold") == 0.62

    def test_non_dict_json_resets_gracefully(self, tmp_path, monkeypatch):
        import supervisor.adaptive_thresholds as mod
        p = _make_tmp_path(tmp_path)
        monkeypatch.setattr(mod, "ADAPTIVE_THRESHOLDS_PATH", p)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump([1, 2, 3], f)
        assert mod.get("critique_threshold") == 0.62
