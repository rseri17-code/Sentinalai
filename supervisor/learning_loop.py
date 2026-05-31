"""Continuous learning loop for SentinalAI.

After every investigation, this module:
  1. Checks whether ground truth exists for the incident.
  2. If so, evaluates the result via GroundTruthEvaluator.
  3. Persists the EvalResult to the database (eval_results table).
  4. Updates the shared ConfidenceCalibrator with the new data point.
  5. Saves the calibration map to disk atomically.

Production feedback path (no ground_truth.json required):
  VerificationLoop calls record_verification_outcome() after fix verification.
  If the fix worked → RCA was correct → positive calibration signal.
  If the fix failed → RCA was wrong → negative signal.
  This closed loop lets the calibrator improve from every production incident.

The entire pipeline is:
  - Non-blocking: caller wraps in try/except; investigation result is unaffected.
  - Thread-safe: calibrator updates are serialised through a module-level lock.
  - Graceful-degrading: each step fails independently; partial success is fine.

Usage (from agent._persist_results):
    from supervisor.learning_loop import run_learning_step
    run_learning_step(incident_id, result)

Usage (from verification_loop):
    from supervisor.learning_loop import record_verification_outcome
    record_verification_outcome(investigation_id, rca_was_correct=True)
"""

from __future__ import annotations

import logging

from supervisor.ground_truth_eval import GroundTruthEvaluator
from supervisor.confidence_calibrator import ConfidenceCalibrator, get_calibrator, _calibrator_lock
from database.persistence import (
    persist_eval_result,
    load_eval_results_for_calibration,
    is_enabled as _db_enabled,
)

logger = logging.getLogger("sentinalai.learning_loop")


def run_learning_step(incident_id: str, result: dict) -> bool:
    """Evaluate result against ground truth and update calibrator if possible.

    Returns True if a full evaluate-persist-calibrate cycle completed.
    Returns False (silently) when ground truth is absent or any step fails.
    """
    # Step 1 — check ground truth corpus
    evaluator = GroundTruthEvaluator.from_file()
    if not evaluator.has_ground_truth(incident_id):
        logger.debug("No ground truth for %s — learning step skipped", incident_id)
        return False

    # Step 2 — evaluate
    eval_result = evaluator.evaluate(incident_id, result)
    if eval_result is None:
        logger.debug("Evaluator returned None for %s", incident_id)
        return False

    logger.info(
        "Learning step: %s match=%s correct=%s conf=%d",
        incident_id,
        eval_result.root_cause_match,
        eval_result.actual_correct,
        eval_result.predicted_confidence,
    )

    # Step 3 — persist eval result (non-blocking; DB may be unavailable)
    try:
        _persist_eval(eval_result, incident_id)
    except Exception as exc:
        logger.warning("Persist eval failed for %s (non-critical): %s", incident_id, exc)

    # Step 4 — update calibrator + neural models under lock
    try:
        _update_calibrator(eval_result, incident_id, result)
    except Exception as exc:
        logger.warning("Calibrator update failed for %s (non-critical): %s", incident_id, exc)

    # Step 5 — feed correctness signal back to PatternRegistry
    _fp = result.get("_dna_fingerprint", "")
    _rc = result.get("root_cause", "")
    if _fp and _rc:
        try:
            _update_pattern_outcome(
                fingerprint=_fp,
                root_cause=_rc,
                was_correct=bool(eval_result.actual_correct),  # type: ignore[attr-defined]
            )
        except Exception as exc:
            logger.debug("Pattern outcome update failed for %s (non-critical): %s", incident_id, exc)

    return True


