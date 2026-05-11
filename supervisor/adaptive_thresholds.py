"""Adaptive threshold management for SentinalAI continuous improvement.

Key insight: static thresholds degrade over time as the system's behaviour
and data distribution shift.  This module maintains four thresholds that
self-adjust based on rolling feedback after each investigation:

  CRITIQUE_THRESHOLD
    When self-critique score < this value, gap filling is triggered.
    Start: 0.62.  Decreases when refinement consistently improves results
    (i.e., critique is triggering correctly).  Increases when refinement
    rarely helps (too many false-positive triggers).

  STORE_QUALITY_THRESHOLD
    Minimum online_quality_score to store in the experience store.
    Start: 0.60.  Increases if retrieved experiences rarely match correctly
    (filtering for higher quality).  Decreases if the store is starved.

  SKIP_WEIGHT_THRESHOLD
    Strategy step weight below which a step is skipped entirely.
    Start: 0.35.  Decreases when skipping hurts quality (too aggressive).
    Increases when keeping low-weight steps wastes budget.

  MIN_CONFIDENCE_TO_ACT
    Minimum calibrated confidence before an RCA result is considered
    actionable (not returned as INSUFFICIENT).
    Start: 30.  Increases when low-confidence results are frequently wrong.

Each threshold uses an exponential moving average with a slow learning rate
(alpha=0.05) to adapt gradually without over-reacting to single outliers.
The adaptation signal for each threshold is drawn from the ongoing quality
feedback already produced by online_evaluator and learning_loop.

Persistence: JSON at ADAPTIVE_THRESHOLDS_PATH (atomic write, thread-safe).

Configuration
-------------
  ADAPTIVE_THRESHOLDS_ENABLED — on/off (default: true)
  ADAPTIVE_THRESHOLDS_PATH    — JSON file (default: eval/adaptive_thresholds.json)
  THRESHOLD_LEARNING_RATE     — EMA alpha (default: 0.05)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.adaptive_thresholds")

ADAPTIVE_THRESHOLDS_ENABLED = os.environ.get(
    "ADAPTIVE_THRESHOLDS_ENABLED", "true"
).lower() in ("1", "true", "yes")

ADAPTIVE_THRESHOLDS_PATH = os.environ.get(
    "ADAPTIVE_THRESHOLDS_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "adaptive_thresholds.json"),
)

THRESHOLD_LEARNING_RATE = float(os.environ.get("THRESHOLD_LEARNING_RATE", "0.05"))

# Hard bounds — prevent runaway adaptation
_BOUNDS: dict[str, tuple[float, float]] = {
    "critique_threshold":       (0.40, 0.80),
    "store_quality_threshold":  (0.40, 0.85),
    "skip_weight_threshold":    (0.20, 0.55),
    "min_confidence_to_act":    (10.0, 60.0),
}

_DEFAULTS: dict[str, float] = {
    "critique_threshold":       0.62,
    "store_quality_threshold":  0.60,
    "skip_weight_threshold":    0.35,
    "min_confidence_to_act":    30.0,
}

_lock = threading.Lock()


@dataclass
class ThresholdEntry:
    """State for a single adaptive threshold."""
    name: str
    value: float            # current effective value
    default: float          # initial value (never changed)
    observations: int = 0   # total updates received
    ema_signal: float = 0.0 # running average of adaptation signal
    last_updated: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ThresholdEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get(name: str) -> float:
    """Return the current adaptive value for a named threshold.

    Falls back to hardcoded default if disabled, not found, or error.
    """
    if not ADAPTIVE_THRESHOLDS_ENABLED:
        return _DEFAULTS.get(name, 0.5)

    try:
        with _lock:
            store = _load()
        entry = store.get(name)
        if isinstance(entry, dict):
            return float(entry.get("value", _DEFAULTS.get(name, 0.5)))
        return _DEFAULTS.get(name, 0.5)
    except Exception as exc:
        logger.debug("Adaptive threshold get failed for %s: %s", name, exc)
        return _DEFAULTS.get(name, 0.5)


def update(
    name: str,
    signal: float,
    context: str = "",
) -> float:
    """Update a threshold with a new adaptation signal.

    The signal should be a value in [0.0, 1.0] indicating whether the
    threshold should *increase* (signal → 1.0) or *decrease* (signal → 0.0)
    from its current position:

        signal > 0.5 → threshold should increase (be more conservative)
        signal < 0.5 → threshold should decrease (be more permissive)
        signal = 0.5 → neutral, no change

    The EMA of signals drives the value update.  The effective change per
    update is bounded by THRESHOLD_LEARNING_RATE (default 0.05 = 5%).

    Returns the new threshold value.
    """
    if not ADAPTIVE_THRESHOLDS_ENABLED:
        return _DEFAULTS.get(name, 0.5)
    if name not in _DEFAULTS:
        logger.debug("Unknown adaptive threshold: %s", name)
        return _DEFAULTS.get(name, 0.5)

    lo, hi = _BOUNDS[name]
    default = _DEFAULTS[name]
    alpha = THRESHOLD_LEARNING_RATE

    try:
        with _lock:
            store = _load()
            raw_entry = store.get(name)
            if isinstance(raw_entry, dict):
                entry = ThresholdEntry.from_dict(raw_entry)
            else:
                entry = ThresholdEntry(
                    name=name, value=default, default=default,
                    observations=0, ema_signal=0.5,
                )

            old_ema = entry.ema_signal if entry.observations > 0 else signal
            new_ema = alpha * signal + (1.0 - alpha) * old_ema

            # Direction: signal > 0.5 pushes value toward hi, < 0.5 toward lo
            direction = (new_ema - 0.5) * 2.0   # in [-1, +1]
            step = direction * (hi - lo) * alpha
            new_value = max(lo, min(hi, entry.value + step))

            entry.ema_signal = round(new_ema, 5)
            entry.value = round(new_value, 4)
            entry.observations += 1
            entry.last_updated = datetime.now(timezone.utc).isoformat()

            store[name] = entry.to_dict()
            _save(store)

        logger.debug(
            "Adaptive threshold %s: %.4f → %.4f (signal=%.3f context=%s)",
            name, raw_entry.get("value", default) if isinstance(raw_entry, dict) else default,
            new_value, signal, context,
        )
        return new_value

    except Exception as exc:
        logger.warning("Adaptive threshold update failed for %s: %s", name, exc)
        return _DEFAULTS.get(name, 0.5)


def record_critique_outcome(
    critique_score: float,
    refinement_triggered: bool,
    refinement_helped: bool,
) -> None:
    """Update CRITIQUE_THRESHOLD based on whether critique + refinement helped.

    Called after each self-critique cycle completes.
    Signal logic:
      - Refinement triggered AND helped → threshold is about right (neutral 0.5)
        but lean toward keeping it: signal = 0.55
      - Refinement triggered AND did NOT help → threshold too aggressive,
        raise it: signal = 0.70
      - Refinement not triggered (score was above threshold) → keep as-is: 0.50
    """
    if refinement_triggered:
        signal = 0.55 if refinement_helped else 0.72
    else:
        signal = 0.50  # no adjustment needed
    update("critique_threshold", signal, context="critique_outcome")


def record_quality_observation(
    online_quality_score: float,
    experience_stored: bool,
) -> None:
    """Update STORE_QUALITY_THRESHOLD based on store saturation signals.

    Signal logic:
      - High quality stored → threshold might be too low (let high-quality in) → neutral 0.50
      - Low quality rejected (score < threshold) but store is dense → raise threshold:  0.65
      - Store is nearly empty (starved) → lower threshold: 0.35
    """
    signal = 0.50  # default neutral
    if experience_stored and online_quality_score > 0.80:
        signal = 0.48  # great quality getting through — stay the course or slightly permissive
    elif not experience_stored and online_quality_score < 0.55:
        signal = 0.52  # correctly rejected low quality — fine
    update("store_quality_threshold", signal, context="quality_obs")


def record_step_skip_outcome(
    step_was_skipped: bool,
    quality_before: float,
    quality_after: float,
) -> None:
    """Update SKIP_WEIGHT_THRESHOLD based on whether skipping helped or hurt.

    Only called when a step is skipped due to low weight.
    Signal logic:
      - Skipped AND quality improved → skip threshold is right or slightly aggressive: 0.55
      - Skipped AND quality degraded → skip threshold too aggressive, lower it: 0.30
    """
    if not step_was_skipped:
        return
    signal = 0.55 if quality_after >= quality_before else 0.30
    update("skip_weight_threshold", signal, context="step_skip_outcome")


def record_confidence_outcome(
    calibrated_confidence: float,
    was_correct: bool,
) -> None:
    """Update MIN_CONFIDENCE_TO_ACT based on historical correctness.

    Signal logic:
      - Low confidence AND correct → threshold might be too high, lower it: 0.35
      - Low confidence AND wrong  → threshold is right or too low, raise it: 0.65
      - High confidence        → neutral 0.50 (not relevant for min threshold)
    """
    if calibrated_confidence > 50:
        return  # only adjust based on low-confidence cases
    signal = 0.35 if was_correct else 0.65
    update("min_confidence_to_act", signal, context="confidence_outcome")


def get_all() -> dict[str, float]:
    """Return all current adaptive threshold values."""
    return {name: get(name) for name in _DEFAULTS}


def reset(name: str | None = None) -> None:
    """Reset one or all thresholds to their defaults (for testing/emergencies)."""
    try:
        with _lock:
            if name:
                store = _load()
                if name in store:
                    del store[name]
                _save(store)
            else:
                _save({})
        logger.info("Adaptive thresholds reset: %s", name or "ALL")
    except Exception as exc:
        logger.warning("Threshold reset failed: %s", exc)


# ---------------------------------------------------------------------------
# Drift detection and auto-correction
# ---------------------------------------------------------------------------

# If a threshold drifts more than this fraction of its [lo, hi] range from
# its default, it is considered "drifted" and eligible for auto-damping.
_DRIFT_FRACTION_WARN  = 0.30   # 30% of range → log warning
_DRIFT_FRACTION_DAMP  = 0.50   # 50% of range → apply damping pull-back
_DAMP_ALPHA           = 0.20   # how strongly to pull back toward default


def detect_drift() -> dict[str, dict]:
    """Check whether any threshold has drifted far from its default.

    Returns a dict keyed by threshold name with:
        current, default, drift_fraction, drifted (bool), recommendation (str)
    """
    results: dict[str, dict] = {}
    try:
        store = _load()
        for name, default in _DEFAULTS.items():
            lo, hi = _BOUNDS[name]
            raw = store.get(name)
            current = float(raw.get("value", default)) if isinstance(raw, dict) else default
            observations = int(raw.get("observations", 0)) if isinstance(raw, dict) else 0
            drift = abs(current - default) / max(1e-9, hi - lo)
            drifted = drift >= _DRIFT_FRACTION_WARN
            if drift >= _DRIFT_FRACTION_DAMP:
                rec = f"CRITICAL: auto-damp recommended (drift={drift:.0%})"
            elif drifted:
                rec = f"WARNING: threshold shifted {drift:.0%} from default — monitor"
            else:
                rec = "OK"
            results[name] = {
                "current": round(current, 4),
                "default": default,
                "drift_fraction": round(drift, 3),
                "observations": observations,
                "drifted": drifted,
                "recommendation": rec,
            }
    except Exception as exc:
        logger.warning("detect_drift failed: %s", exc)
    return results


def auto_damp_drift() -> dict[str, str]:
    """Pull drifted thresholds back toward their defaults.

    For each threshold drifted > _DRIFT_FRACTION_DAMP of its range, apply a
    weighted blend: new = (1-_DAMP_ALPHA) * current + _DAMP_ALPHA * default.
    This avoids a hard reset while correcting runaway drift.

    Returns a dict of {name: action_taken}.
    """
    actions: dict[str, str] = {}
    try:
        with _lock:
            store = _load()
            for name, default in _DEFAULTS.items():
                lo, hi = _BOUNDS[name]
                raw = store.get(name)
                if not isinstance(raw, dict):
                    continue
                current = float(raw.get("value", default))
                drift = abs(current - default) / max(1e-9, hi - lo)
                if drift < _DRIFT_FRACTION_DAMP:
                    actions[name] = "no_action"
                    continue
                damped = (1.0 - _DAMP_ALPHA) * current + _DAMP_ALPHA * default
                damped = round(max(lo, min(hi, damped)), 4)
                raw["value"] = damped
                raw["last_updated"] = datetime.now(timezone.utc).isoformat()
                store[name] = raw
                actions[name] = f"damped {current:.4f} → {damped:.4f}"
                logger.warning(
                    "Adaptive threshold auto-damped: %s %.4f → %.4f (drift was %.0f%% of range)",
                    name, current, damped, drift * 100,
                )
                try:
                    from database.ops_persistence import get_ops_store
                    get_ops_store().persist_safety_event(
                        event_type="threshold_damped",
                        threshold_name=name,
                        old_value=current,
                        new_value=damped,
                        drift_fraction=round(drift, 3),
                        context="auto_damp_drift",
                    )
                except Exception:
                    pass
            _save(store)
    except Exception as exc:
        logger.warning("auto_damp_drift failed: %s", exc)
    return actions


def get_health_report() -> dict:
    """Return a health summary of all adaptive thresholds.

    Includes current values, drift status, observation counts, and
    actionable recommendations for each threshold.
    """
    drift = detect_drift()
    health: dict = {
        "thresholds": drift,
        "overall_status": "OK",
        "drifted_count": sum(1 for v in drift.values() if v.get("drifted")),
        "recommendations": [],
    }
    for name, info in drift.items():
        if "CRITICAL" in info.get("recommendation", ""):
            health["overall_status"] = "CRITICAL"
            health["recommendations"].append(
                f"Run auto_damp_drift() — {name} has drifted {info['drift_fraction']:.0%} from default"
            )
        elif "WARNING" in info.get("recommendation", ""):
            if health["overall_status"] == "OK":
                health["overall_status"] = "WARNING"
            health["recommendations"].append(
                f"Monitor {name}: current={info['current']} default={info['default']} drift={info['drift_fraction']:.0%}"
            )
    if not health["recommendations"]:
        health["recommendations"].append("All thresholds within healthy range")
    return health


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load() -> dict[str, Any]:
    try:
        with open(ADAPTIVE_THRESHOLDS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Adaptive thresholds corrupt, resetting: %s", exc)
        return {}


def _save(data: dict) -> None:
    data["_meta"] = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "entries": sum(1 for k in data if not k.startswith("_")),
    }
    os.makedirs(os.path.dirname(ADAPTIVE_THRESHOLDS_PATH), exist_ok=True)
    tmp = ADAPTIVE_THRESHOLDS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, ADAPTIVE_THRESHOLDS_PATH)
