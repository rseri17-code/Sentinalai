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
import threading
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

    def update(self, eval_results: list[dict], decay_factor: float = 1.0) -> None:
        """Update calibration bins from ground truth evaluation results.

        Each result dict must have:
            - predicted_confidence: int
            - actual_correct: bool

        Args:
            decay_factor: Multiply existing bin counts by this before adding new
                          observations.  Use < 1.0 (e.g. 0.95) to make old data
                          count less than recent data.  Default 1.0 = no decay.
        """
        if decay_factor != 1.0 and 0.0 < decay_factor < 1.0:
            for b in self._bins:
                b.total = int(b.total * decay_factor)
                b.correct = int(b.correct * decay_factor)
                b.sum_confidence *= decay_factor

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
            logger.info("Calibration updated: %d results processed (decay=%.2f)", updated, decay_factor)
            try:
                from database.ops_persistence import get_ops_store
                total = sum(b.total for b in self._bins)
                bins_w = sum(1 for b in self._bins if b.total > 0)
                mean_conf = (
                    sum(b.sum_confidence for b in self._bins) / total if total else 0.0
                )
                get_ops_store().persist_convergence_snapshot(
                    investigation_id="",
                    ece=self.expected_calibration_error(),
                    total_samples=total,
                    mean_confidence=mean_conf,
                    bins_with_data=bins_w,
                )
            except Exception:
                pass

    def update_with_decay(self, eval_results: list[dict], decay_factor: float = 0.95) -> None:
        """Convenience wrapper applying temporal decay before updating.

        Call this instead of update() when incorporating new observations so that
        older predictions gradually lose influence.  A decay_factor of 0.95 means
        each update retains 95% of prior weight, giving a half-life of ~14 updates.
        """
        self.update(eval_results, decay_factor=decay_factor)

    def get_calibration_report(self) -> dict:
        """Return a summary of the current calibration state."""
        total_samples = sum(b.total for b in self._bins)
        bins_with_data = sum(1 for b in self._bins if b.total > 0)

        return {
            "total_samples": total_samples,
            "bins_with_data": bins_with_data,
            "ece": round(self.expected_calibration_error(), 4),
            "bins": [b.to_dict() for b in self._bins],
            "enabled": CALIBRATION_ENABLED,
        }

    def expected_calibration_error(self) -> float:
        """Compute Expected Calibration Error (ECE) across bins.

        ECE = weighted average of |avg_confidence - accuracy| per bin.
        Lower is better; 0.0 = perfectly calibrated.
        Returns 0.0 if there are no samples.
        """
        total = sum(b.total for b in self._bins)
        if total == 0:
            return 0.0
        ece = sum(
            (b.total / total) * abs(b.avg_confidence / 100.0 - b.accuracy)
            for b in self._bins
            if b.total > 0
        )
        return round(ece, 4)

    def is_stale(self, max_ece: float = 0.15, min_samples: int = 20) -> bool:
        """Return True if the calibrator needs retraining.

        Considered stale when ECE exceeds max_ece OR total samples < min_samples.
        """
        total = sum(b.total for b in self._bins)
        if total < min_samples:
            return True
        return self.expected_calibration_error() > max_ece

    def reset(self) -> None:
        """Reset all calibration bins."""
        self._bins = [CalibrationBin(bin_index=i) for i in range(N_BINS)]
        logger.info("Calibration bins reset")


# ---------------------------------------------------------------------------
# Process-level singleton — shared by agent.py and learning_loop.py so that
# in-memory state is never stale after a learning step updates the calibrator.
# ---------------------------------------------------------------------------

_calibrator: ConfidenceCalibrator | None = None
_calibrator_lock = threading.RLock()  # RLock allows same-thread re-entry (get_calibrator() inside with _calibrator_lock:)


def get_calibrator() -> ConfidenceCalibrator:
    """Return the process-level singleton, loading from disk on first access."""
    global _calibrator
    if _calibrator is not None:
        return _calibrator
    with _calibrator_lock:
        if _calibrator is None:
            _calibrator = ConfidenceCalibrator.load()
        return _calibrator
