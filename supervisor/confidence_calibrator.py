"""Confidence calibration feedback loop for SentinalAI.

Adjusts agent confidence scores based on historical accuracy data, reducing
overconfidence and underconfidence over time. Uses isotonic regression-style
binned calibration derived from ground truth evaluations.

The calibrator maintains a calibration map: raw confidence bin -> calibrated
confidence. This map is updated after each ground truth evaluation batch.

Usage:
    from supervisor.confidence_calibrator import ConfidenceCalibrator

    calibrator = ConfidenceCalibrator.load()
    calibrated = calibrator.calibrate(raw_confidence=85)
    calibrator.update(eval_results)
    calibrator.save()

Configuration:
    CALIBRATION_MAP_PATH - Path to calibration map JSON (default: eval/calibration_map.json)
    CALIBRATION_ENABLED  - Enable/disable calibration (default: false)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("sentinalai.calibration")

DEFAULT_MAP_PATH = os.environ.get(
    "CALIBRATION_MAP_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "calibration_map.json"),
)

CALIBRATION_ENABLED = os.environ.get("CALIBRATION_ENABLED", "false").lower() in ("true", "1", "yes")

# Number of confidence bins (10 bins = 0-9, 10-19, ..., 90-100)
N_BINS = 10


@dataclass
class CalibrationBin:
    """Tracks accuracy within a confidence bin."""
    bin_index: int
    total: int = 0
    correct: int = 0
    sum_confidence: float = 0.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total > 0 else 0.0

    @property
    def avg_confidence(self) -> float:
        return self.sum_confidence / self.total if self.total > 0 else (self.bin_index + 0.5) * (100 / N_BINS)

    @property
    def calibrated_confidence(self) -> int:
        """The calibrated confidence for this bin = observed accuracy * 100."""
        if self.total < 3:
            # Not enough data — return bin midpoint (identity mapping)
            return int((self.bin_index + 0.5) * (100 / N_BINS))
        return max(0, min(100, int(round(self.accuracy * 100))))

    def to_dict(self) -> dict:
        return {
            "bin_index": self.bin_index,
            "total": self.total,
            "correct": self.correct,
            "sum_confidence": self.sum_confidence,
            "accuracy": round(self.accuracy, 4),
            "calibrated_confidence": self.calibrated_confidence,
        }


class ConfidenceCalibrator:
    """Calibrates raw confidence scores based on historical accuracy."""

    def __init__(self, bins: list[CalibrationBin] | None = None):
        if bins:
            self._bins = bins
        else:
            self._bins = [CalibrationBin(bin_index=i) for i in range(N_BINS)]

    @classmethod
    def load(cls, path: str | None = None) -> ConfidenceCalibrator:
        """Load calibration map from disk."""
        path = path or DEFAULT_MAP_PATH
        try:
            with open(path, "r") as f:
                data = json.load(f)
            bins = []
            for entry in data.get("bins", []):
                bins.append(CalibrationBin(
                    bin_index=entry["bin_index"],
                    total=entry.get("total", 0),
                    correct=entry.get("correct", 0),
                    sum_confidence=entry.get("sum_confidence", 0.0),
                ))
            logger.info("Calibration map loaded: %d bins from %s", len(bins), path)
            return cls(bins)
        except FileNotFoundError:
            logger.debug("No calibration map found at %s, using identity", path)
            return cls()
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Invalid calibration map: %s", exc)
            return cls()

    def save(self, path: str | None = None) -> None:
        """Save calibration map to disk."""
        path = path or DEFAULT_MAP_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "version": 1,
            "n_bins": N_BINS,
            "bins": [b.to_dict() for b in self._bins],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Calibration map saved to %s", path)

    def calibrate(self, raw_confidence: int) -> int:
        """Apply calibration to a raw confidence score.

        Returns calibrated confidence (0-100).
        If calibration is disabled or insufficient data, returns raw value.
        """
        if not CALIBRATION_ENABLED:
            return raw_confidence

        bin_idx = min(int(raw_confidence / (100 / N_BINS)), N_BINS - 1)
        calibrated = self._bins[bin_idx].calibrated_confidence

        if calibrated != raw_confidence:
            logger.debug(
                "Confidence calibrated: raw=%d -> calibrated=%d (bin=%d, n=%d)",
                raw_confidence, calibrated, bin_idx, self._bins[bin_idx].total,
            )

        return calibrated

    def update(self, eval_results: list[dict]) -> None:
        """Update calibration bins from ground truth evaluation results.

        Each result dict must have:
            - predicted_confidence: int
            - actual_correct: bool
        """
        updated = 0
        for r in eval_results:
            conf = r.get("predicted_confidence", 0)
            correct = r.get("actual_correct", False)

            bin_idx = min(int(conf / (100 / N_BINS)), N_BINS - 1)
            self._bins[bin_idx].total += 1
            self._bins[bin_idx].sum_confidence += conf
            if correct:
                self._bins[bin_idx].correct += 1
            updated += 1

        if updated:
            logger.info("Calibration updated: %d results processed", updated)

    def get_calibration_report(self) -> dict:
        """Return a summary of the current calibration state."""
        total_samples = sum(b.total for b in self._bins)
        bins_with_data = sum(1 for b in self._bins if b.total > 0)

        return {
            "total_samples": total_samples,
            "bins_with_data": bins_with_data,
            "bins": [b.to_dict() for b in self._bins],
            "enabled": CALIBRATION_ENABLED,
        }

    def reset(self) -> None:
        """Reset all calibration bins."""
        self._bins = [CalibrationBin(bin_index=i) for i in range(N_BINS)]
        logger.info("Calibration bins reset")
