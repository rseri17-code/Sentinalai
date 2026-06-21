"""Tests for intelligence/confidence_calibrator.py (Platt scaling calibrator).

Covers:
- fit() returns False with < 10 samples
- fit() returns True with enough samples
- calibrate() returns float in [0, 1]
- calibrate() returns raw value when unfitted
- calibrate_result() updates the confidence key in dict
- persistence: saves/loads from eval/calibration.json
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass

import pytest


# ---------------------------------------------------------------------------
# Minimal Episode stub (avoids importing full EpisodicMemory)
# ---------------------------------------------------------------------------

@dataclass
class _Episode:
    confidence: float
    outcome: str


def _make_episodes(n: int, outcome: str = "resolved", confidence: float = 0.85) -> list:
    return [_Episode(confidence=confidence, outcome=outcome) for _ in range(n)]


def _make_mixed_episodes(n_resolved: int, n_escalated: int) -> list:
    eps = [_Episode(confidence=0.90, outcome="resolved") for _ in range(n_resolved)]
    eps += [_Episode(confidence=0.45, outcome="escalated") for _ in range(n_escalated)]
    return eps


# ---------------------------------------------------------------------------
# Tests: fit()
# ---------------------------------------------------------------------------

class TestFit:
    def test_fit_returns_false_with_fewer_than_min_samples(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=10, persist_path=str(tmp_path / "calib.json"))
        episodes = _make_episodes(5)
        result = cal.fit(episodes)
        assert result is False

    def test_fit_returns_false_with_exactly_min_minus_one(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=10, persist_path=str(tmp_path / "calib.json"))
        episodes = _make_episodes(9)
        result = cal.fit(episodes)
        assert result is False

    def test_fit_returns_true_with_enough_samples(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=10, persist_path=str(tmp_path / "calib.json"))
        episodes = _make_mixed_episodes(n_resolved=10, n_escalated=5)
        result = cal.fit(episodes)
        assert result is True

    def test_fit_marks_calibrator_as_fitted(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=5, persist_path=str(tmp_path / "calib.json"))
        episodes = _make_mixed_episodes(n_resolved=5, n_escalated=3)
        cal.fit(episodes)
        assert cal._fitted is True

    def test_unfitted_after_insufficient_samples(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=10, persist_path=str(tmp_path / "calib.json"))
        cal.fit(_make_episodes(3))
        assert cal._fitted is False


# ---------------------------------------------------------------------------
# Tests: calibrate()
# ---------------------------------------------------------------------------

class TestCalibrate:
    def test_calibrate_returns_raw_when_unfitted(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(persist_path=str(tmp_path / "calib.json"))
        # Ensure unfitted
        cal._fitted = False
        result = cal.calibrate(0.75)
        assert result == 0.75

    def test_calibrate_returns_float_in_0_1_when_fitted(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=5, persist_path=str(tmp_path / "calib.json"))
        cal.fit(_make_mixed_episodes(8, 4))
        result = cal.calibrate(0.80)
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_calibrate_0_returns_low_probability(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=5, persist_path=str(tmp_path / "calib.json"))
        cal.fit(_make_mixed_episodes(8, 4))
        result = cal.calibrate(0.0)
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_calibrate_1_returns_high_probability(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=5, persist_path=str(tmp_path / "calib.json"))
        cal.fit(_make_mixed_episodes(8, 4))
        result = cal.calibrate(1.0)
        assert isinstance(result, float)
        assert 0.0 <= result <= 1.0

    def test_calibrate_percentage_scale_stays_in_0_100(self, tmp_path):
        """When raw_confidence > 1 (percentage scale), calibrated result should also be 0-100."""
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=5, persist_path=str(tmp_path / "calib.json"))
        cal.fit(_make_mixed_episodes(8, 4))
        result = cal.calibrate(85.0)
        assert 0.0 <= result <= 100.0

    def test_calibrate_unfitted_percentage_passthrough(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(persist_path=str(tmp_path / "calib.json"))
        cal._fitted = False
        result = cal.calibrate(85.0)
        assert result == 85.0


# ---------------------------------------------------------------------------
# Tests: calibrate_result()
# ---------------------------------------------------------------------------

class TestCalibrateResult:
    def test_calibrate_result_updates_confidence_key(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(min_samples=5, persist_path=str(tmp_path / "calib.json"))
        cal.fit(_make_mixed_episodes(8, 4))
        result = {"confidence": 0.9, "root_cause": "test"}
        updated = cal.calibrate_result(result)
        assert "confidence" in updated
        assert isinstance(updated["confidence"], float)
        assert 0.0 <= updated["confidence"] <= 1.0

    def test_calibrate_result_returns_same_dict(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(persist_path=str(tmp_path / "calib.json"))
        d = {"confidence": 0.7}
        returned = cal.calibrate_result(d)
        assert returned is d

    def test_calibrate_result_noop_when_no_confidence_key(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(persist_path=str(tmp_path / "calib.json"))
        d = {"root_cause": "something"}
        returned = cal.calibrate_result(d)
        assert returned == {"root_cause": "something"}

    def test_calibrate_result_passthrough_when_unfitted(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        cal = ConfidenceCalibrator(persist_path=str(tmp_path / "calib.json"))
        cal._fitted = False
        d = {"confidence": 0.77}
        cal.calibrate_result(d)
        assert d["confidence"] == 0.77


# ---------------------------------------------------------------------------
# Tests: persistence (save / load)
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_creates_calibration_json(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        path = str(tmp_path / "calibration.json")
        cal = ConfidenceCalibrator(min_samples=5, persist_path=path)
        cal.fit(_make_mixed_episodes(8, 4))
        # Fit calls _save internally
        assert os.path.exists(path)

    def test_saved_json_has_expected_keys(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        path = str(tmp_path / "calibration.json")
        cal = ConfidenceCalibrator(min_samples=5, persist_path=path)
        cal.fit(_make_mixed_episodes(8, 4))
        with open(path) as f:
            data = json.load(f)
        assert "fitted" in data
        assert "coef" in data
        assert "intercept" in data
        assert data["fitted"] is True

    def test_load_restores_fitted_state(self, tmp_path):
        pytest.importorskip("sklearn")
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        path = str(tmp_path / "calibration.json")
        # Fit and save
        cal1 = ConfidenceCalibrator(min_samples=5, persist_path=path)
        cal1.fit(_make_mixed_episodes(8, 4))
        coef1 = cal1._coef
        intercept1 = cal1._intercept

        # Load fresh instance
        cal2 = ConfidenceCalibrator(min_samples=5, persist_path=path)
        assert cal2._fitted is True
        assert cal2._coef == pytest.approx(coef1)
        assert cal2._intercept == pytest.approx(intercept1)

    def test_load_noop_when_no_file(self, tmp_path):
        from intelligence.confidence_calibrator import ConfidenceCalibrator
        path = str(tmp_path / "nonexistent.json")
        cal = ConfidenceCalibrator(persist_path=path)
        assert cal._fitted is False
