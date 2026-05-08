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

Per-service weights are tracked alongside per-type weights using the key
format "incident_type.step_label.service".  When a service-specific weight
exists and has enough observations (>= MIN_CALLS_TO_EVOLVE), it takes
precedence over the generic type-level weight for step filtering decisions.

Step skipping: steps whose evolved weight falls below SKIP_WEIGHT_THRESHOLD
(from adaptive_thresholds) are skipped entirely via should_skip_step().

Gap pattern penalization: record_gap_pattern() applies a negative signal to
steps that consistently fail to fill a gap, lowering their weight over time.

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

# Rolling quality circuit-breaker: if the rolling average of the last
# _ROLLING_WINDOW investigations drops below _QUALITY_FLOOR, all evolved
# weights are reset to defaults to prevent the system from converging on a
# bad local optimum.
_ROLLING_WINDOW  = 20
_QUALITY_FLOOR   = 0.40
_rolling_scores: list[float] = []

_strategy_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def should_skip_step(
    incident_type: str,
    step_label: str,
    service: str = "",
) -> bool:
    """Return True if a playbook step should be skipped due to low evolved weight.

    Checks per-service weight first (if service provided and has enough calls),
    then falls back to per-type weight.  Uses SKIP_WEIGHT_THRESHOLD from
    adaptive_thresholds (default 0.35, self-adjusting).

    Returns False (never skip) when evolver is disabled or data is insufficient.
    """
    if not STRATEGY_EVOLVER_ENABLED:
        return False

    try:
        from supervisor.adaptive_thresholds import get as _get_threshold
        skip_threshold = _get_threshold("skip_weight_threshold")
    except Exception:
        skip_threshold = 0.35  # hard fallback

    try:
        with _strategy_lock:
            strategy = _load_raw()

        # Check service-specific weight first
        if service:
            svc_key = f"{incident_type}.{step_label}.{service}"
            svc_entry = strategy.get("_service_weights", {}).get(svc_key)
            if (
                isinstance(svc_entry, dict)
                and svc_entry.get("calls", 0) >= MIN_CALLS_TO_EVOLVE
            ):
                if svc_entry.get("weight", 1.0) < skip_threshold:
                    logger.debug(
                        "Skipping step %s for service=%s (svc_weight=%.3f < threshold=%.3f)",
                        step_label, service, svc_entry["weight"], skip_threshold,
                    )
                    return True

        # Fall back to per-type weight
        type_entry = strategy.get(incident_type, {}).get(step_label)
        if (
            isinstance(type_entry, dict)
            and type_entry.get("calls", 0) >= MIN_CALLS_TO_EVOLVE
            and type_entry.get("weight", 1.0) < skip_threshold
        ):
            logger.debug(
                "Skipping step %s (type_weight=%.3f < threshold=%.3f)",
                step_label, type_entry["weight"], skip_threshold,
            )
            return True

        return False

    except Exception as exc:
        logger.debug("should_skip_step check failed (non-critical): %s", exc)
        return False