def record_verification_outcome(
    investigation_id: str,
    rca_was_correct: bool,
    predicted_confidence: float | None = None,
    verification_duration_sec: float = 0.0,
    dna_fingerprint: str = "",
    root_cause: str = "",
) -> None:
    """Feed a production verification signal into the confidence calibrator.

    Called by VerificationLoop after fix verification completes. This is the
    closed loop: fix worked → RCA was correct → positive calibration signal.
    No ground_truth.json entry needed — production is the ground truth.

    Parameters
    ----------
    investigation_id:       Investigation that was verified.
    rca_was_correct:        True if the fix stabilised the service.
    predicted_confidence:   The confidence score from the original RCA (0–100).
                            If None, attempts to load from the DB outcome record.
    verification_duration_sec: How long verification took (for logging).
    """
    try:
        # Resolve predicted confidence if not provided
        conf = predicted_confidence
        if conf is None:
            records = load_eval_results_for_calibration(limit=500)
            # Records are {predicted_confidence, actual_correct} — no investigation_id
            # index, so we can't filter. Use last known confidence as proxy when
            # the DB has entries; otherwise fall back to 0.5 (neutral prior).
            conf = float(records[-1]["predicted_confidence"]) if records else 50.0

        calibration_data = [{"predicted_confidence": conf, "actual_correct": rca_was_correct}]

        with _calibrator_lock:
            calibrator = get_calibrator()
            calibrator.update(calibration_data)
            calibrator.save()

        # Train neural confidence calibrator on the verification outcome.
        # No evidence context here — train on confidence-only features as a
        # partial update (partial information is still a useful gradient signal).
        try:
            from supervisor.neural_confidence_calibrator import get_neural_calibrator
            ncal = get_neural_calibrator()
            ncal.train_one(
                raw_confidence=int(conf),
                actual_correct=1.0 if rca_was_correct else 0.0,
            )
            ncal.save()
        except Exception as exc:
            logger.debug("NeuralConfidenceCalibrator verification train failed: %s", exc)

        # Feed correctness signal back to PatternRegistry when fingerprint is known.
        if dna_fingerprint and root_cause:
            try:
                _update_pattern_outcome(dna_fingerprint, root_cause, rca_was_correct)
            except Exception as exc:
                logger.debug("Pattern outcome update failed for inv=%s (non-critical): %s", investigation_id, exc)

        logger.info(
            "Verification outcome recorded: inv=%s correct=%s predicted_conf=%.0f duration=%.0fs",
            investigation_id,
            rca_was_correct,
            conf,
            verification_duration_sec,
        )
    except Exception as exc:
        # Never let learning loop failure surface to the caller
        logger.warning(
            "record_verification_outcome failed for %s (non-critical): %s",
            investigation_id,
            exc,
        )


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _update_pattern_outcome(fingerprint: str, root_cause: str, was_correct: bool) -> None:
    """Feed a correctness signal into PatternRegistry for the given fingerprint."""
    if not fingerprint or not root_cause:
        return
    from supervisor.pattern_registry import get_registry
    get_registry().update_outcome(fingerprint, root_cause, was_correct)
    logger.debug(
        "PatternRegistry.update_outcome fingerprint=%s hypothesis=%s correct=%s",
        fingerprint, root_cause, was_correct,
    )


def _persist_eval(eval_result: object, incident_id: str) -> None:
    """Persist EvalResult to database. Fails silently if DB unavailable."""
    try:
        if not _db_enabled():
            return
        persist_eval_result(
            incident_id=incident_id,
            root_cause_match=eval_result.root_cause_match,          # type: ignore[attr-defined]
            root_cause_score=eval_result.root_cause_score,          # type: ignore[attr-defined]
            confidence_error=eval_result.confidence_error,          # type: ignore[attr-defined]
            evidence_coverage=eval_result.evidence_coverage,        # type: ignore[attr-defined]
            actual_correct=eval_result.actual_correct,              # type: ignore[attr-defined]
            predicted_confidence=eval_result.predicted_confidence,  # type: ignore[attr-defined]
            missing_evidence=eval_result.missing_evidence,          # type: ignore[attr-defined]
        )
    except Exception as exc:
        logger.warning("Failed to persist eval result for %s: %s", incident_id, exc)


def _update_calibrator(eval_result: object, incident_id: str, result: dict | None = None) -> None:
    """Update the binned calibrator, neural calibrator, and quality net under lock."""
    try:
        calibration_data = [
            {
                "predicted_confidence": eval_result.predicted_confidence,  # type: ignore[attr-defined]
                "actual_correct": eval_result.actual_correct,              # type: ignore[attr-defined]
            }
        ]
        with _calibrator_lock:
            calibrator = get_calibrator()
            calibrator.update(calibration_data)
            calibrator.save()
        logger.debug(
            "Calibrator updated: incident=%s pred_conf=%d correct=%s",
            incident_id,
            eval_result.predicted_confidence,  # type: ignore[attr-defined]
            eval_result.actual_correct,        # type: ignore[attr-defined]
        )
    except Exception as exc:
        logger.warning("Failed to update calibrator for %s: %s", incident_id, exc)

    if result is None:
        return

    target = 1.0 if eval_result.actual_correct else 0.0  # type: ignore[attr-defined]

    # Train neural confidence calibrator on ground-truth outcome
    try:
        from supervisor.neural_confidence_calibrator import get_neural_calibrator
        ncal = get_neural_calibrator()
        loss = ncal.train_from_result(result, target)
        ncal.save()
        logger.debug(
            "NeuralConfidenceCalibrator trained: incident=%s loss=%.4f samples=%d",
            incident_id, loss, ncal.total_samples,
        )
    except Exception as exc:
        logger.debug("NeuralConfidenceCalibrator train skipped for %s: %s", incident_id, exc)

    # Train neural quality net on ground-truth outcome
    try:
        from supervisor.neural_quality_net import get_quality_net, build_features_from_result
        net = get_quality_net()
        features = build_features_from_result(result)
        if features is not None:
            loss = net.train_one(features, target)
            net.save()
            logger.debug(
                "NeuralQualityNet trained: incident=%s loss=%.4f samples=%d alpha=%.3f",
                incident_id, loss, net.total_samples, net.blend_alpha(),
            )
    except Exception as exc:
        logger.debug("NeuralQualityNet train skipped for %s: %s", incident_id, exc)


