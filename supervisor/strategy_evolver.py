"""Strategy evolver: adapt playbook step ordering based on accumulated outcomes.

Every investigation produces an online_quality_score (from online_evaluator).
The strategy evolver records which playbook steps ran and correlates them with
that outcome.  Steps that consistently co-occur with high-quality RCAs are
promoted (weight > 1.0); steps that correlate with poor outcomes or are
frequently empty are demoted (weight < 1.0).

Weight update rule (exponential moving average):
    signal  = online_quality_score / 0.70   (normalised around "good" threshold)
    new_weight = α × signal + (1-α) × old_weight
    α = EMA_ALPHA (default 0.12)

Weights are stored per (incident_type, step_label) and applied by
tool_selector.get_evolved_playbook() to reorder playbook steps before
execution.  Higher-weight steps run earlier → faster time-to-evidence on
common incident patterns.

Persisted to: eval/evolved_strategy.json  (atomic write)
Thread-safe: all mutations under _strategy_lock

Configuration:
  STRATEGY_EVOLVER_ENABLED  — Enable/disable (default: true)
  EVOLVED_STRATEGY_PATH     — JSON file location
  EMA_ALPHA                 — Learning rate (default: 0.12)
  MIN_CALLS_TO_EVOLVE       — Minimum observations before weight diverges from 1.0 (default: 5)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone

logger = logging.getLogger("sentinalai.strategy_evolver")

STRATEGY_EVOLVER_ENABLED = os.environ.get(
    "STRATEGY_EVOLVER_ENABLED", "true"
).lower() in ("1", "true", "yes")

EVOLVED_STRATEGY_PATH = os.environ.get(
    "EVOLVED_STRATEGY_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "evolved_strategy.json"),
)

EMA_ALPHA      = float(os.environ.get("EMA_ALPHA", "0.12"))
MIN_CALLS_TO_EVOLVE = int(os.environ.get("MIN_CALLS_TO_EVOLVE", "5"))
QUALITY_NORM   = 0.70   # online_score / QUALITY_NORM = signal

_strategy_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_outcome(
    incident_type: str,
    receipts: list[dict],
    online_quality_score: float,
) -> None:
    """Update step weights after a completed investigation.

    Args:
        incident_type: Classified incident type (timeout, oomkill, etc.)
        receipts: List of receipt dicts from ReceiptCollector.to_list()
                  Each receipt has keys: tool, action, status, elapsed_ms
        online_quality_score: Score from online_evaluator (0.0–1.0)
    """
    if not STRATEGY_EVOLVER_ENABLED:
        return

    signal = online_quality_score / QUALITY_NORM  # normalised around 0.70 = 1.0

    # Collect step labels that ran successfully in this investigation
    step_labels = _extract_step_labels(receipts)
    if not step_labels:
        return

    try:
        with _strategy_lock:
            strategy = _load_raw()
            type_weights = strategy.setdefault(incident_type, {})

            for label in step_labels:
                entry = type_weights.setdefault(label, {
                    "weight": 1.0,
                    "calls":  0,
                    "ema_signal": None,  # None = uninitialized; seeded from first signal
                })
                entry["calls"] += 1

                # Only start diverging from 1.0 once we have MIN_CALLS_TO_EVOLVE
                if entry["calls"] >= MIN_CALLS_TO_EVOLVE:
                    old_ema = entry.get("ema_signal")
                    if old_ema is None:
                        new_ema = signal  # unbiased seed from first observation
                    else:
                        new_ema = EMA_ALPHA * signal + (1 - EMA_ALPHA) * old_ema
                    entry["ema_signal"] = round(new_ema, 4)
                    # Weight is the EMA signal, clamped to [0.3, 2.0]
                    entry["weight"] = round(max(0.3, min(2.0, new_ema)), 4)

                entry["last_updated"] = datetime.now(timezone.utc).isoformat()

            strategy["_meta"] = {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_updates": strategy.get("_meta", {}).get("total_updates", 0) + 1,
            }
            _save_raw(strategy)

        logger.info(
            "Strategy updated: type=%s steps=%d signal=%.3f",
            incident_type, len(step_labels), signal,
        )

    except Exception as exc:
        logger.warning("Strategy update failed (non-critical): %s", exc)


def get_weights(incident_type: str) -> dict[str, float]:
    """Return current {step_label: weight} map for an incident type.

    Returns empty dict (all steps equal weight) if evolver disabled or error.
    Only returns weights for steps with >= MIN_CALLS_TO_EVOLVE observations.
    """
    if not STRATEGY_EVOLVER_ENABLED:
        return {}

    try:
        with _strategy_lock:
            strategy = _load_raw()

        type_weights = strategy.get(incident_type, {})
        result: dict[str, float] = {}
        for label, entry in type_weights.items():
            if label.startswith("_"):
                continue
            if isinstance(entry, dict) and entry.get("calls", 0) >= MIN_CALLS_TO_EVOLVE:
                result[label] = entry.get("weight", 1.0)

        if result:
            logger.debug(
                "Evolved weights for %s: %s",
                incident_type,
                {k: round(v, 2) for k, v in sorted(result.items(), key=lambda x: -x[1])[:5]},
            )
        return result

    except Exception as exc:
        logger.warning("get_weights failed (non-critical): %s", exc)
        return {}


def get_report() -> dict:
    """Return a full report of the evolved strategy for introspection."""
    try:
        with _strategy_lock:
            strategy = _load_raw()

        report: dict = {"meta": strategy.get("_meta", {}), "incident_types": {}}
        for inc_type, steps in strategy.items():
            if inc_type.startswith("_"):
                continue
            sorted_steps = sorted(
                [(k, v) for k, v in steps.items() if not k.startswith("_") and isinstance(v, dict)],
                key=lambda x: x[1].get("weight", 1.0),
                reverse=True,
            )
            report["incident_types"][inc_type] = [
                {"step": k, "weight": v.get("weight", 1.0), "calls": v.get("calls", 0)}
                for k, v in sorted_steps
            ]
        return report

    except Exception as exc:
        logger.warning("get_report failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_step_labels(receipts: list[dict]) -> list[str]:
    """Extract unique step labels from receipts (tool.action format).

    Only includes successful calls (status in {"ok", "success", "completed"}).
    """
    seen: set[str] = set()
    labels: list[str] = []
    for r in receipts:
        if not isinstance(r, dict):
            continue
        status = r.get("status", "")
        if status not in ("ok", "success", "completed"):
            continue
        tool   = r.get("tool", r.get("worker", ""))
        action = r.get("action", "")
        if tool and action:
            label = f"{action}"   # use action name as label (matches playbook step labels)
            if label not in seen:
                seen.add(label)
                labels.append(label)
    return labels


def _load_raw() -> dict:
    """Load strategy from disk. Returns {} if absent or corrupt."""
    path = EVOLVED_STRATEGY_PATH
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Evolved strategy corrupt, resetting: %s", exc)
        return {}


def _save_raw(strategy: dict) -> None:
    """Persist strategy atomically."""
    path = EVOLVED_STRATEGY_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(strategy, f, indent=2)
    os.replace(tmp, path)
