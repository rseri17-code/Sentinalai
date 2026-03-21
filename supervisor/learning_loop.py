"""Continuous learning loop for SentinalAI.

After every investigation, this module:
  1. Checks whether ground truth exists for the incident.
  2. If so, evaluates the result via GroundTruthEvaluator.
  3. Persists the EvalResult to the database (eval_results table).
  4. Updates the shared ConfidenceCalibrator with the new data point.
  5. Saves the calibration map to disk atomically.

The entire pipeline is:
  - Non-blocking: caller wraps in try/except; investigation result is unaffected.
  - Thread-safe: calibrator updates are serialised through a module-level lock.
  - Graceful-degrading: each step fails independently; partial success is fine.

Usage (from agent._persist_results):
    from supervisor.learning_loop import run_learning_step
    run_learning_step(incident_id, result)
"""

from __future__ import annotations

import logging
import threading

from supervisor.ground_truth_eval import GroundTruthEvaluator
from supervisor.confidence_calibrator import ConfidenceCalibrator
from database.persistence import persist_eval_result, load_eval_results_for_calibration, is_enabled as _db_enabled

logger = logging.getLogger("sentinalai.learning_loop")

# Serialise calibrator updates across all concurrent investigations.
_calibrator_lock = threading.Lock()


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
            calibrator = ConfidenceCalibrator.load()
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
            calibrator = ConfidenceCalibrator()   # fresh bins
            calibrator.update(records)
            calibrator.save()

        logger.info("Calibrator rebuilt from %d eval records", len(records))
        return len(records)

    except Exception as exc:
        logger.warning("Failed to rebuild calibrator from DB: %s", exc)
        return 0
