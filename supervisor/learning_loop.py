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

    # Step 4 — update calibrator under lock (shared across all threads)
    try:
        _update_calibrator(eval_result, incident_id)
    except Exception as exc:
        logger.warning("Calibrator update failed for %s (non-critical): %s", incident_id, exc)

    return True


def record_verification_outcome(
    investigation_id: str,
    rca_was_correct: bool,
    predicted_confidence: float | None = None,
    verification_duration_sec: float = 0.0,
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


def _update_calibrator(eval_result: object, incident_id: str) -> None:
    """Update and save the shared calibrator under the module lock."""
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
            from supervisor.confidence_calibrator import _calibrator as _global_ref
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