def generate_self_eval_report() -> dict:
    """Generate a comprehensive health report of the entire self-learning stack.

    Synthesises the state of all learning components and produces actionable
    recommendations.  Safe to call at any time — all reads are non-destructive.

    Returns a dict suitable for logging, API response, or nightly alerting.
    """
    report: dict = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "components": {},
        "overall_status": "OK",
        "action_items": [],
    }

    # --- Adaptive thresholds ---
    try:
        from supervisor.adaptive_thresholds import get_health_report as _ath_health
        ath = _ath_health()
        report["components"]["adaptive_thresholds"] = ath
        if ath.get("overall_status") == "CRITICAL":
            report["overall_status"] = "CRITICAL"
            report["action_items"].extend(ath.get("recommendations", []))
        elif ath.get("overall_status") == "WARNING" and report["overall_status"] == "OK":
            report["overall_status"] = "WARNING"
            report["action_items"].extend(ath.get("recommendations", []))
    except Exception as exc:
        report["components"]["adaptive_thresholds"] = {"error": str(exc)}

    # --- Confidence calibrator ---
    try:
        with _calibrator_lock:
            calibrator = get_calibrator()
            cal_report = calibrator.get_calibration_report()
        report["components"]["confidence_calibrator"] = cal_report
        if calibrator.is_stale():
            if report["overall_status"] == "OK":
                report["overall_status"] = "WARNING"
            report["action_items"].append(
                f"Calibrator is stale: ECE={cal_report.get('ece', '?')} "
                f"samples={cal_report.get('total_samples', 0)} — run rebuild_calibrator_from_db()"
            )
    except Exception as exc:
        report["components"]["confidence_calibrator"] = {"error": str(exc)}

    # --- Strategy evolver ---
    try:
        from supervisor.strategy_evolver import get_rolling_quality_stats, get_report as _se_report
        rolling = get_rolling_quality_stats()
        report["components"]["strategy_evolver"] = {
            "rolling_quality": rolling,
            "report_summary": _se_report().get("meta", {}),
        }
        if rolling.get("status") == "degraded":
            report["overall_status"] = "CRITICAL"
            report["action_items"].append(
                f"Strategy evolver quality degraded: rolling_avg={rolling.get('avg')} "
                f"< floor={rolling.get('floor')} — circuit breaker may fire"
            )
    except Exception as exc:
        report["components"]["strategy_evolver"] = {"error": str(exc)}

    # --- Adaptive thresholds drift (detailed) ---
    try:
        from supervisor.adaptive_thresholds import detect_drift
        drift = detect_drift()
        drifted = {k: v for k, v in drift.items() if v.get("drifted")}
        report["components"]["threshold_drift"] = drift
        if drifted:
            report["action_items"].append(
                f"Drifted thresholds: {list(drifted.keys())} — consider calling auto_damp_drift()"
            )
    except Exception as exc:
        report["components"]["threshold_drift"] = {"error": str(exc)}

    # --- Neural quality net ---
    try:
        from supervisor.neural_quality_net import get_quality_net
        net = get_quality_net()
        nqn_report = net.get_report()
        report["components"]["neural_quality_net"] = nqn_report
        if not nqn_report["active"]:
            report["action_items"].append(
                f"NeuralQualityNet warming: {nqn_report['total_samples']}/{nqn_report.get('min_samples_for_blend', 30)} samples — "
                "heuristic scoring dominant until training data accumulates"
            )
    except Exception as exc:
        report["components"]["neural_quality_net"] = {"error": str(exc)}

    # --- Neural confidence calibrator ---
    try:
        from supervisor.neural_confidence_calibrator import get_neural_calibrator
        ncal = get_neural_calibrator()
        ncal_report = ncal.get_report()
        report["components"]["neural_confidence_calibrator"] = ncal_report
        if not ncal_report["active"]:
            report["action_items"].append(
                f"NeuralConfidenceCalibrator warming: {ncal_report['total_samples']}/{ncal_report.get('min_samples_full', 50)} samples — "
                "binned calibration dominant"
            )
    except Exception as exc:
        report["components"]["neural_confidence_calibrator"] = {"error": str(exc)}

    # --- DB availability ---
    try:
        db_ok = _db_enabled()
        report["components"]["database"] = {"enabled": db_ok}
        if not db_ok:
            report["action_items"].append(
                "Database unavailable — calibrator cannot load historical eval results"
            )
    except Exception as exc:
        report["components"]["database"] = {"error": str(exc)}

    if not report["action_items"]:
        report["action_items"].append("All learning components healthy — no action required")

    logger.info(
        "Self-eval report: status=%s action_items=%d",
        report["overall_status"], len(report["action_items"]),
    )
    return report


