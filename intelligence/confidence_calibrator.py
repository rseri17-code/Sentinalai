"""Platt-scaling confidence calibrator for SentinalAI investigation scores.

Fits a logistic regression (Platt scaling) on historical episode outcomes to
turn raw confidence scores into calibrated probabilities.

Usage:
    from intelligence.confidence_calibrator import get_calibrator

    cal = get_calibrator()
    calibrated = cal.calibrate(0.82)       # returns float in [0, 1]
    result = cal.calibrate_result(result)  # replaces 'confidence' key in-place

Persistence:
    Fitted parameters (coef_ and intercept_) are stored in eval/calibration.json
    and loaded on init if the file exists.

Singleton:
    get_calibrator() returns a process-level singleton that auto-fits on first
    access from EpisodicMemory if enough samples (>= min_samples) exist.
"""
from __future__ import annotations

import json
import logging
import os
import threading

logger = logging.getLogger("sentinalai.intelligence.confidence_calibrator")

_DEFAULT_CALIB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "eval", "calibration.json"
)


class ConfidenceCalibrator:
    """Platt-scaling calibrator: logistic regression on raw confidence → probability."""

    def __init__(
        self,
        min_samples: int = 10,
        persist_path: str = _DEFAULT_CALIB_PATH,
    ) -> None:
        self._min_samples = min_samples
        self._persist_path = persist_path
        self._fitted = False
        self._coef: float = 1.0       # slope
        self._intercept: float = 0.0  # bias

        # Try loading persisted parameters
        self._load()

    # ------------------------------------------------------------------ #
    # Fitting
    # ------------------------------------------------------------------ #

    def fit(self, episodes: list) -> bool:
        """Fit on a list of Episode objects.

        Maps outcome: 'resolved' or 'auto-remediated' → 1, 'escalated' → 0.
        Episodes with other outcomes (e.g. 'unknown') are included as 0.

        Returns True if fitting succeeded (>= min_samples), False otherwise.
        """
        if len(episodes) < self._min_samples:
            logger.debug(
                "ConfidenceCalibrator.fit: only %d episodes (need %d); skipping",
                len(episodes), self._min_samples,
            )
            return False

        try:
            from sklearn.linear_model import LogisticRegression  # lazy import

            X = [[ep.confidence] for ep in episodes]
            y = [
                1 if ep.outcome in ("resolved", "auto-remediated") else 0
                for ep in episodes
            ]

            clf = LogisticRegression(max_iter=200)
            clf.fit(X, y)

            self._coef = float(clf.coef_[0][0])
            self._intercept = float(clf.intercept_[0])
            self._fitted = True

            logger.info(
                "ConfidenceCalibrator fitted on %d episodes: coef=%.4f intercept=%.4f",
                len(episodes), self._coef, self._intercept,
            )
            self._save()
            return True

        except Exception as exc:
            logger.warning("ConfidenceCalibrator.fit failed: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Calibration
    # ------------------------------------------------------------------ #

    def calibrate(self, raw_confidence: float) -> float:
        """Return calibrated probability in [0, 1].

        If not fitted, returns raw_confidence unchanged (pass-through).
        raw_confidence may be in [0, 1] or [0, 100]; values > 1.0 are
        treated as percentage and normalised before calibration, then
        returned in the same scale.
        """
        if not self._fitted:
            return raw_confidence

        # Normalise to [0, 1] for sigmoid
        if raw_confidence > 1.0:
            normalised = raw_confidence / 100.0
            result = self._sigmoid(self._coef * normalised + self._intercept)
            return result * 100.0
        else:
            result = self._sigmoid(self._coef * raw_confidence + self._intercept)
            return result

    def calibrate_result(self, result: dict) -> dict:
        """Return a copy of result with 'confidence' replaced by calibrated value.

        Operates on the dict in-place and also returns it for chaining.
        """
        if "confidence" not in result:
            return result
        raw = result["confidence"]
        calibrated = self.calibrate(raw)
        result["confidence"] = calibrated
        if calibrated != raw:
            result.setdefault("raw_confidence", raw)
        return result

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sigmoid(x: float) -> float:
        import math
        try:
            return 1.0 / (1.0 + math.exp(-x))
        except OverflowError:
            return 0.0 if x < 0 else 1.0

    def _save(self) -> None:
        """Persist fitted parameters to eval/calibration.json."""
        try:
            os.makedirs(os.path.dirname(self._persist_path), exist_ok=True)
            data = {
                "fitted": self._fitted,
                "coef": self._coef,
                "intercept": self._intercept,
            }
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.debug("ConfidenceCalibrator saved to %s", self._persist_path)
        except Exception as exc:
            logger.warning("ConfidenceCalibrator._save failed: %s", exc)

    def _load(self) -> None:
        """Load fitted parameters from eval/calibration.json if it exists."""
        try:
            if not os.path.exists(self._persist_path):
                return
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._coef = float(data.get("coef", 1.0))
            self._intercept = float(data.get("intercept", 0.0))
            self._fitted = bool(data.get("fitted", False))
            if self._fitted:
                logger.info(
                    "ConfidenceCalibrator loaded from %s: coef=%.4f intercept=%.4f",
                    self._persist_path, self._coef, self._intercept,
                )
        except Exception as exc:
            logger.warning("ConfidenceCalibrator._load failed: %s", exc)


# -------------------------------------------------------------------------
# Process-level singleton
# -------------------------------------------------------------------------

_calibrator: ConfidenceCalibrator | None = None
_calibrator_lock = threading.Lock()


def get_calibrator() -> ConfidenceCalibrator:
    """Return the process-level singleton ConfidenceCalibrator.

    On first access, auto-fits from EpisodicMemory if enough samples exist.
    """
    global _calibrator
    if _calibrator is not None:
        return _calibrator
    with _calibrator_lock:
        if _calibrator is not None:
            return _calibrator
        cal = ConfidenceCalibrator()
        # Auto-fit from EpisodicMemory on startup if not already loaded from disk
        if not cal._fitted:
            try:
                from intelligence.episodic_memory import EpisodicMemory
                episodes = EpisodicMemory().list_all() if hasattr(EpisodicMemory(), "list_all") else []
                if not episodes:
                    # Fall back to _episodes attribute (EpisodicMemory stores episodes in-memory)
                    em = EpisodicMemory()
                    episodes = list(em._episodes)
                if episodes:
                    cal.fit(episodes)
            except Exception as exc:
                logger.debug("Auto-fit from EpisodicMemory failed (non-critical): %s", exc)
        _calibrator = cal
    return _calibrator
