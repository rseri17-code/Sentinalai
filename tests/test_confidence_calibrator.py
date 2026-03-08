"""Tests for confidence calibration feedback loop (supervisor/confidence_calibrator.py).

Covers CalibrationBin, ConfidenceCalibrator: load, save, calibrate,
update, reset, and calibration report.
"""

from __future__ import annotations

import json

import pytest

from supervisor.confidence_calibrator import (
    CalibrationBin,
    ConfidenceCalibrator,
    N_BINS,
)


# =========================================================================
# CalibrationBin
# =========================================================================

class TestCalibrationBin:
    def test_accuracy_with_data(self):
        b = CalibrationBin(bin_index=0, total=10, correct=7, sum_confidence=800.0)
        assert b.accuracy == pytest.approx(0.7)

    def test_accuracy_no_data(self):
        b = CalibrationBin(bin_index=0, total=0, correct=0)
        assert b.accuracy == 0.0

    def test_avg_confidence_with_data(self):
        b = CalibrationBin(bin_index=0, total=4, correct=2, sum_confidence=320.0)
        assert b.avg_confidence == 80.0

    def test_avg_confidence_no_data_returns_midpoint(self):
        b = CalibrationBin(bin_index=5)
        # Midpoint of bin 5: (5 + 0.5) * 10 = 55
        assert b.avg_confidence == 55.0

    def test_calibrated_confidence_insufficient_data(self):
        b = CalibrationBin(bin_index=8, total=2, correct=1)
        # Not enough data (< 3) -> return bin midpoint
        expected = int((8 + 0.5) * (100 / N_BINS))
        assert b.calibrated_confidence == expected

    def test_calibrated_confidence_with_data(self):
        b = CalibrationBin(bin_index=7, total=10, correct=6, sum_confidence=750.0)
        # accuracy = 0.6 -> calibrated = 60
        assert b.calibrated_confidence == 60

    def test_to_dict(self):
        b = CalibrationBin(bin_index=3, total=5, correct=3, sum_confidence=200.0)
        d = b.to_dict()
        assert d["bin_index"] == 3
        assert d["total"] == 5
        assert d["correct"] == 3
        assert "accuracy" in d
        assert "calibrated_confidence" in d


# =========================================================================
# ConfidenceCalibrator — init / load / save
# =========================================================================

class TestCalibratorInit:
    def test_default_bins(self):
        cal = ConfidenceCalibrator()
        assert len(cal._bins) == N_BINS

    def test_custom_bins(self):
        bins = [CalibrationBin(bin_index=i, total=10, correct=5) for i in range(N_BINS)]
        cal = ConfidenceCalibrator(bins=bins)
        assert cal._bins[0].total == 10

    def test_load_missing_file(self, tmp_path):
        cal = ConfidenceCalibrator.load(str(tmp_path / "nope.json"))
        assert len(cal._bins) == N_BINS
        assert cal._bins[0].total == 0

    def test_load_invalid_json(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json")
        cal = ConfidenceCalibrator.load(str(path))
        assert len(cal._bins) == N_BINS

    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "cal.json")
        cal = ConfidenceCalibrator()
        cal._bins[5].total = 10
        cal._bins[5].correct = 7
        cal._bins[5].sum_confidence = 550.0
        cal.save(path)

        loaded = ConfidenceCalibrator.load(path)
        assert loaded._bins[5].total == 10
        assert loaded._bins[5].correct == 7

    def test_save_creates_directory(self, tmp_path):
        path = str(tmp_path / "subdir" / "cal.json")
        cal = ConfidenceCalibrator()
        cal.save(path)
        loaded = ConfidenceCalibrator.load(path)
        assert len(loaded._bins) == N_BINS


# =========================================================================
# ConfidenceCalibrator — calibrate
# =========================================================================