def record_outcome(
    incident_type: str,
    receipts: list[dict],
    online_quality_score: float,
    service: str = "",
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

    # Track rolling quality and trigger circuit breaker if needed
    _track_rolling_quality(online_quality_score)

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
                    old_weight = entry.get("weight", 1.0)
                    new_weight = round(max(0.3, min(2.0, new_ema)), 4)
                    entry["weight"] = new_weight
                    # Persist weight change to ops history
                    try:
                        from database.ops_persistence import get_ops_store
                        get_ops_store().persist_weight_change(
                            incident_type=incident_type, step_label=label,
                            weight_before=old_weight, weight_after=new_weight,
                            quality_signal=signal, calls=entry["calls"],
                        )
                    except Exception:
                        pass

                entry["last_updated"] = datetime.now(timezone.utc).isoformat()

            # Per-service weights (keyed "incident_type.step_label.service")
            if service:
                svc_weights = strategy.setdefault("_service_weights", {})
                for label in step_labels:
                    svc_key = f"{incident_type}.{label}.{service}"
                    svc_entry = svc_weights.setdefault(svc_key, {
                        "weight": 1.0,
                        "calls": 0,
                        "ema_signal": None,
                    })
                    svc_entry["calls"] += 1
                    if svc_entry["calls"] >= MIN_CALLS_TO_EVOLVE:
                        old_ema = svc_entry.get("ema_signal")
                        new_ema = signal if old_ema is None else (
                            EMA_ALPHA * signal + (1 - EMA_ALPHA) * old_ema
                        )
                        svc_entry["ema_signal"] = round(new_ema, 4)
                        svc_entry["weight"] = round(max(0.3, min(2.0, new_ema)), 4)
                    svc_entry["last_updated"] = datetime.now(timezone.utc).isoformat()

            strategy["_meta"] = {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_updates": strategy.get("_meta", {}).get("total_updates", 0) + 1,
            }
            _save_raw(strategy)

        logger.info(
            "Strategy updated: type=%s steps=%d signal=%.3f service=%s",
            incident_type, len(step_labels), signal, service or "(none)",
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


def record_gap_pattern(
    incident_type: str,
    service: str,
    gap_categories: list[str],
) -> None:
    """Apply a negative weight signal to steps associated with persistent gaps.

    When a gap category has been identified as persistently missing for a given
    (incident_type, service), this demotes the steps responsible for that category
    so the strategy learns to de-prioritise dead-end evidence paths.

    The negative signal (0.30) is applied once per gap category per investigation.
    This is a weak signal — it takes ~10 repeated gaps before a step is meaningfully
    demoted (EMA alpha=0.12, starting weight=1.0, target weight≈0.3).
    """
    if not STRATEGY_EVOLVER_ENABLED or not gap_categories:
        return

    # Map gap categories to the step labels that are supposed to collect them
    _GAP_STEP_MAP: dict[str, list[str]] = {
        "golden_signals":   ["get_golden_signals", "get_metric_chart"],
        "apm_data":         ["get_service_metrics", "get_apm_data"],
        "logs":             ["get_recent_errors", "search_logs"],
        "metrics":          ["get_golden_signals", "get_service_metrics"],
        "change_records":   ["get_recent_changes", "get_deployments"],
        "itsm_context":     ["get_incident_history", "get_change_record"],
        "confluence_context": ["search_knowledge_base", "search_confluence"],
        "devops_context":   ["get_deployments", "get_recent_changes"],
        "git_context":      ["git_log_for_service", "git_find_breaking_change"],
        "cmdb_blast_radius": ["get_service_dependencies", "get_blast_radius"],
        "trace_correlation": ["get_traces", "get_trace_context"],
        "visual_evidence":  ["get_metric_chart", "get_dashboard_snapshot"],
    }

    gap_signal = 0.30  # negative: consistently missing → demote

    try:
        with _strategy_lock:
            strategy = _load_raw()
            type_weights = strategy.setdefault(incident_type, {})
            svc_weights = strategy.setdefault("_service_weights", {})

            for cat in gap_categories:
                for step_label in _GAP_STEP_MAP.get(cat, []):
                    # Update type-level weight
                    entry = type_weights.setdefault(step_label, {
                        "weight": 1.0, "calls": 0, "ema_signal": None,
                    })
                    entry["calls"] += 1
                    old_ema = entry.get("ema_signal")
                    new_ema = gap_signal if old_ema is None else (
                        EMA_ALPHA * gap_signal + (1 - EMA_ALPHA) * old_ema
                    )
                    entry["ema_signal"] = round(new_ema, 4)
                    entry["weight"] = round(max(0.3, min(2.0, new_ema)), 4)
                    entry["last_updated"] = datetime.now(timezone.utc).isoformat()

                    # Update service-level weight
                    if service:
                        svc_key = f"{incident_type}.{step_label}.{service}"
                        svc_entry = svc_weights.setdefault(svc_key, {
                            "weight": 1.0, "calls": 0, "ema_signal": None,
                        })
                        svc_entry["calls"] += 1
                        old_sema = svc_entry.get("ema_signal")
                        new_sema = gap_signal if old_sema is None else (
                            EMA_ALPHA * gap_signal + (1 - EMA_ALPHA) * old_sema
                        )
                        svc_entry["ema_signal"] = round(new_sema, 4)
                        svc_entry["weight"] = round(max(0.3, min(2.0, new_sema)), 4)
                        svc_entry["last_updated"] = datetime.now(timezone.utc).isoformat()

            strategy["_meta"] = {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_updates": strategy.get("_meta", {}).get("total_updates", 0) + 1,
            }
            _save_raw(strategy)

        logger.debug(
            "Gap pattern signals applied: type=%s service=%s gaps=%s",
            incident_type, service, gap_categories,
        )

    except Exception as exc:
        logger.warning("record_gap_pattern failed (non-critical): %s", exc)


def get_service_weight(incident_type: str, step_label: str, service: str) -> float:
    """Return the service-specific evolved weight for a step, or 1.0 if unknown."""
    if not STRATEGY_EVOLVER_ENABLED or not service:
        return 1.0
    try:
        with _strategy_lock:
            strategy = _load_raw()
        svc_key = f"{incident_type}.{step_label}.{service}"
        svc_entry = strategy.get("_service_weights", {}).get(svc_key)
        if isinstance(svc_entry, dict) and svc_entry.get("calls", 0) >= MIN_CALLS_TO_EVOLVE:
            return float(svc_entry.get("weight", 1.0))
        return 1.0
    except Exception:
        return 1.0


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

def record_outcome_decomposed(
    incident_type: str,
    receipts: list[dict],
    online_quality_score: float,
    dimensions: dict[str, float] | None = None,
    service: str = "",
) -> None:
    """Update step weights using per-dimension quality signals.

    Unlike record_outcome() which applies a single blended signal to all steps,
    this function maps each quality dimension to the steps most responsible for
    it, giving a more precise learning signal:

      - volume / coherence  → evidence-gathering steps (search_logs, get_golden_signals, …)
      - specificity         → analysis steps (analysis, get_incident_by_id, …)
      - calibration         → confidence-adjusting steps
      - diversity           → hypothesis-generating steps

    Falls back to the blended signal for steps not mapped to a specific dimension.
    """
    if not STRATEGY_EVOLVER_ENABLED:
        return

    dims = dimensions or {}
    _track_rolling_quality(online_quality_score)
    blended_signal = online_quality_score / QUALITY_NORM

    # Map dimension → step label substrings that primarily contribute to it
    _DIM_STEP_HINTS: dict[str, list[str]] = {
        "volume":    ["search_logs", "get_error_logs", "query_metrics", "get_events",
                      "get_change_data", "get_recent_deployments"],
        "coherence": ["get_golden_signals", "get_apm_signals", "check_golden_signals",
                      "get_apm_traces", "get_apm_dependencies"],
        "specificity": ["get_incident_by_id", "get_incident_history", "get_trace_context",
                        "get_blast_radius", "get_service_dependencies"],
    }

    step_labels = _extract_step_labels(receipts)
    if not step_labels:
        return

    def _signal_for_step(label: str) -> float:
        for dim, hints in _DIM_STEP_HINTS.items():
            if any(h in label for h in hints) and dim in dims:
                return dims[dim] / QUALITY_NORM
        return blended_signal

    try:
        with _strategy_lock:
            strategy = _load_raw()
            type_weights = strategy.setdefault(incident_type, {})
            svc_weights = strategy.setdefault("_service_weights", {})

            for label in step_labels:
                sig = _signal_for_step(label)
                entry = type_weights.setdefault(label, {"weight": 1.0, "calls": 0, "ema_signal": None})
                entry["calls"] += 1
                if entry["calls"] >= MIN_CALLS_TO_EVOLVE:
                    old_ema = entry.get("ema_signal")
                    new_ema = sig if old_ema is None else EMA_ALPHA * sig + (1 - EMA_ALPHA) * old_ema
                    entry["ema_signal"] = round(new_ema, 4)
                    entry["weight"] = round(max(0.3, min(2.0, new_ema)), 4)
                entry["last_updated"] = datetime.now(timezone.utc).isoformat()

                if service:
                    svc_key = f"{incident_type}.{label}.{service}"
                    svc_entry = svc_weights.setdefault(svc_key, {"weight": 1.0, "calls": 0, "ema_signal": None})
                    svc_entry["calls"] += 1
                    if svc_entry["calls"] >= MIN_CALLS_TO_EVOLVE:
                        old = svc_entry.get("ema_signal")
                        new = sig if old is None else EMA_ALPHA * sig + (1 - EMA_ALPHA) * old
                        svc_entry["ema_signal"] = round(new, 4)
                        svc_entry["weight"] = round(max(0.3, min(2.0, new)), 4)
                    svc_entry["last_updated"] = datetime.now(timezone.utc).isoformat()

            strategy["_meta"] = {
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_updates": strategy.get("_meta", {}).get("total_updates", 0) + 1,
            }
            _save_raw(strategy)

    except Exception as exc:
        logger.warning("record_outcome_decomposed failed (non-critical): %s", exc)


def _track_rolling_quality(score: float) -> None:
    """Add score to rolling window and fire circuit breaker if quality degrades."""
    global _rolling_scores
    _rolling_scores.append(score)
    if len(_rolling_scores) > _ROLLING_WINDOW:
        _rolling_scores = _rolling_scores[-_ROLLING_WINDOW:]

    if len(_rolling_scores) >= _ROLLING_WINDOW:
        avg = sum(_rolling_scores) / len(_rolling_scores)
        if avg < _QUALITY_FLOOR:
            logger.warning(
                "Quality circuit breaker triggered: rolling_avg=%.3f < floor=%.2f "
                "— resetting evolved weights to defaults",
                avg, _QUALITY_FLOOR,
            )
            try:
                from database.ops_persistence import get_ops_store
                get_ops_store().persist_safety_event(
                    event_type="circuit_breaker_fired",
                    context="strategy_evolver",
                    old_value=avg,
                    new_value=_QUALITY_FLOOR,
                    details={"rolling_window": len(_rolling_scores), "quality_floor": _QUALITY_FLOOR},
                )
            except Exception:
                pass
            _reset_weights_to_defaults()
            _rolling_scores.clear()


def _reset_weights_to_defaults() -> None:
    """Reset all evolved step weights to 1.0 while preserving call counts.

    Preserving call counts means the system retains memory of which steps it
    has tried — only the learned weights are cleared so re-learning starts
    unbiased.
    """
    try:
        with _strategy_lock:
            strategy = _load_raw()
            for inc_type, steps in strategy.items():
                if inc_type.startswith("_") or not isinstance(steps, dict):
                    continue
                for label, entry in steps.items():
                    if isinstance(entry, dict):
                        entry["weight"] = 1.0
                        entry["ema_signal"] = None
            for svc_key, entry in strategy.get("_service_weights", {}).items():
                if isinstance(entry, dict):
                    entry["weight"] = 1.0
                    entry["ema_signal"] = None
            strategy.setdefault("_meta", {})["circuit_breaker_reset_at"] = (
                datetime.now(timezone.utc).isoformat()
            )
            _save_raw(strategy)
        logger.info("Strategy weights reset to defaults by circuit breaker")
    except Exception as exc:
        logger.warning("_reset_weights_to_defaults failed: %s", exc)


def get_rolling_quality_stats() -> dict:
    """Return rolling quality window statistics for health monitoring."""
    if not _rolling_scores:
        return {"window": 0, "avg": None, "min": None, "status": "no_data"}
    avg = sum(_rolling_scores) / len(_rolling_scores)
    return {
        "window": len(_rolling_scores),
        "avg": round(avg, 3),
        "min": round(min(_rolling_scores), 3),
        "floor": _QUALITY_FLOOR,
        "status": "degraded" if avg < _QUALITY_FLOOR else "healthy",
    }


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