def run_nightly_self_improvement() -> dict:
    """Run the nightly self-improvement cycle.

    Executes a sequence of corrective actions based on the current health
    report:
      1. Detect and auto-damp drifted adaptive thresholds
      2. Rebuild calibrator from DB if stale
      3. Log a full health report

    Returns a dict summarising what was done.
    Designed to be called from a nightly cron / scheduled task.
    Never raises — all exceptions are caught and logged.
    """
    summary: dict = {
        "run_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "actions_taken": [],
        "errors": [],
    }

    # Step 1 — auto-damp drifted thresholds
    try:
        from supervisor.adaptive_thresholds import auto_damp_drift
        damp_actions = auto_damp_drift()
        damped = {k: v for k, v in damp_actions.items() if v != "no_action"}
        if damped:
            summary["actions_taken"].append(f"Auto-damped thresholds: {damped}")
            logger.info("Nightly self-improvement: damped thresholds %s", damped)
        else:
            summary["actions_taken"].append("Threshold drift check: all within bounds, no damping needed")
    except Exception as exc:
        summary["errors"].append(f"auto_damp_drift failed: {exc}")
        logger.warning("Nightly self-improvement: auto_damp_drift error: %s", exc)

    # Step 2 — rebuild calibrator from DB if stale
    try:
        with _calibrator_lock:
            calibrator = get_calibrator()
            stale = calibrator.is_stale()
        if stale:
            n = rebuild_calibrator_from_db()
            summary["actions_taken"].append(f"Rebuilt calibrator from {n} DB records (was stale)")
            logger.info("Nightly self-improvement: calibrator rebuilt from %d records", n)
        else:
            summary["actions_taken"].append("Calibrator health check: not stale, no rebuild needed")
    except Exception as exc:
        summary["errors"].append(f"calibrator rebuild failed: {exc}")
        logger.warning("Nightly self-improvement: calibrator rebuild error: %s", exc)

    # Step 3 — generate and log full report
    try:
        report = generate_self_eval_report()
        summary["health_report"] = report
        summary["overall_status"] = report.get("overall_status", "UNKNOWN")
    except Exception as exc:
        summary["errors"].append(f"generate_self_eval_report failed: {exc}")

    logger.info(
        "Nightly self-improvement complete: actions=%d errors=%d status=%s",
        len(summary["actions_taken"]),
        len(summary["errors"]),
        summary.get("overall_status", "UNKNOWN"),
    )
    return summary


def rebuild_calibrator_from_db() -> int:
    """Rebuild the calibration map from all persisted eval results.

    Use this to re-seed the calibrator after a cold start or data correction.
    Returns the number of records loaded.
    """
    try:
        records = load_eval_results_for_calibration(limit=10_000)
        if not records:
            logger.info("rebuild_calibrator_from_db: no eval records found")
            return 0

        with _calibrator_lock:
            import supervisor.confidence_calibrator as _cal_mod
            calibrator = ConfidenceCalibrator()   # fresh bins
            calibrator.update(records)
            calibrator.save()
            # Update the module-level singleton so get_calibrator() returns
            # the rebuilt instance without requiring a process restart.
            _cal_mod._calibrator = calibrator

        logger.info("Calibrator rebuilt from %d eval records", len(records))
        return len(records)

    except Exception as exc:
        logger.warning("Failed to rebuild calibrator from DB: %s", exc)
        return 0