class TestCalibrateMethod:
    def test_calibrate_disabled_returns_raw(self):
        """When CALIBRATION_ENABLED is false, returns raw value."""
        cal = ConfidenceCalibrator()
        # Default is disabled
        assert cal.calibrate(85) == 85

    def test_calibrate_enabled_with_data(self, monkeypatch):
        monkeypatch.setattr("supervisor.confidence_calibrator.CALIBRATION_ENABLED", True)
        bins = [CalibrationBin(bin_index=i) for i in range(N_BINS)]
        # Bin 8 (covers 80-89): accuracy 60% -> calibrated confidence = 60
        bins[8].total = 20
        bins[8].correct = 12
        bins[8].sum_confidence = 1700.0
        cal = ConfidenceCalibrator(bins=bins)
        assert cal.calibrate(85) == 60

    def test_calibrate_enabled_insufficient_data_returns_midpoint(self, monkeypatch):
        monkeypatch.setattr("supervisor.confidence_calibrator.CALIBRATION_ENABLED", True)
        cal = ConfidenceCalibrator()
        # Bin 8 midpoint = 85
        result = cal.calibrate(85)
        assert result == 85

    def test_calibrate_boundary_0(self, monkeypatch):
        monkeypatch.setattr("supervisor.confidence_calibrator.CALIBRATION_ENABLED", True)
        cal = ConfidenceCalibrator()
        result = cal.calibrate(0)
        assert 0 <= result <= 100

    def test_calibrate_boundary_100(self, monkeypatch):
        monkeypatch.setattr("supervisor.confidence_calibrator.CALIBRATION_ENABLED", True)
        cal = ConfidenceCalibrator()
        result = cal.calibrate(100)
        assert 0 <= result <= 100


# =========================================================================
# ConfidenceCalibrator — update
# =========================================================================

class TestUpdateMethod:
    def test_update_single_result(self):
        cal = ConfidenceCalibrator()
        cal.update([{"predicted_confidence": 85, "actual_correct": True}])
        assert cal._bins[8].total == 1
        assert cal._bins[8].correct == 1

    def test_update_multiple_results(self):
        cal = ConfidenceCalibrator()
        results = [
            {"predicted_confidence": 85, "actual_correct": True},
            {"predicted_confidence": 85, "actual_correct": False},
            {"predicted_confidence": 25, "actual_correct": True},
        ]
        cal.update(results)
        assert cal._bins[8].total == 2
        assert cal._bins[8].correct == 1
        assert cal._bins[2].total == 1
        assert cal._bins[2].correct == 1

    def test_update_empty_list(self):
        cal = ConfidenceCalibrator()
        cal.update([])
        assert all(b.total == 0 for b in cal._bins)

    def test_update_missing_fields_defaults(self):
        cal = ConfidenceCalibrator()
        cal.update([{}])
        # predicted_confidence defaults to 0 -> bin 0
        assert cal._bins[0].total == 1
        assert cal._bins[0].correct == 0  # actual_correct defaults to False


# =========================================================================
# ConfidenceCalibrator — report and reset
# =========================================================================

class TestReportAndReset:
    def test_calibration_report(self):
        cal = ConfidenceCalibrator()
        cal._bins[5].total = 10
        cal._bins[5].correct = 7
        report = cal.get_calibration_report()
        assert report["total_samples"] == 10
        assert report["bins_with_data"] == 1
        assert len(report["bins"]) == N_BINS

    def test_reset_clears_all(self):
        cal = ConfidenceCalibrator()
        cal._bins[3].total = 100
        cal._bins[3].correct = 50
        cal.reset()
        assert all(b.total == 0 for b in cal._bins)
        assert len(cal._bins) == N_BINS

    def test_report_enabled_flag(self, monkeypatch):
        monkeypatch.setattr("supervisor.confidence_calibrator.CALIBRATION_ENABLED", True)
        cal = ConfidenceCalibrator()
        report = cal.get_calibration_report()
        assert report["enabled"] is True
